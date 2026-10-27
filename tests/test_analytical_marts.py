"""Actual production SQL, isolated PostgreSQL schemas, synthetic sources only."""

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app import analytics_cli
from app.config import get_settings
from app.models import Receipt, ReceiptItem, SyncRun
from app.services.analytical_identity import (
    Product,
    apply_review,
    export_review,
)
from app.services.analytical_marts import calculate, sql_file

CUTOFF = datetime(2026, 10, 26, tzinfo=UTC)


@pytest.fixture
def pg_marts(monkeypatch):
    dsn = os.environ.get("TRATTE_AUDIT_TEST_DSN")
    if not dsn:
        pytest.skip("TRATTE_AUDIT_TEST_DSN required: disposable PostgreSQL only")
    schema = "marts_test_" + uuid4().hex
    url = dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = create_engine(url)
    try:
        command.upgrade(Config("alembic.ini"), "head")
        yield engine, schema
    finally:
        engine.dispose()
        get_settings.cache_clear()
        with admin.begin() as connection:
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()


def add(session, when="2026-08-05T00:00:00+00:00", amount="100", qty="2", **extra):
    receipt = Receipt(
        gmail_message_id=uuid4().hex, purchased_at=datetime.fromisoformat(when) if when else None,
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
        updated_at=datetime(2026, 10, 1, tzinfo=UTC), status="parsed",
        operation_type="ПРИХОД", currency="RUB", total=Decimal("100"), store="synthetic-a",
        source_subject="PRIVATE SUBJECT", location="PRIVATE ADDRESS", inn="PRIVATE INN",
    )
    for key, value in extra.items():
        setattr(receipt, key, value)
    item = ReceiptItem(
        receipt=receipt, original_name="PRIVATE NAME", normalized_name="PRIVATE NAME",
        quantity=Decimal(qty), unit="шт.", unit_price=Decimal("50"),
        total=Decimal(amount), category="synthetic",
    )
    session.add(receipt)
    session.flush()
    return item


def mapping(session, items, *, basis="package_count", unit="l", content="930", source_unit="ml"):
    review = export_review(session, [i.id for i in items], "synthetic")
    review.products = [Product(
        id="synthetic-milk-930ml", description="Synthetic milk", brand="Synthetic",
        variant="plain", packaging="bottle", comparison_unit=unit,
        content_quantity=Decimal(content) if content else None,
        content_unit=source_unit,
    )]
    for decision in review.decisions:
        decision.status = "confirmed"
        decision.product_id = review.products[0].id
        decision.quantity_basis = basis
    return apply_review(session, review)


def report(engine, **kwargs):
    return json.loads(calculate(engine, as_of=CUTOFF, **kwargs), parse_float=Decimal)


def test_postgres_grains_coverage_price_timezone_and_privacy(pg_marts):
    engine, _ = pg_marts
    with Session(engine) as session:
        a = add(session, "2026-07-31T21:00:00+00:00")  # August in Moscow
        b = add(session)
        c = add(session, "2026-08-31T21:00:00+00:00", amount="120")
        d = add(session, "2026-09-05T00:00:00+00:00", amount="120", total=130)
        version = mapping(session, [a, b, c, d])
        # A second item doubles item amount, but never receipt total or observations.
        session.add(ReceiptItem(
            receipt=a.receipt, original_name="PRIVATE EXTRA", normalized_name="PRIVATE EXTRA",
            quantity=1, unit="шт.", unit_price=50, total=50, category="synthetic",
        ))
        add(session, "2026-09-30T21:00:00+00:00")  # October, excluded
        add(session, None)
        add(session, "2026-10-27T00:00:00+00:00")
        session.add(SyncRun(started_at=CUTOFF, finished_at=CUTOFF, status="completed"))
        session.commit()
    result = report(engine, mapping_version=version)
    assert [r["recorded_receipt_amount"] for r in result["receipt_monthly"]] == [200, 230]
    assert [r["receipt_count"] for r in result["receipt_monthly"]] == [2, 2]
    assert result["overview"]["missing_purchase_dates"] == 1
    assert result["overview"]["future_purchase_dates"] == 1
    august, september = result["coverage"]
    assert august["confirmed_comparable_amount"] == 200
    assert august["positive_rub_item_amount"] == 250
    assert august["confirmed_coverage_of_positive_rub"] == Decimal("0.8")
    assert september["confirmed_comparable_amount"] == 240
    assert september["comparison_status"] == "available_partial"
    product = result["product_monthly"][0]
    assert product["receipt_observations"] == 2
    assert product["comparable_quantity"] == Decimal("3.72")
    assert abs(product["paid_unit_price"] - Decimal(200) / Decimal("3.72")) < Decimal("1e-14")
    assert product["passes_pair_threshold"] is True
    assert [r["receipt_minus_items"] for r in result["reconciliation"]] == [-50, 0, -20, 10]
    assert result["quality"][1]["arithmetic_mismatches"] == 2
    assert result["freshness"]["last_completed_sync"] is not None
    assert "PRIVATE" not in json.dumps(result, default=str)
    assert result["metadata"]["mapping_version"] == version
    assert len(result["sql_sha256"]["comparison"]) == 64


