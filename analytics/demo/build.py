"""Rebuild public data using only the standard library: python -m analytics.demo.build."""

import json
import random
from pathlib import Path

SEED = 20261027
TARGET = Path(__file__).resolve().parents[2] / "docs/assets/case/data.json"


def dataset():
    rng = random.Random(SEED)
    rows = []
    for month in (7, 8, 9):
        for day, store in ((1, "synthetic-a"), (5, "synthetic-a"),
                           (10, "synthetic-b"), (15, "synthetic-b")):
            for product, unit in (("demo-milk-1l", "l"), ("demo-rice-1kg", "kg")):
                price = 50 if month < 9 else 65 if unit == "l" else 45
                quantity = 2 if month < 9 else 3
                rows.append(dict(month=month, day=day, store=store, product=product,
                                 quantity=quantity, price=price, amount=quantity*price,
                                 unit=unit, mapped=True, operation="ПРИХОД",
                                 name=f"Synthetic {'молоко 1 л' if unit == 'l' else 'рис 1 кг'}"))
        for day, product, amount in ((20, "demo-exit-piece" if month < 9 else
                                     "demo-entry-piece", 30 if month < 9 else 70),
                                     (21, "unmatched", 20), (22, "unknown", 10)):
            rows.append(dict(month=month, day=day, store="synthetic-b" if day == 21
                             else "synthetic-a", product=product, quantity=1, price=amount,
                             amount=amount, unit="piece", mapped=day == 20,
                             operation=None if day == 22 else "ПРИХОД",
                             name="Synthetic смесь ?" if day == 21 else f"Synthetic {product}"))
    # Missing data is outside the illustrated pair: a separate insufficient-data scenario.
    rows.append(dict(month=7, day=25, store="synthetic-b", product="missing",
                     quantity=None, price=None, amount=None, unit=None, mapped=False,
                     operation="ПРИХОД", name="Synthetic упаковка неизвестна"))
    rng.shuffle(rows)
    for index, row in enumerate(rows, 1):
        row["id"] = index
        row["date"] = f"2026-{row['month']:02}-{row['day']:02}"
    return {"synthetic": True, "seed": SEED, "rows": rows}


def main():
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(dataset(), ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
