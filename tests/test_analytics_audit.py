"""Exercise the actual SQL against PostgreSQL using only synthetic CTE relations."""

import json
import os
import subprocess
from decimal import Decimal

import pytest

from analytics.run_audit import SQL_PATH, command, parameters, transaction

PARAMS = parameters("2026-10-25T20:00:00+00:00", "Europe/Moscow", None, None)


def test_month_boundaries_and_timezone():
    assert PARAMS["base_month"] == "2026-08-01"
    assert PARAMS["current_month"] == "2026-09-01"
    # In Moscow October has already started; September is complete.
    assert parameters("2026-09-30T21:00:00+00:00", "Europe/Moscow", None, None) == {
        **PARAMS, "as_of": "2026-09-30T21:00:00+00:00",
    }


@pytest.mark.parametrize("cutoff,base,current", [
    ("2026-10-25", None, None),
    (PARAMS["as_of"], "2026-08-02", "2026-09-01"),
    (PARAMS["as_of"], "2026-07-01", "2026-09-01"),
    (PARAMS["as_of"], "2026-09-01", "2026-10-01"),
])
def test_reject_invalid_or_incomplete_pair(cutoff, base, current):
    with pytest.raises(ValueError):
        parameters(cutoff, "Europe/Moscow", base, current)


def test_remote_command_quotes_arguments_and_transaction_is_read_only():
    cmd = command(PARAMS, host="fixture-host", container="pg", database="audit", user="audit")
    assert cmd[:6] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "fixture-host"]
    assert "ON_ERROR_STOP=1" in cmd[-1]
    sql = transaction("SELECT 1;")
    assert "REPEATABLE READ READ ONLY" in sql
    assert sql.endswith("ROLLBACK;\n")


def relation(name, rows, fields):
    value = json.dumps(rows, ensure_ascii=False).replace("'", "''")
    return f"{name} AS (SELECT * FROM jsonb_to_recordset('{value}'::jsonb) AS x({fields}))"


def execute_audit(receipts, items):
    sources = [
        relation("receipts", receipts,
                 "id int, status text, purchased_at timestamptz, operation_type text, "
                 "currency text, total numeric, created_at timestamptz, updated_at timestamptz"),
        relation("receipt_items", items,
                 "id int, receipt_id int, original_name text, normalized_name text, "
                 "quantity numeric, unit text, unit_price numeric, total numeric"),
        relation("sync_runs", [], "started_at timestamptz, finished_at timestamptz, status text"),
        relation("product_aliases", [], "id int"),
    ]
    sql = SQL_PATH.read_text().replace(
        "WITH parameters AS (", "WITH " + ",\n".join(sources) + ",\nparameters AS (", 1
    )
    dsn = os.environ.get("TRATTE_AUDIT_TEST_DSN")
    if dsn:
        import psycopg

        # Values are validated constants, not runtime user input.
        for key, value in PARAMS.items():
            sql = sql.replace(f":'{key}'", "'" + value.replace("'", "''") + "'")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '30s'")
            try:
                result = conn.execute(sql).fetchone()[0]
            finally:
                conn.execute("ROLLBACK")
        return json.loads(json.dumps(result), parse_float=Decimal)
    host = os.environ.get("TRATTE_AUDIT_TEST_SSH_HOST")
    if not host:
        pytest.skip("Set TRATTE_AUDIT_TEST_DSN or TRATTE_AUDIT_TEST_SSH_HOST for PostgreSQL")
    cmd = command(PARAMS, host=host,
                  container=os.environ.get("TRATTE_AUDIT_TEST_CONTAINER", "tratte-db-1"),
                  database="tratte", user="tratte")
    result = subprocess.run(cmd, input=transaction(sql), capture_output=True,
                            text=True, timeout=60, check=True)
    return json.loads(result.stdout, parse_float=Decimal)


def fixture_receipt(id_, date_, operation="ПРИХОД", amount=100, currency="RUB", **extra):
    return dict(id=id_, status="parsed", purchased_at=date_, operation_type=operation,
                currency=currency, total=amount, created_at="2026-10-01T00:00:00Z",
                updated_at="2026-10-01T00:00:00Z") | extra


def fixture_item(id_, receipt, name="synthetic milk 1 l", amount=100, unit="шт.", qty=1):
    return dict(id=id_, receipt_id=receipt, original_name=name, normalized_name=name,
                quantity=qty, unit=unit, unit_price=amount, total=amount)


