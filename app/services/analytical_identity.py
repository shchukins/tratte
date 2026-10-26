"""Explicit, versioned item decisions. No importer or ProductAlias writes."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import (
    AnalyticsItemMapping,
    AnalyticsMappingVersion,
    AnalyticsProduct,
    Receipt,
    ReceiptItem,
)

RULES = "analytical_identity_v1"
UNITS = {
    "kg": ("kg", Decimal(1)),
    "кг": ("kg", Decimal(1)),
    "g": ("kg", Decimal("0.001")),
    "г": ("kg", Decimal("0.001")),
    "l": ("l", Decimal(1)),
    "л": ("l", Decimal(1)),
    "ml": ("l", Decimal("0.001")),
    "мл": ("l", Decimal("0.001")),
    "piece": ("piece", Decimal(1)),
    "шт": ("piece", Decimal(1)),
    "шт.": ("piece", Decimal(1)),
    "шт./шт.": ("piece", Decimal(1)),
    "ед.": ("piece", Decimal(1)),
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Product(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    description: str = Field(min_length=1)
    # Operator supplies the distinguishing properties, including explicit unknowns.
    brand: str = Field(min_length=1)
    variant: str = Field(min_length=1)
    packaging: str = Field(min_length=1)
    comparison_unit: Literal["kg", "l", "piece"]
    content_quantity: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=9)
    content_unit: Literal["g", "kg", "ml", "l"] | None = None


class ItemEvidence(StrictModel):
    original_name: str
    normalized_name: str
    quantity: str
    unit: str | None


class Decision(StrictModel):
    item_id: int = Field(gt=0)
    source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence: ItemEvidence
    status: Literal["confirmed", "rejected", "unresolved"] = "unresolved"
    product_id: str | None = None
    quantity_basis: (
        Literal["source_weight", "source_volume", "package_count", "piece_count"] | None
    ) = None
    source: str = Field(min_length=1)
    note: str = ""


class Review(StrictModel):
    rules_version: Literal["analytical_identity_v1"] = RULES
    parent_version: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    scope: str = Field(min_length=1)
    products: list[Product] = Field(default_factory=list)
    decisions: list[Decision]

    @field_validator("decisions")
    @classmethod
    def unique_items(cls, value):
        if len({d.item_id for d in value}) != len(value):
            raise ValueError("Duplicate item IDs")
        return sorted(value, key=lambda d: d.item_id)

    @field_validator("products")
    @classmethod
    def unique_products(cls, value):
        if len({p.id for p in value}) != len(value):
            raise ValueError("Duplicate product IDs")
        return sorted(value, key=lambda p: p.id)


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def decimal_text(value: Decimal | None):
    return None if value is None else format(Decimal(value).normalize(), "f")


def timestamp(value: datetime | None):
    if value is None:
        return None
    # SQLite drops timezone information; timestamps in Tratte are stored in UTC.
    return (
        value.replace(tzinfo=UTC).isoformat()
        if value.tzinfo is None
        else (value.astimezone(UTC).isoformat())
    )


def fingerprint(item: ReceiptItem) -> str:
    receipt = item.receipt
    return digest(
        {
            "item": {
                k: getattr(item, k)
                for k in (
                    "id",
                    "receipt_id",
                    "original_name",
                    "normalized_name",
                    "unit",
                    "category",
                    "vat_rate",
                )
            },
            "values": {
                k: decimal_text(getattr(item, k)) for k in ("quantity", "unit_price", "total")
            },
            "receipt": {
                k: getattr(receipt, k)
                for k in ("id", "store", "seller", "currency", "operation_type", "status")
            },
            "receipt_total": decimal_text(receipt.total),
            "purchased_at": timestamp(receipt.purchased_at),
            "created_at": timestamp(receipt.created_at),
            "updated_at": timestamp(receipt.updated_at),
        }
    )


def unit(value: str | None):
    result = UNITS.get((value or "").strip().lower())
    if result is None:
        raise ValueError("Unsupported source unit")
    return result


def product_spec(product: Product) -> dict:
    result = product.model_dump(mode="json")
    result["content_quantity"] = decimal_text(product.content_quantity)
    return result


def evidence(item: ReceiptItem) -> ItemEvidence:
    return ItemEvidence(
        original_name=item.original_name,
        normalized_name=item.normalized_name,
        quantity=decimal_text(item.quantity),
        unit=item.unit,
    )


def check_product(product: Product):
    if (product.content_quantity is None) != (product.content_unit is None):
        raise ValueError("Content quantity and unit must be supplied together")
    if product.content_unit and unit(product.content_unit)[0] != product.comparison_unit:
        raise ValueError("Package contents and comparison unit have different dimensions")


def factor_for(item: ReceiptItem, decision: Decision, product: Product) -> Decimal:
    dimension, multiplier = unit(item.unit)
    basis = decision.quantity_basis
    if basis == "package_count":
        if dimension != "piece" or product.content_quantity is None or product.content_unit is None:
            raise ValueError("Package count requires count source unit and known contents")
        target, scale = unit(product.content_unit)
        factor = product.content_quantity * scale
    elif basis in ("source_weight", "source_volume"):
        target = "kg" if basis == "source_weight" else "l"
        if dimension != target:
            raise ValueError("Source quantity has incompatible dimension")
        factor = multiplier  # Never multiply an already weighed quantity by package contents.
    elif basis == "piece_count" and dimension == "piece":
        target, factor = "piece", Decimal(1)
    else:
        raise ValueError("Invalid quantity basis")
    if product.comparison_unit != target:
        raise ValueError("Mass, volume and count cannot be interchanged")
    if factor <= 0 or factor != factor.quantize(Decimal("0.000000001")):
        raise ValueError("Factor must be positive and exactly representable to 9 decimal places")
    return factor


def load_items(session: Session, ids: list[int]) -> dict[int, ReceiptItem]:
    # Avoid SQLite/driver parameter limits for explicit larger backfills.
    result = {}
    for offset in range(0, len(ids), 500):
        rows = session.scalars(
            select(ReceiptItem)
            .options(joinedload(ReceiptItem.receipt))
            .where(ReceiptItem.id.in_(ids[offset : offset + 500]))
            .execution_options(populate_existing=True)
        )
        result.update((item.id, item) for item in rows)
    return result


def export_review(session: Session, ids: list[int], scope: str) -> Review:
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Select a nonempty set of unique item IDs")
    items = load_items(session, ids)
    if set(items) != set(ids):
        raise ValueError("Selected source items are missing")
    return Review(
        scope=scope,
        decisions=[
            Decision(
                item_id=i,
                source_fingerprint=fingerprint(items[i]),
                evidence=evidence(items[i]),
                source="manual_review",
            )
            for i in ids
        ],
    )


def apply_review(session: Session, review: Review) -> str:
    """Validate all decisions, then stage one atomic immutable snapshot; caller commits."""
    # Revalidate programmatic callers too, including list ordering and duplicate IDs.
    review = Review.model_validate(review.model_dump(mode="json"))
    if not review.decisions:
        raise ValueError("An empty review cannot be applied")
    if review.parent_version:
        if session.get(AnalyticsMappingVersion, review.parent_version) is None:
            raise ValueError("Unknown parent version")
    products = {p.id: p for p in review.products}
    referenced = {d.product_id for d in review.decisions if d.status == "confirmed"}
    if set(products) != referenced:
        raise ValueError("Provide exactly the products referenced by confirmed decisions")
    for product in products.values():
        check_product(product)
        existing = session.get(AnalyticsProduct, product.id)
        if existing and existing.specification != canonical(product_spec(product)):
            raise ValueError("Product ID already has a different specification; use a new ID")
    items = load_items(session, [d.item_id for d in review.decisions])
    prepared = []
    for decision in review.decisions:
        item = items.get(decision.item_id)
        if (
            item is None
            or fingerprint(item) != decision.source_fingerprint
            or (evidence(item) != decision.evidence)
        ):
            raise ValueError(f"Stale or missing source item: {decision.item_id}")
        factor = None
        if decision.status == "confirmed":
            factor = factor_for(item, decision, products[decision.product_id])
        elif decision.product_id is not None or decision.quantity_basis is not None:
            raise ValueError("Unconfirmed decisions cannot carry a product or quantity basis")
        prepared.append((decision, factor))
    document = review.model_dump(mode="json")
    document["products"] = [product_spec(p) for p in review.products]
    version_id = digest(document)
    if session.get(AnalyticsMappingVersion, version_id):
        validate_version(session, version_id)
        return version_id
    for product in products.values():
        if session.get(AnalyticsProduct, product.id) is None:
            session.add(
                AnalyticsProduct(
                    id=product.id,
                    specification=canonical(product_spec(product)),
                    comparison_unit=product.comparison_unit,
                )
            )
    session.add(
        AnalyticsMappingVersion(id=version_id, rules_version=RULES, manifest=canonical(document))
    )
    session.flush()
    for decision, factor in prepared:
        session.add(
            AnalyticsItemMapping(
                mapping_version_id=version_id,
                item_id=decision.item_id,
                source_item_id=decision.item_id,
                source_fingerprint=decision.source_fingerprint,
                status=decision.status,
                product_id=decision.product_id,
                quantity_basis=decision.quantity_basis,
                factor=factor,
                source=decision.source,
                note=decision.note,
            )
        )
    session.flush()
    return version_id


def validate_version(session: Session, version_id: str) -> dict:
    """Mandatory gate for consumers: fail closed after deletion, reprocess or tampering."""
    version = session.get(AnalyticsMappingVersion, version_id, populate_existing=True)
    if version is None:
        raise ValueError("Unknown mapping version")
    document = json.loads(version.manifest)
    review = Review.model_validate(document)
    if digest(document) != version_id or version.rules_version != RULES:
        raise ValueError("Mapping version integrity failure")
    rows = {
        m.item_id: m
        for m in session.scalars(
            select(AnalyticsItemMapping)
            .where(AnalyticsItemMapping.mapping_version_id == version_id)
            .execution_options(populate_existing=True)
        )
    }
    if set(rows) != {d.item_id for d in review.decisions}:
        raise ValueError("Mapping rows differ from sealed manifest")
    products = {p.id: p for p in review.products}
    for product in products.values():
        stored = session.get(AnalyticsProduct, product.id, populate_existing=True)
        if (
            stored is None
            or stored.specification != canonical(product_spec(product))
            or (stored.comparison_unit != product.comparison_unit)
        ):
            raise ValueError("Catalog integrity failure")
    items = load_items(session, list(rows))
    result = {"version": version_id, "confirmed": 0, "rejected": 0, "unresolved": 0}
    for decision in review.decisions:
        row, item = rows[decision.item_id], items.get(decision.item_id)
        if (
            row.source_item_id is None
            or item is None
            or fingerprint(item) != (decision.source_fingerprint)
            or evidence(item) != decision.evidence
        ):
            raise ValueError(f"Stale or missing source item: {decision.item_id}")
        expected = (
            factor_for(item, decision, products[decision.product_id])
            if (decision.status == "confirmed")
            else None
        )
        if (
            any(
                getattr(row, field) != getattr(decision, field)
                for field in (
                    "source_fingerprint",
                    "status",
                    "product_id",
                    "quantity_basis",
                    "source",
                    "note",
                )
            )
            or row.factor != expected
        ):
            raise ValueError("Mapping integrity failure")
        result[decision.status] += 1
    return result


def candidates(session: Session, base_month: date, current_month: date) -> list[dict]:
    """Exact audit-v1 keys with >=2 different eligible receipts in each complete month.

    Output is private review material, never automatically a product identity.
    """

    def next_month(value):
        return date(value.year + (value.month == 12), value.month % 12 + 1, 1)

    if (
        base_month.day != 1
        or current_month != next_month(base_month)
        or (next_month(current_month) > datetime.now(ZoneInfo("Europe/Moscow")).date())
    ):
        raise ValueError("Select two adjacent completed calendar months")
    zone = ZoneInfo("Europe/Moscow")
    start = datetime.combine(base_month, datetime.min.time(), zone)
    middle = datetime.combine(current_month, datetime.min.time(), zone)
    end = datetime.combine(next_month(current_month), datetime.min.time(), zone)
    rows = session.scalars(
        select(ReceiptItem)
        .join(Receipt)
        .options(joinedload(ReceiptItem.receipt))
        .where(
            Receipt.purchased_at >= start.astimezone(UTC),
            Receipt.purchased_at < end.astimezone(UTC),
            Receipt.status == "parsed",
            Receipt.currency == "RUB",
            Receipt.total > 0,
            ReceiptItem.quantity > 0,
            ReceiptItem.unit_price > 0,
            ReceiptItem.total > 0,
        )
    )
    groups = {}
    for item in rows:
        if (item.receipt.operation_type or "").strip().lower() != "приход":
            continue
        if not item.normalized_name.strip():
            continue
        # Deliberately preserve the audit-v1 candidate population (count/kg only).
        raw_unit = (item.unit or "").strip().lower()
        if raw_unit not in {"шт", "шт.", "шт./шт.", "ед.", "кг"}:
            continue
        key = (item.normalized_name, "kg" if raw_unit == "кг" else "piece")
        group = groups.setdefault(key, [[], []])
        purchase = item.receipt.purchased_at
        purchase = purchase.replace(tzinfo=UTC) if purchase.tzinfo is None else purchase
        group[int(purchase >= middle)].append(item)
    return [
        {
            "normalized_name": key[0],
            "audit_unit": key[1],
            "receipt_counts": [len({i.receipt_id for i in month}) for month in months],
            "items": [
                {
                    "item_id": i.id,
                    "original_name": i.original_name,
                    "quantity": decimal_text(i.quantity),
                    "unit": i.unit,
                    "source_fingerprint": fingerprint(i),
                }
                for i in sorted(months[0] + months[1], key=lambda i: i.id)
            ],
        }
        for key, months in sorted(groups.items())
        if all(len({i.receipt_id for i in month}) >= 2 for month in months)
    ]
