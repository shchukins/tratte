"""Synthetic source for the isolated demo. Refuses a database with ANY existing tables."""

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.models import Receipt, ReceiptItem
from app.services.analytical_dashboard import publish
from app.services.analytical_identity import Product, apply_review, export_review

AS_OF = datetime(2026, 10, 27, tzinfo=UTC)


def seed(engine):
    with Session(engine) as session:
        groups = {}
        for month in (8, 9):
            for day, store in ((1, "synthetic-a"), (5, "synthetic-a"),
                               (10, "synthetic-b"), (15, "synthetic-b")):
                for product in ("demo-milk-1l", "demo-rice-1kg"):
                    price = Decimal(50 if month == 8 else 65 if "milk" in product else 45)
                    quantity = Decimal(2 if month == 8 else 3)
                    item = observation(session, month, day, store, product, quantity, price)
                    groups.setdefault(product, []).append(item)
            product = "demo-exit-piece" if month == 8 else "demo-entry-piece"
            groups[product] = [observation(session, month, 20, "synthetic-a", product,
                                          Decimal(1), Decimal(30 if month==8 else 70))]
            observation(session, month, 21, "synthetic-b", "unmatched", Decimal(1), Decimal(20))
            excluded = observation(session, month, 22, "synthetic-a", "unknown", Decimal(1),
                                   Decimal(10))
            excluded.receipt.operation_type = None
        review = export_review(session, [i.id for rows in groups.values() for i in rows],
                               "Explicit synthetic demo, no real product identities")
        review.products = [Product(id=key, description=key, brand="Synthetic", variant="demo",
            packaging="demo", comparison_unit="piece") for key in groups]
        item_product = {item.id: key for key, rows in groups.items() for item in rows}
        for decision in review.decisions:
            decision.status = "confirmed"
            decision.product_id = item_product[decision.item_id]
            decision.quantity_basis = "piece_count"
        version = apply_review(session, review)
        session.commit()
    return version


def observation(session, month, day, store, product, quantity, price):
    receipt = Receipt(gmail_message_id=f"synthetic-{month}-{day}-{product}",
        purchased_at=datetime(2026, month, day, 9, tzinfo=UTC),
        created_at=AS_OF, updated_at=AS_OF, store=store, status="parsed",
        currency="RUB", operation_type="ПРИХОД", total=quantity*price)
    item = ReceiptItem(receipt=receipt, original_name="Synthetic demo", normalized_name="Synthetic",
        category="demo-food" if "milk" in product else "demo-other",
        quantity=quantity, unit="шт.", unit_price=price, total=quantity*price)
    session.add(item)
    session.flush()
    return item


def main():
    url = os.environ["TRATTE_DEMO_DATABASE_URL"]
    engine = create_engine(url)
    if inspect(engine).get_table_names():
        raise ValueError("Demo requires a new empty DISPOSABLE database")
    os.environ["DATABASE_URL"] = url
    from app.config import get_settings
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    version = seed(engine)
    with engine.begin() as connection:
        connection.execute(text((Path(__file__).parent /
                                 "snapshot.sql").read_text()))
    snapshot_id = publish(engine, engine, mapping_version=version, as_of=AS_OF)
    print(f"Synthetic demo published: {snapshot_id}")


if __name__ == "__main__":
    main()
