"""The public example must reproduce its independent oracle through real SQL."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from test_analytical_marts import pg_marts as pg_marts

from analytics.demo.build import TARGET, dataset
from analytics.demo.load import seed
from analytics.demo.validate import EXPECTED, check_report
from analytics.superset.demo import AS_OF
from app.services.analytical_marts import calculate


def test_public_fixture_is_reproducible_and_explicitly_synthetic():
    data = dataset()
    assert json.loads(TARGET.read_text()) == data
    assert data["synthetic"] is True
    assert {r["month"] for r in data["rows"]} == {7, 8, 9}
    assert all(r["name"].startswith("Synthetic ") for r in data["rows"])
    assert {r["store"] for r in data["rows"]} == {"synthetic-a", "synthetic-b"}
    assert sum(r["amount"] is None for r in data["rows"]) == 1
    for forbidden in ("gmail_message_id", "source_subject", "fiscal", "token", "@"):
        assert forbidden not in TARGET.read_text()


def test_public_fixture_matches_oracle_through_production_sql(pg_marts):
    engine, _ = pg_marts
    version = seed(engine)
    report = json.loads(calculate(engine, as_of=AS_OF, mapping_version=version, dashboard=True))
    check_report(report)
    for component in EXPECTED["bridge"]:
        key = {"entry": "current_only", "exit": "base_only"}.get(component, component)
        broken = deepcopy(report)
        broken["bridge"] = [r for r in broken["bridge"] if r["component"] != key]
        with pytest.raises(ValueError, match="oracle mismatch"):
            check_report(broken)
    duplicated = deepcopy(report)
    duplicated["bridge"].append(deepcopy(report["bridge"][0]))
    with pytest.raises(ValueError, match="oracle mismatch"):
        check_report(duplicated)
    milk = next(r for r in report["products"] if r["product_id"] == "demo-milk-1l")
    milk["price_effect"] += 1
    with pytest.raises(ValueError, match="oracle mismatch"):
        check_report(report)
    incomplete = json.loads(calculate(engine, as_of=AS_OF, mapping_version=version,
        base_month="2026-07-01", current_month="2026-08-01", dashboard=True))
    assert incomplete["bridge_status"] == "insufficient_data"
    assert incomplete["target_delta"] is None


def test_public_assets_are_local_and_captioned():
    page = Path("docs/case.html").read_text()
    for name in ("superset-bridge.png", "superset-detail.png", "superset-quality.png",
                 "walkthrough.webm", "walkthrough.vtt", "demo-poster.png"):
        assert f"assets/case/{name}" in page
        assert Path("docs/assets/case", name).is_file()
    assert 'kind="captions"' in page
    assert "синтетические" in page