def test_postgres_empty_months_and_missing_denominators(pg_marts):
    engine, _ = pg_marts
    result = report(engine)
    assert result["receipt_monthly"] == []
    assert result["reconciliation"] == []
    assert result["items"] == []
    assert result["freshness"]["last_completed_sync"] is None
    for month in result["coverage"]:
        assert month["confirmed_comparable_amount"] == 0
        assert month["confirmed_coverage_of_positive_rub"] is None
        assert month["comparison_status"] == "insufficient_data"


@pytest.mark.parametrize("basis,unit,content,content_unit,source_unit,qty,expected", [
    ("source_weight", "kg", "500", "g", "кг", "0.25", "0.25"),
    ("source_weight", "kg", None, None, "г", "250", "0.25"),
    ("source_volume", "l", None, None, "мл", "500", "0.5"),
    ("piece_count", "piece", None, None, "шт.", "3", "3"),
])
def test_postgres_mapped_quantity_units(
    pg_marts, basis, unit, content, content_unit, source_unit, qty, expected,
):
    engine, _ = pg_marts
    with Session(engine) as session:
        item = add(session, qty=qty)
        item.unit = source_unit
        version = mapping(session, [item], basis=basis, unit=unit,
                          content=content, source_unit=content_unit)
        session.commit()
    result = report(engine, mapping_version=version)
    row = result["items"][0]
    assert row["comparable_quantity"] == Decimal(expected)
    assert row["comparison_unit"] == unit
    assert row["reason"] == "not_observed_in_both_periods"
    assert abs(row["paid_unit_price"] - Decimal(100) / Decimal(expected)) < Decimal("1e-14")


def test_postgres_repeated_rows_not_receipts_and_unresolved_mapping(pg_marts):
    engine, _ = pg_marts
    with Session(engine) as session:
        a = add(session)
        b = add(session, "2026-09-05T00:00:00+00:00")
        duplicate = ReceiptItem(
            receipt=a.receipt, original_name="Synthetic", normalized_name="Synthetic",
            quantity=2, unit="шт.", unit_price=50, total=100, category="synthetic",
        )
        session.add(duplicate)
        session.flush()
        version = mapping(session, [a, b, duplicate])
        session.commit()
        unresolved = export_review(session, [a.id], "unresolved synthetic")
        unresolved_version = apply_review(session, unresolved)
        session.commit()
    result = report(engine, mapping_version=version)
    assert result["product_monthly"][0]["item_rows"] == 2
    assert result["product_monthly"][0]["receipt_observations"] == 1
    assert all(r["reason"] == "too_few_receipts" for r in result["items"])
    assert all(c["confirmed_comparable_amount"] == 0 for c in result["coverage"])
    result = report(engine, mapping_version=unresolved_version)
    assert {r["reason"] for r in result["items"]} == {
        "identity_unresolved", "identity_unmatched",
    }


def test_postgres_exclusion_reasons_currencies_zero_returns_and_scope(pg_marts):
    engine, _ = pg_marts
    with Session(engine) as session:
        items = [add(session), add(session, store="synthetic-b"),
                 add(session, "2026-09-05T00:00:00+00:00"),
                 add(session, "2026-09-06T00:00:00+00:00", store="synthetic-b")]
        version = mapping(session, items)
        add(session, operation_type=None)
        add(session, operation_type=" ВОЗВРАТ ПРИХОДА ", amount="-10")
        add(session, operation_type="other")
        add(session, currency="EUR")
        add(session, total=0)
        add(session, qty="0")
        add(session, amount="0")
        add(session, status="failed")
        add(session, created_at=datetime(2026, 10, 27, tzinfo=UTC))
        session.commit()
    result = report(engine, mapping_version=version)
    assert {r["reason"] for r in result["reasons"]} == {
        "comparable", "operation_unknown", "operation_purchase_return", "operation_other",
        "currency_not_rub", "invalid_receipt_total", "invalid_item_values",
    }
    assert len(result["items"]) == 11
    eur = next(r for r in result["receipt_monthly"] if r["currency"] == "EUR")
    assert eur["recorded_receipt_amount"] == 100
    assert result["coverage"][0]["positive_rub_item_amount"] == 600
    assert result["coverage"][0]["eligible_purchase_item_amount"] == 300
    filtered = report(engine, mapping_version=version, store="synthetic-a")
    assert all(c["confirmed_comparable_amount"] == 0 for c in filtered["coverage"])
    assert "too_few_receipts" in {r["reason"] for r in filtered["reasons"]}
    assert not any(p["passes_pair_threshold"] for p in filtered["product_monthly"])