def test_postgres_candidate_coverage_reasons_and_receipt_grain():
    receipts = [
        fixture_receipt(1, "2026-07-31T21:00:00Z", amount=200),  # August in Moscow
        fixture_receipt(2, "2026-08-20T09:00:00Z"),
        fixture_receipt(3, "2026-08-31T21:00:00Z"),  # September in Moscow
        fixture_receipt(4, "2026-09-20T09:00:00Z", amount=110),  # reconciliation +10
        fixture_receipt(5, "2026-08-10T09:00:00Z", operation=None),
        fixture_receipt(6, "2026-09-10T09:00:00Z", operation="Возврат прихода"),
        fixture_receipt(7, "2026-09-10T09:00:00Z", currency="EUR"),
        fixture_receipt(8, "2026-09-11T09:00:00Z", amount=0),
        fixture_receipt(9, "2026-09-12T09:00:00Z"),
        fixture_receipt(10, "2026-09-13T09:00:00Z"),
        fixture_receipt(11, "2026-09-14T09:00:00Z"),
        fixture_receipt(12, "2026-09-30T21:00:00Z"),  # October, outside pair
        fixture_receipt(13, "2026-09-15T09:00:00Z"),
        fixture_receipt(14, "2026-08-15T09:00:00Z"),
        fixture_receipt(15, None),
    ]
    items = [fixture_item(i, i) for i in range(1, 16)]
    items.append(fixture_item(16, 1))  # same receipt counts only once for threshold
    items[8] = fixture_item(9, 9, name="only September")
    items[9] = fixture_item(10, 10, unit="mystery")
    items[10] = fixture_item(11, 11, name="")
    items[12] = fixture_item(13, 13, name="rare")
    items[13] = fixture_item(14, 14, name="rare")
    result = execute_audit(receipts, items)
    august, september = result["pair_coverage"]
    assert august["repeated_candidate_amount"] == 300
    assert september["repeated_candidate_amount"] == 200
    assert august["positive_rub_item_amount"] == 500
    assert august["eligible_purchase_item_amount"] == 400
    assert august["candidate_coverage_of_positive_rub"] == Decimal("0.6")
    assert august["candidate_coverage_of_eligible_purchases"] == Decimal("0.75")
    assert august["confirmed_comparable_amount"] == 0
    assert result["overview"]["duplicate_item_extra_rows"] == 1
    assert result["overview"]["missing_purchase_date"] == 1
    assert september["item_rows"] == 9
    reasons = {r["reason"] for r in result["pair_reasons"]}
    assert reasons == {"identity_unverified", "operation_unknown", "operation_purchase_return",
                       "currency_not_rub", "invalid_receipt_total", "missing_name",
                       "unsupported_unit", "not_observed_in_both_periods", "too_few_receipts"}
    september_rub = next(r for r in result["monthly_receipts"]
                         if r["month"] == "2026-09-01" and r["currency"] == "RUB")
    assert september_rub["receipt_minus_items"] == -90  # +10 discrepancy, zero receipt -100
    assert september_rub["mismatched_receipts"] == 2
    assert not any("milk" in str(v) for v in result.values())


def test_postgres_empty_periods_preserve_missing_denominators():
    result = execute_audit([], [])
    assert len(result["pair_coverage"]) == 2
    for month in result["pair_coverage"]:
        assert month["positive_rub_item_amount"] == 0
        assert month["candidate_coverage_of_positive_rub"] is None
        assert month["confirmed_coverage_of_eligible_purchases"] is None
    assert result["pair_reasons"] == []
    assert result["monthly_receipts"] == []


def test_postgres_duplicate_rows_do_not_satisfy_receipt_threshold_and_invalid_values():
    receipts = [
        fixture_receipt(1, "2026-08-05T09:00:00Z", amount=200),
        fixture_receipt(2, "2026-09-05T09:00:00Z", amount=200),
        fixture_receipt(3, "2026-09-06T09:00:00Z"),
        fixture_receipt(4, "2026-09-07T09:00:00Z", created_at="2026-10-26T00:00:00Z"),
        fixture_receipt(5, "2026-09-08T09:00:00Z", status="failed"),
    ]
    items = [fixture_item(1, 1), fixture_item(2, 1), fixture_item(3, 2),
             fixture_item(4, 2), fixture_item(5, 3, qty=0),
             fixture_item(6, 4), fixture_item(7, 5)]
    result = execute_audit(receipts, items)
    assert result["overview"]["parsed_receipts"] == 3
    for month in result["pair_coverage"]:
        assert month["repeated_candidate_amount"] == 0
    assert {r["reason"] for r in result["pair_reasons"]} == {
        "too_few_receipts", "invalid_item_values",
    }
    assert result["units"][0]["zero_quantities"] == 1
