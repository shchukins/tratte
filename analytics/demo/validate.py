"""Independent, hand-calculated oracle for the public fixture; no production imports."""

import json
from pathlib import Path

EXPECTED = json.loads(Path(__file__).with_name("expected.json").read_text())


def check_report(report):
    """Reject any SQL result that disagrees with the frozen arithmetic examples."""
    checks = {
        "totals": ([r["recorded_receipt_amount"] for r in report["receipt_monthly"]
                    if r["operation"] == "purchase" and r["currency"] == "RUB"],
                   EXPECTED["receipt_totals"]),
        "delta": (report["target_delta"], EXPECTED["delta"]),
        "comparable": ([r["matched_amount"] for r in report["coverage"]],
                       EXPECTED["comparable_amount"]),
        "positive": ([r["positive_item_amount"] for r in report["coverage"]],
                     EXPECTED["positive_amount"]),
        "unmatched": ([r["unmatched"] for r in report["coverage"]], EXPECTED["unmatched_amount"]),
        "excluded": ([r["recorded_receipt_amount"] for r in report["receipt_monthly"]
                      if r["operation"] == "unknown"], EXPECTED["excluded_amount"]),
    }
    for row in report["bridge"]:
        key = {"current_only": "entry", "base_only": "exit"}.get(row["component"],
                                                                  row["component"])
        checks[f"bridge/{key}"] = row["amount_rounded"], EXPECTED["bridge"][key]
    for key, product in (("milk", "demo-milk-1l"), ("rice", "demo-rice-1kg")):
        row = next(r for r in report["products"] if r["product_id"] == product)
        for metric, fields in (("amount", ("s0", "s1")), ("price", ("p0", "p1")),
                               ("quantity", ("q0", "q1"))):
            checks[f"{key}/{metric}"] = [row[f] for f in fields], EXPECTED[key][metric]
        for metric in ("price_effect", "quantity_effect"):
            checks[f"{key}/{metric}"] = row[metric], EXPECTED[key][metric]
        details = [r for r in report["details"] if r["product_id"] == product]
        checks[f"{key}/observations"] = len(details), EXPECTED["observations_per_product"]
    for name, (actual, expected) in checks.items():
        if actual != expected:
            raise ValueError(f"Synthetic oracle mismatch: {name}: {actual} != {expected}")
