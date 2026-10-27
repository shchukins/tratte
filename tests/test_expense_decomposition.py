"""Synthetic accounting identities executed by the production SQL consumer."""

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from test_analytical_marts import (
    CUTOFF,
    add,
    mapping,
    report,
)
from test_analytical_marts import (
    pg_marts as pg_marts,
)
from typer.testing import CliRunner

from app import analytics_cli


def decompose(engine, **kwargs):
    return report(engine, decomposition=True, **kwargs)


def assert_bridge(result, delta):
    assert result["bridge_status"] == "available"
    assert result["target_delta"] == Decimal(delta)
    assert sum(r["amount_rounded"] or 0 for r in result["bridge"]) == Decimal(delta)


@pytest.mark.parametrize("amount,qty,price,quantity,scenario", [
    ("100", "2", "0", "0", "200"),
    ("120", "2", "40", "0", "200"),
    ("200", "4", "0", "200", "400"),
    ("240", "4", "60", "220", "400"),
    ("101", "3", "-81.6666666666666667", "83.6666666666666667", "300"),
])
def test_symmetric_effects_scenario_and_package_quantities(
    pg_marts, amount, qty, price, quantity, scenario,
):
    engine, _ = pg_marts
    with Session(engine) as session:
        items = [add(session), add(session),
                 add(session, "2026-09-05T00:00:00+00:00", amount=amount, qty=qty),
                 add(session, "2026-09-06T00:00:00+00:00", amount=amount, qty=qty)]
        for item in items:
            item.receipt.total = item.total
        version = mapping(session, items)
        session.commit()
    result = decompose(engine, mapping_version=version)
    f = result["products"][0]
    assert f["comparison_unit"] == "l"
    assert f["q0"] == Decimal("3.72")
    assert abs(f["price_effect"] - Decimal(price)) < Decimal("1e-12")
    assert abs(f["quantity_effect"] - Decimal(quantity)) < Decimal("1e-12")
    assert abs(f["price_effect"] + f["quantity_effect"] - f["amount_delta"]) < Decimal("1e-12")
    assert abs(result["scenario"]["previous_price_cost"]-Decimal(scenario)) < Decimal("1e-12")
    assert_bridge(result, str(Decimal(amount)*2-200))
    assert result["metadata"]["methodology_version"] == "expense_decomposition_v1"
    assert len(result["sql_sha256"]["decomposition"]) == 64


@pytest.mark.parametrize("month,sign,reason", [
    ("2026-08-05T00:00:00+00:00", -1, "base_only"),
    ("2026-09-05T00:00:00+00:00", 1, "current_only"),
])
def test_observed_entry_exit_and_unmatched_residual(pg_marts, month, sign, reason):
    engine, _ = pg_marts
    with Session(engine) as session:
        item = add(session, month, amount="75")
        version = mapping(session, [item])
        add(session, month, amount="10")  # Unmapped: never invent a price.
        add(session, month, amount="-5", operation_type="ВОЗВРАТ ПРИХОДА")
        add(session, month, currency="EUR")
        session.commit()
    result = decompose(engine, mapping_version=version)
    assert result["products"][0]["reason"] == reason
    amounts = {r["component"]: r["amount"] for r in result["bridge"]}
    assert amounts[reason] == sign*75
    assert amounts["unmatched"] == sign*10
    assert amounts["reconciliation"] == sign*115
    assert_bridge(result, str(sign*200))
    assert result["scenario"]["previous_price_cost"] is None
    assert result["comparison_status"] == "insufficient_data"
    assert any(r["operation"] == "purchase_return" for r in result["reasons"])


