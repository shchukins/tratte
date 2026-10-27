"""Dashboard source gate, scope, actual BI dataset SQL and sanitized export graph."""

import json
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from test_analytical_marts import CUTOFF, add, mapping
from test_analytical_marts import pg_marts as pg_marts

from analytics.superset.bundle import build, configs, dataset_sql, uid
from app.services.analytical_dashboard import publish
from app.services.analytical_marts import calculate


def setup_snapshot(engine):
    with engine.begin() as connection:
        # Each test owns its disposable source schema; shadow the dedicated BI schema
        # SQL here so parallel tests/production never share the snapshot table.
        ddl = Path("analytics/superset/snapshot.sql").read_text()
        schema = connection.scalar(text("SELECT current_schema()"))
        connection.execute(text(ddl.replace("tratte_bi", schema)))


def rows(engine, name):
    with engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
        snapshot_id = connection.scalar(text(
            "SELECT snapshot_id FROM snapshot_v1 ORDER BY published_at DESC LIMIT 1"))
        return list(connection.execute(text(dataset_sql(snapshot_id)[name].replace(
            "tratte_bi.snapshot_v1", f"{schema}.snapshot_v1"))).mappings())


def publication(engine, **kwargs):
    # Keep production publish SQL intact by translating schema via search_path? SQL
    # is explicitly schema-qualified, so use a connection proxy scoped to the test.
    from unittest.mock import patch

    import app.services.analytical_dashboard as module
    with engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    original = module.text
    with patch.object(module, "text", lambda sql: original(sql.replace(
        "tratte_bi.snapshot_v1", f"{schema}.snapshot_v1"))):
        return publish(engine, engine, as_of=CUTOFF, **kwargs)


@pytest.mark.parametrize("scope", [{}, {"store": "synthetic-a"}, {"category": "synthetic"},
                                  {"store": "none"}, {"category": "none"}])
def test_dashboard_scope_all_datasets_and_numeric_totals(pg_marts, scope):
    engine, _ = pg_marts
    with Session(engine) as session:
        items = [add(session, "2026-07-31T21:00:00+00:00"), add(session, store="synthetic-b"),
                 add(session, "2026-08-31T21:00:00+00:00", amount="120"),
                 add(session, "2026-09-05T00:00:00+00:00", store="synthetic-b", amount="120")]
        # Outside pair (October in Moscow) must never appear in detail/prices.
        outside = add(session, "2026-09-30T21:00:00+00:00")
        version = mapping(session, items)
        session.commit()
        outside_id = outside.id
    setup_snapshot(engine)
    snapshot_id = publication(engine, mapping_version=version, **scope)
    result = json.loads(calculate(engine, as_of=CUTOFF, dashboard=True,
                                 mapping_version=version, **scope), parse_float=Decimal)
    assert result["dashboard_contract"] == "expense_dashboard_v1"
    assert "PRIVATE" not in json.dumps(result, default=str)
    for name in dataset_sql():
        data = rows(engine, name)  # Actual SQL imported by Superset, not a mirror.
        assert all(r["snapshot_id"] == snapshot_id for r in data)
    details = rows(engine, "details")
    assert all(r["item_id"] != outside_id for r in details)
    if scope.get("store"):
        assert all(r["store"] == scope["store"] for r in details)
    if scope.get("category"):
        assert all(r["category"] == scope["category"] for r in details)
    totals = rows(engine, "totals")
    bridge = rows(engine, "bridge")
    assert len(totals) == 2
    assert bridge[0]["running_known_total"] == totals[0]["known_total"]
    assert (next(r for r in bridge if r["step"]==7)["running_known_total"]
            == totals[1]["known_total"])
    if scope in ({"store": "none"}, {"category": "none"}):
        assert result["comparison_status"] == "insufficient_data"
        assert all(r["delta_ratio"] is None for r in totals)
        assert all(r["matched_coverage_of_positive_items"] is None
                   for r in rows(engine, "coverage"))
    if scope == {"store": "synthetic-a"}:
        assert all(r["reason"]=="too_few_receipts" for r in rows(engine,"products"))
        assert rows(engine,"observations") == []  # Store applied BEFORE pair threshold.
    if not scope:
        assert len(rows(engine,"observations")) == 4
        assert len(rows(engine,"medians")) == 4
        assert [r["delta"] for r in totals] == [0,0]  # Receipt scope, not item delta 40.
        assert next(r for r in bridge if r["step"]==6)["amount"] == -40


