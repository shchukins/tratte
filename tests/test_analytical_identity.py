import json
import os
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app import analytics_cli
from app.config import get_settings
from app.models import (
    AnalyticsItemMapping,
    AnalyticsMappingVersion,
    AnalyticsProduct,
    Receipt,
    ReceiptItem,
)
from app.services.analytical_identity import (
    Product,
    Review,
    apply_review,
    candidates,
    export_review,
    fingerprint,
    validate_version,
)


@pytest.fixture
def migrated(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/identity.db"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def enable_fk(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()
    get_settings.cache_clear()


def seed(session, name="synthetic milk", unit="шт.", quantity="2", day="2026-08-04"):
    receipt = Receipt(
        gmail_message_id=f"synthetic-{session.scalar(select(func.count(Receipt.id)))}",
        purchased_at=datetime.fromisoformat(day).replace(tzinfo=UTC),
        status="parsed",
        operation_type="ПРИХОД",
        total=100,
        currency="RUB",
    )
    item = ReceiptItem(
        original_name=name,
        normalized_name=name,
        unit=unit,
        quantity=Decimal(quantity),
        unit_price=50,
        total=100,
        receipt=receipt,
    )
    session.add(receipt)
    session.commit()
    return item


def product(id="milk-930", amount="930", content_unit="ml", comparison="l", **extra):
    return Product(
        id=id,
        description="Synthetic milk",
        brand="Synthetic brand",
        variant="plain 2.5%",
        packaging="bottle",
        comparison_unit=comparison,
        content_quantity=Decimal(amount) if amount else None,
        content_unit=content_unit,
        **extra,
    )


def confirm(session, item, p=None, basis="package_count"):
    p = p or product()
    review = export_review(session, [item.id], "synthetic-selected-sample")
    review.products = [p]
    review.decisions[0].status = "confirmed"
    review.decisions[0].product_id = p.id
    review.decisions[0].quantity_basis = basis
    return review


@pytest.mark.parametrize(
    "content,unit,source_unit,basis,qty,expected",
    [
        ("930", "ml", "шт.", "package_count", "2", "1.86"),
        ("1", "l", "шт", "package_count", "2", "2"),
        ("500", "g", "шт", "package_count", "2", "1"),
        (None, None, "г", "source_weight", "250", "0.25"),
        (None, None, "кг", "source_weight", "0.25", "0.25"),
        ("500", "g", "кг", "source_weight", "0.25", "0.25"),  # no double multiplication
        (None, None, "мл", "source_volume", "500", "0.5"),
        (None, None, "л", "source_volume", "1.5", "1.5"),
        (None, None, "шт.", "piece_count", "2", "2"),
    ],
)
def test_quantity_conversion(migrated, content, unit, source_unit, basis, qty, expected):
    comparison = "kg" if unit in ("g", "kg") or basis == "source_weight" else "l"
    if basis == "piece_count":
        comparison = "piece"
    item = seed(migrated, unit=source_unit, quantity=qty)
    review = confirm(
        migrated, item, product(amount=content, content_unit=unit, comparison=comparison), basis
    )
    version = apply_review(migrated, review)
    migrated.commit()
    mapping = migrated.get(AnalyticsItemMapping, (version, item.id))
    assert mapping.factor * item.quantity == Decimal(expected)
    assert validate_version(migrated, version)["confirmed"] == 1


@pytest.mark.parametrize(
    "p,basis,source_unit",
    [
        (product(amount=None, content_unit=None), "package_count", "шт."),
        (product(), "source_weight", "кг"),
        (product(), "source_volume", "кг"),
        (product(), "package_count", "кг"),
        (product(), "piece_count", "шт."),
        (product(amount="500", content_unit="g"), "package_count", "шт."),
        (product(amount="1", content_unit=None), "package_count", "шт."),
        (product(), "package_count", "unknown"),
    ],
)
def test_invalid_or_unknown_units_fail_atomically(migrated, p, basis, source_unit):
    item = seed(migrated, unit=source_unit)
    before = fingerprint(item)
    with pytest.raises(ValueError):
        apply_review(migrated, confirm(migrated, item, p, basis))
    assert migrated.scalar(select(func.count(AnalyticsMappingVersion.id))) == 0
    assert fingerprint(item) == before


def test_versions_idempotency_revocation_and_distinct_packages(migrated):
    item = seed(migrated)
    other = seed(migrated, name="synthetic milk 1 l")
    review = confirm(migrated, item)
    second = confirm(migrated, other, product("milk-1l", "1", "l"))
    review.products += second.products
    review.decisions += second.decisions
    version = apply_review(migrated, review)
    migrated.commit()
    assert apply_review(migrated, Review.model_validate_json(review.model_dump_json())) == version
    assert migrated.scalar(select(func.count(AnalyticsItemMapping.item_id))) == 2
    assert migrated.scalar(select(func.count(AnalyticsProduct.id))) == 2
    # Revoke only the mistaken item in a NEW complete snapshot.
    review.parent_version = version
    review.decisions[0].status = "rejected"
    review.decisions[0].product_id = review.decisions[0].quantity_basis = None
    review.decisions[0].note = "Wrong variant"
    review.products = [second.products[0]]
    corrected = apply_review(migrated, review)
    migrated.commit()
    assert corrected != version
    assert validate_version(migrated, corrected)["rejected"] == 1
    assert validate_version(migrated, version)["confirmed"] == 2


def test_same_id_cannot_change_brand_variant_or_package(migrated):
    item = seed(migrated)
    review = confirm(migrated, item)
    apply_review(migrated, review)
    migrated.commit()
    for field, value in [
        ("brand", "other"),
        ("variant", "chocolate"),
        ("content_quantity", Decimal(1000)),
    ]:
        revised = review.model_copy(deep=True)
        setattr(revised.products[0], field, value)
        with pytest.raises(ValueError, match="different specification"):
            apply_review(migrated, revised)


@pytest.mark.parametrize(
    "target,field,value",
    [
        ("item", "quantity", Decimal("3")),
        ("item", "original_name", "new variant"),
        ("item", "normalized_name", "new alias"),
        ("item", "unit", "кг"),
        ("item", "total", Decimal("150")),
        ("receipt", "operation_type", "ВОЗВРАТ ПРИХОДА"),
        ("receipt", "currency", "EUR"),
        ("receipt", "purchased_at", datetime(2026, 9, 1, tzinfo=UTC)),
    ],
)
def test_source_edits_invalidate_export_and_calculation(migrated, target, field, value):
    item = seed(migrated)
    review = confirm(migrated, item)
    version = apply_review(migrated, review)
    migrated.commit()
    setattr(item if target == "item" else item.receipt, field, value)
    migrated.commit()
    with pytest.raises(ValueError, match="Stale"):
        validate_version(migrated, version)
    with pytest.raises(ValueError, match="Stale"):
        apply_review(migrated, review)


def test_reprocess_deletion_preserves_history_and_import_still_works(migrated):
    item = seed(migrated)
    version = apply_review(migrated, confirm(migrated, item))
    migrated.commit()
    item_id = item.id
    # retry_failed deletes the receipt (and its items) before reimporting it.
    migrated.delete(item.receipt)
    migrated.commit()
    migrated.expire_all()
    row = migrated.get(AnalyticsItemMapping, (version, item_id))
    assert row.source_item_id is None
    assert row.item_id == item_id
    with pytest.raises(ValueError, match="Stale"):
        validate_version(migrated, version)
    new_item = seed(migrated, name="new unmatched product")
    assert migrated.scalar(select(func.count(AnalyticsItemMapping.item_id))) == 1
    assert new_item.id is not None


@pytest.mark.parametrize(
    "table,column",
    [
        ("analytics_products", "specification"),
        ("analytics_mapping_versions", "manifest"),
        ("analytics_item_mappings", "note"),
    ],
)
def test_database_prevents_snapshot_mutation(migrated, table, column):
    item = seed(migrated)
    apply_review(migrated, confirm(migrated, item))
    migrated.commit()
    for statement in (f"UPDATE {table} SET {column} = 'tampered'", f"DELETE FROM {table}"):
        with pytest.raises(IntegrityError):
            with migrated.begin_nested():
                migrated.execute(text(statement))


def test_selection_unknown_and_unresolved_are_explicit(migrated):
    item = seed(migrated)
    seed(migrated, name="outside selection")
    review = export_review(migrated, [item.id], "chosen set")
    version = apply_review(migrated, review)
    migrated.commit()
    assert validate_version(migrated, version)["unresolved"] == 1
    assert migrated.scalar(select(func.count(AnalyticsItemMapping.item_id))) == 1
    with pytest.raises(ValueError, match="missing"):
        export_review(migrated, [999], "missing")
    with pytest.raises(ValueError):
        export_review(migrated, [item.id, item.id], "duplicates")


def test_candidates_exact_audit_threshold_and_private_cli(migrated, monkeypatch, tmp_path):
    for day in ["2026-08-02", "2026-08-03", "2026-09-02", "2026-09-03"]:
        seed(migrated, day=day)
    seed(migrated, name="different flavor", day="2026-08-04")
    result = candidates(migrated, date(2026, 8, 1), date(2026, 9, 1))
    assert len(result) == 1
    assert result[0]["receipt_counts"] == [2, 2]
    monkeypatch.setattr(analytics_cli, "SessionLocal", lambda: Session(migrated.bind))
    runner = CliRunner()
    output = tmp_path / "private.json"
    assert (
        runner.invoke(
            analytics_cli.cli, ["candidates", "2026-08-01", "2026-09-01", str(output)]
        ).exit_code
        == 0
    )
    assert output.stat().st_mode & 0o777 == 0o600
    assert len(json.loads(output.read_text())) == 1
    assert (
        runner.invoke(
            analytics_cli.cli, ["candidates", "2026-08-01", "2026-09-01", str(output)]
        ).exit_code
        != 0
    )
    item_id = result[0]["items"][0]["item_id"]
    review_path = tmp_path / "review.json"
    assert (
        runner.invoke(
            analytics_cli.cli, ["export", str(review_path), str(item_id), "selected sample"]
        ).exit_code
        == 0
    )
    applied = runner.invoke(analytics_cli.cli, ["apply", str(review_path)])
    assert applied.exit_code == 0, applied.output
    version = applied.output.strip()
    assert runner.invoke(analytics_cli.cli, ["validate", version]).exit_code == 0
    revised = tmp_path / "revise.json"
    assert runner.invoke(analytics_cli.cli, ["revise", version, str(revised)]).exit_code == 0
    assert json.loads(revised.read_text())["parent_version"] == version


def test_migration_rollback_keeps_source_data(migrated):
    item = seed(migrated)
    apply_review(migrated, confirm(migrated, item))
    migrated.commit()
    migrated.close()
    command.downgrade(Config("alembic.ini"), "0002_weekly_summary_deliveries")
    assert "analytics_products" not in inspect(migrated.bind).get_table_names()
    assert migrated.scalar(select(func.count(Receipt.id))) == 1
    command.upgrade(Config("alembic.ini"), "head")
    assert "analytics_products" in inspect(migrated.bind).get_table_names()


def test_postgres_migration_guards_and_reprocess(monkeypatch):
    """Isolated CI database only; never use a production DSN here."""
    dsn = os.environ.get("TRATTE_AUDIT_TEST_DSN")
    if not dsn:
        pytest.skip("TRATTE_AUDIT_TEST_DSN required for disposable PostgreSQL")
    import uuid

    schema = "identity_test_" + uuid.uuid4().hex
    url = dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
    # PGOPTIONS affects only these new test connections, not the shared audit tests.
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head")
        isolated = create_engine(url)
        with Session(isolated, expire_on_commit=False) as session:
            item = seed(session)
            version = apply_review(session, confirm(session, item))
            session.commit()
            assert validate_version(session, version)["confirmed"] == 1
            for sql in [
                "UPDATE analytics_products SET specification = 'bad'",
                "DELETE FROM analytics_mapping_versions",
                "UPDATE analytics_item_mappings SET factor = 2",
            ]:
                with pytest.raises(Exception, match="immutable"):
                    with session.begin_nested():
                        session.execute(text(sql))
            item.receipt.items.clear()
            session.commit()
            with pytest.raises(ValueError, match="Stale"):
                validate_version(session, version)
        command.downgrade(Config("alembic.ini"), "0002_weekly_summary_deliveries")
        command.upgrade(Config("alembic.ini"), "head")
        isolated.dispose()
    finally:
        get_settings.cache_clear()
        with engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        engine.dispose()


def test_full_import_path_without_mapping_and_reprocess_with_mapping(migrated, html):
    from app.schemas import EmailMessage
    from app.services.importer import ReceiptImporter

    importer = ReceiptImporter(migrated)
    message = EmailMessage(
        "synthetic-import", "ofdreceipt@beeline.ru", "synthetic", html("beeline.html")
    )
    assert importer.import_message(message) == "parsed"
    assert migrated.scalar(select(func.count(AnalyticsItemMapping.item_id))) == 0
    item = migrated.scalar(select(ReceiptItem).order_by(ReceiptItem.id))
    review = export_review(migrated, [item.id], "synthetic import")
    version = apply_review(migrated, review)
    migrated.commit()
    migrated.delete(item.receipt)
    migrated.commit()
    assert importer.import_message(message) == "parsed"
    with pytest.raises(ValueError, match="Stale"):
        validate_version(migrated, version)


def test_review_evidence_cannot_be_silently_changed(migrated):
    item = seed(migrated)
    review = confirm(migrated, item)
    review.decisions[0].evidence.original_name = "a different product"
    with pytest.raises(ValueError, match="Stale"):
        apply_review(migrated, review)
    assert migrated.scalar(select(func.count(AnalyticsMappingVersion.id))) == 0


def test_invalid_second_decision_does_not_partially_apply(migrated):
    first, second = seed(migrated), seed(migrated, unit="unknown")
    review = confirm(migrated, first)
    review.decisions += confirm(migrated, second).decisions
    with pytest.raises(ValueError, match="Unsupported"):
        apply_review(migrated, review)
    assert migrated.scalar(select(func.count(AnalyticsProduct.id))) == 0
    assert migrated.scalar(select(func.count(AnalyticsMappingVersion.id))) == 0
