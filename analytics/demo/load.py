"""Adapter for production SQL in a new disposable PostgreSQL; see README."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from analytics.demo.build import dataset
from analytics.superset.demo import AS_OF
from app.models import Receipt, ReceiptItem
from app.services.analytical_identity import Product, apply_review, export_review


def seed(engine):
    with Session(engine) as session:
        identities = {}
        for row in dataset()["rows"]:
            receipt = Receipt(gmail_message_id=f"synthetic-public-{row['id']}",
                purchased_at=datetime(2026, row["month"], row["day"], 9, tzinfo=UTC),
                created_at=AS_OF, updated_at=AS_OF, store=row["store"], status="parsed",
                currency="RUB", operation_type=row["operation"], total=row["amount"])
            if row["amount"] is None:
                session.add(receipt)
                continue
            item = ReceiptItem(receipt=receipt, original_name=row["name"],
                normalized_name=row["name"], category="demo-food" if row["unit"] == "l"
                else "demo-other", quantity=row["quantity"], unit="шт.",
                unit_price=row["price"], total=row["amount"])
            session.add(item)
            session.flush()
            if row["mapped"]:
                identities[item.id] = row["product"]
        review = export_review(session, list(identities), "Explicit public synthetic seed 20261027")
        products = sorted(set(identities.values()))
        review.products = [Product(id=key, description=f"Synthetic {key}", brand="Synthetic",
            variant="demo", packaging="1 l" if "milk" in key else "1 kg" if "rice" in key
            else "piece", comparison_unit="l" if "milk" in key else "kg" if "rice" in key
            else "piece", content_quantity=Decimal(1) if "milk" in key or "rice" in key
            else None, content_unit="l" if "milk" in key else "kg" if "rice" in key else None)
            for key in products]
        for decision in review.decisions:
            decision.status = "confirmed"
            decision.product_id = identities[decision.item_id]
            decision.quantity_basis = "package_count" if decision.product_id in (
                "demo-milk-1l", "demo-rice-1kg") else "piece_count"
        version = apply_review(session, review)
        session.commit()
    return version