@pytest.mark.parametrize("decomposition", [False, True])
def test_postgres_validation_fails_closed_and_read_only_snapshot(
    pg_marts, monkeypatch, decomposition,
):
    engine, _ = pg_marts
    with Session(engine) as session:
        item = add(session)
        version = mapping(session, [item])
        session.commit()
        with pytest.raises(ValueError, match="Unknown"):
            report(engine, decomposition=decomposition, mapping_version="0" * 64)
        item.total = 101
        session.commit()
    with pytest.raises(ValueError, match="Stale"):
        report(engine, decomposition=decomposition, mapping_version=version)
    from app.services import analytical_marts

    def validate(session, version):
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
        assert session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        # A concurrent source change after the first snapshot read remains invisible.
        before = session.scalar(text("SELECT count(*) FROM receipts"))
        with Session(engine) as writer:
            add(writer)
            writer.commit()
        assert session.scalar(text("SELECT count(*) FROM receipts")) == before

    monkeypatch.setattr(analytical_marts, "validate_version", validate)
    result = report(engine, decomposition=decomposition, mapping_version=version)
    assert sum(r["receipt_count"] for r in result["receipt_monthly"]) == 1


def test_postgres_reinstall_rollback_permissions_and_null_reconciliation(pg_marts):
    engine, schema = pg_marts
    role = "marts_reader_" + uuid4().hex
    with Session(engine) as session:
        item = add(session)
        receipt_id = item.receipt_id
        session.delete(item)
        null_receipt = add(session, total=None)
        null_receipt.receipt.total = None  # Explicit NULL, not a default.
        session.commit()
    rows = report(engine)["reconciliation"]
    assert rows[0]["receipt_minus_items"] is None
    assert rows[0]["reconciliation_status"] == "no_items"
    assert rows[1]["receipt_minus_items"] is None
    assert rows[1]["reconciliation_status"] == "missing_receipt_total"
    with engine.begin() as connection:
        connection.execute(text(sql_file("views_v1.sql")))
        connection.execute(text(f"CREATE ROLE {role} NOLOGIN"))
        connection.execute(text(f"GRANT USAGE ON SCHEMA {schema} TO {role}"))
        connection.execute(text(f"GRANT SELECT ON analytics_receipts_v1, analytics_items_v1, "
                                f"analytics_reconciliation_v1, analytics_quality_v1 TO {role}"))
    try:
        with engine.begin() as connection:
            connection.execute(text(f"SET LOCAL ROLE {role}"))
            assert connection.scalar(text(
                "SELECT min(receipt_id) FROM analytics_receipts_v1"
            )) == (
                receipt_id
            )
            assert connection.scalar(text("SELECT count(*) FROM analytics_items_v1")) == 1
            assert connection.scalar(text("SELECT receipt_count FROM analytics_quality_v1")) == 2
            for table in ("receipts", "receipt_items", "gmail_integrations",
                          "analytics_item_mappings", "analytics_mapping_versions"):
                assert connection.scalar(text(
                    "SELECT has_table_privilege(current_user, :table, 'SELECT')"
                ), {"table": table}) is False
            assert connection.scalar(text(
                "SELECT has_table_privilege(current_user, 'analytics_receipts_v1', 'UPDATE')"
            )) is False
        command.downgrade(Config("alembic.ini"), "0003_analytical_identity")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM receipts")) == 2
        command.upgrade(Config("alembic.ini"), "head")
        assert report(engine)["overview"]["observed_receipts"] == 2
    finally:
        with engine.begin() as connection:
            connection.execute(text(f"DROP OWNED BY {role}"))
            connection.execute(text(f"DROP ROLE {role}"))


def test_postgres_null_item_arithmetic_and_reconciliation_remain_unknown(pg_marts):
    engine, _ = pg_marts
    with Session(engine) as session:
        add(session)
        session.commit()
    # Current application schema prevents null item totals. Exercise the SQL's
    # missingness contract explicitly in this disposable schema as well.
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE receipt_items ALTER COLUMN total DROP NOT NULL"))
        connection.execute(text("UPDATE receipt_items SET total = NULL"))
    result = report(engine)
    assert result["reconciliation"][0]["reconciliation_status"] == "missing_item_totals"
    assert result["reconciliation"][0]["receipt_minus_items"] is None
    assert result["quality"][0]["unknown_arithmetic"] == 1
    assert result["quality"][0]["arithmetic_mismatches"] == 0
    assert result["items"][0]["reason"] == "invalid_item_values"


def test_marts_cli_private_output_parameters_and_no_overwrite(pg_marts, monkeypatch, tmp_path):
    engine, _ = pg_marts
    monkeypatch.setattr(analytics_cli, "engine", engine)
    runner = CliRunner()
    output = tmp_path / "private.json"
    args = ["marts", str(output), "--as-of", CUTOFF.isoformat()]
    result = runner.invoke(analytics_cli.cli, args)
    assert result.exit_code == 0, result.output
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text())["metadata"]["mapping_version"] is None
    content = output.read_bytes()
    assert runner.invoke(analytics_cli.cli, args).exit_code != 0
    assert output.read_bytes() == content
    for suffix in [
        ["--as-of", "2026-10-26"],
        ["--current-month", "2026-10-01"],
        ["--mapping-version", "0" * 64],
    ]:
        bad = tmp_path / "bad.json"
        result = runner.invoke(analytics_cli.cli, ["marts", str(bad), *args[2:], *suffix])
        assert result.exit_code != 0
        assert not bad.exists()