def test_stale_mapping_preserves_published_snapshot_and_detail_gate(pg_marts):
    engine, _ = pg_marts
    with Session(engine) as session:
        item = add(session)
        version = mapping(session, [item])
        session.commit()
    setup_snapshot(engine)
    first = publication(engine, mapping_version=version)
    with engine.begin() as connection:
        connection.execute(text("UPDATE receipt_items SET quantity=quantity+1"))
    with pytest.raises(ValueError, match="Stale"):
        publication(engine, mapping_version=version)
    assert rows(engine, "context")[0]["snapshot_id"] == first
    publication(engine)
    with engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
        frozen = connection.execute(text(dataset_sql(first)["context"].replace(
            "tratte_bi.snapshot_v1", f"{schema}.snapshot_v1"))).mappings().one()
    assert frozen["snapshot_id"] == first  # New publication never changes an existing binding.
    assert rows(engine,"observations") == []
    assert rows(engine,"context")[0]["comparison_status"] == "insufficient_data"


def test_export_safe_graph_and_import_shapes(tmp_path):
    path = tmp_path / "dashboard.zip"
    build(path)
    objects = configs()
    with ZipFile(path) as archive:
        assert len(archive.namelist()) == len(objects)
        for name, obj in objects.items():
            assert json.loads(archive.read("tratte/"+name)) == obj
    datasets = {d["uuid"] for n,d in objects.items() if n.startswith("datasets/")}
    charts = {d["uuid"] for n,d in objects.items() if n.startswith("charts/")}
    for name,obj in objects.items():
        if name.startswith("charts/"):
            assert obj["dataset_uuid"] in datasets
            assert isinstance(obj["params"],dict)
        if name.startswith("dashboards/"):
            for node in obj["position"].values():
                if isinstance(node,dict) and node.get("type")=="CHART":
                    assert node["meta"]["uuid"] in charts
    database = objects["databases/tratte_bi.yaml"]
    assert database["uuid"] == uid("database")
    assert "CHANGE_ME" in database["sqlalchemy_uri"]
    assert all(not database[key] for key in ("allow_dml","allow_ctas","allow_cvas",
                                           "expose_in_sqllab","allow_file_upload"))
    exported = json.dumps(objects)
    for private in ("gmail_message_id", "original_name", "normalized_name", "source_subject",
                    "access_token", "refresh_token", "fiscal", "PRIVATE"):
        assert private not in exported


def test_missing_amounts_null_complete_total_and_percentage(pg_marts):
    engine, _ = pg_marts
    with Session(engine) as session:
        add(session, total=None)
        add(session,"2026-09-05T00:00:00+00:00")
        session.commit()
    setup_snapshot(engine)
    publication(engine)
    data = rows(engine,"totals")
    assert data[0]["total"] is None
    assert data[0]["missing"] == 1
    assert all(r["delta"] is None and r["delta_ratio"] is None for r in data)
    assert rows(engine,"context")[0]["bridge_status"] == "insufficient_data"


def test_snapshot_binding_fail_closed_and_rejects_sql_injection():
    assert all("snapshot_id='REPLACE_WITH_SNAPSHOT_ID'" in sql for sql in dataset_sql().values())
    with pytest.raises(ValueError):
        dataset_sql("' OR true --")


def test_price_median_distinct_from_weighted_factor_price(pg_marts):
    engine, _ = pg_marts
    with Session(engine) as session:
        items = [add(session, amount="10", qty="1"), add(session, amount="90", qty="3"),
                 add(session, "2026-09-05T00:00:00+00:00", amount="20", qty="1"),
                 add(session, "2026-09-06T00:00:00+00:00", amount="120", qty="3")]
        version = mapping(session, items, basis="piece_count", unit="piece",
                          content=None, source_unit=None)
        session.commit()
    setup_snapshot(engine)
    publication(engine, mapping_version=version)
    assert [r["median_paid_unit_price"] for r in rows(engine,"medians")] == [20,30]
    product = rows(engine,"products")[0]
    assert product["p0"] == 25 and product["p1"] == 35
    assert len(rows(engine,"observations")) == 4


def test_checked_in_native_export_bindings_and_privacy():
    with ZipFile("analytics/superset/tratte-expenses.zip") as archive:
        objects = {name: json.loads(archive.read(name)) for name in archive.namelist()}
    for kind,count in (("databases",1),("datasets",11),("charts",15),("dashboards",2)):
        assert sum(f"/{kind}/" in name for name in objects) == count
    expected = {obj["uuid"] for name,obj in configs().items() if name!="metadata.yaml"}
    assert {obj["uuid"] for name,obj in objects.items()
            if not name.endswith("metadata.yaml")} == expected
    for name,obj in objects.items():
        if "/datasets/" in name:
            assert "snapshot_id='" in obj["sql"]
            assert "REPLACE_WITH_SNAPSHOT_ID" not in obj["sql"]
    serialized = json.dumps(objects)
    for term in ("password", "encrypted_extra", "@gmail.com", "original_name", "source_subject",
                 "tratte-issue6-postgres", "synthetic-demo@"):
        assert term not in serialized