def test_filters_recompute_thresholds_category_conflict_and_empty(pg_marts):
    engine, _ = pg_marts
    empty = decompose(engine)
    assert_bridge(empty, "0")
    assert empty["comparison_status"] == "insufficient_data"
    assert empty["scenario"]["actual_same_part_cost"] is None
    assert all(r["matched_coverage_of_positive_items"] is None for r in empty["coverage"])
    with Session(engine) as session:
        items = [add(session), add(session, store="synthetic-b"),
                 add(session, "2026-09-05T00:00:00+00:00"),
                 add(session, "2026-09-06T00:00:00+00:00", store="synthetic-b")]
        version = mapping(session, items)
        session.commit()
    assert decompose(engine, mapping_version=version)["comparison_status"] == "available_partial"
    result = decompose(engine, mapping_version=version, store="synthetic-a")
    assert result["products"][0]["reason"] == "too_few_receipts"
    with Session(engine) as session:
        # Category changes invalidate the manifest; build a fresh explicit version.
        items = list(session.query(type(items[0])).order_by(type(items[0]).id))
        items[0].category = "other"
        version = mapping(session, items)
        session.commit()
    result = decompose(engine, mapping_version=version)
    assert result["products"][0]["reason"] == "category_conflict"
    result = decompose(engine, mapping_version=version, category="synthetic")
    assert result["products"][0]["reason"] == "too_few_receipts"
    assert result["metadata"]["bridge_target"] == "known_category_item_totals"
    assert_bridge(result, "100")
    assert next(r for r in result["bridge"] if r["component"]=="reconciliation")["amount"] is None


def test_missing_values_block_complete_bridge_and_stale_gate(pg_marts):
    engine, _ = pg_marts
    with Session(engine) as session:
        item = add(session, qty="0", total=None)
        item.receipt.total = None
        version = mapping(session, [item])
        session.commit()
    result = decompose(engine, mapping_version=version)
    assert result["bridge_status"] == "insufficient_data"
    assert result["target_delta"] is None
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE receipt_items ALTER COLUMN total DROP NOT NULL"))
        connection.execute(text("UPDATE receipt_items SET total=NULL"))
    with pytest.raises(ValueError, match="Stale"):
        decompose(engine, mapping_version=version)
    result = decompose(engine)
    assert result["bridge_status"] == "insufficient_data"
    assert result["scenario"]["previous_price_cost"] is None


def test_cli_private_export_and_no_overwrite(pg_marts, monkeypatch, tmp_path):
    engine, _ = pg_marts
    monkeypatch.setattr(analytics_cli, "engine", engine)
    output = tmp_path / "private.json"
    args = ["decompose", str(output), "--as-of", CUTOFF.isoformat(), "--category", "synthetic"]
    runner = CliRunner()
    result = runner.invoke(analytics_cli.cli, args)
    assert result.exit_code == 0, result.output
    assert output.stat().st_mode & 0o777 == 0o600
    before = output.read_bytes()
    assert runner.invoke(analytics_cli.cli, args).exit_code != 0
    assert output.read_bytes() == before


def test_half_cent_rounding_is_explicit_and_categories_reconcile(pg_marts):
    engine, _ = pg_marts
    with Session(engine) as session:
        items = [add(session, amount="0.01"), add(session, amount="0.01"),
                 add(session, "2026-09-05T00:00:00+00:00", amount="0.03", qty="3"),
                 add(session, "2026-09-06T00:00:00+00:00", amount="0.03", qty="3")]
        for item in items:
            item.receipt.total = item.total
        # Pieces give exact half cents; physical conversion is tested separately above.
        version = mapping(session, items, basis="piece_count", unit="piece",
                          content=None, source_unit=None)
        session.commit()
    result = decompose(engine, mapping_version=version)
    assert_bridge(result, "0.04")
    amounts = {r["component"]: r["amount_rounded"] for r in result["bridge"]}
    assert amounts["price"] == Decimal("0.03")
    assert amounts["quantity"] == Decimal("0.02")
    assert amounts["rounding"] == Decimal("-0.01")
    category = result["categories"][0]
    assert category["price_effect"] + category["quantity_effect"] == Decimal("0.04")
    assert category["unmatched_delta"] == 0
    assert result["scenario"]["current_coverage_of_positive_items"] == 1
