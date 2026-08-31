from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import ProcessingStatus, Receipt, ReceiptItem


@dataclass(frozen=True)
class PeriodStats:
    start: datetime
    end: datetime
    total: Decimal
    receipt_count: int
    average: Decimal
    stores: list[tuple[str, Decimal]]
    top_by_spend: list[tuple[str, Decimal]]
    top_by_quantity: list[tuple[str, Decimal]]
    previous_total: Decimal | None


class StatsService:
    def __init__(self, session: Session, timezone: str = "Europe/Moscow"):
        self.session = session
        self.tz = ZoneInfo(timezone)

    def boundaries(self, period: str, now: datetime | None = None) -> tuple[datetime, datetime]:
        current = now or datetime.now(self.tz)
        current = (
            current.replace(tzinfo=self.tz)
            if current.tzinfo is None
            else current.astimezone(self.tz)
        )
        if period == "today":
            start = current.replace(hour=0, minute=0, second=0, microsecond=0)
            return start, start + timedelta(days=1)
        if period == "week":
            start = (current - timedelta(days=current.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            return start, start + timedelta(days=7)
        if period == "month":
            start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
            return start, next_month
        raise ValueError(f"Неизвестный период: {period}")

    def period(self, period: str, now: datetime | None = None) -> PeriodStats:
        start, end = self.boundaries(period, now)
        rows = list(
            self.session.scalars(
                select(Receipt)
                .options(selectinload(Receipt.items))
                .where(
                    Receipt.status == ProcessingStatus.PARSED,
                    Receipt.purchased_at >= start,
                    Receipt.purchased_at < end,
                )
            )
        )
        total = sum((row.total or Decimal("0") for row in rows), Decimal("0"))
        stores: dict[str, Decimal] = {}
        spend: dict[str, Decimal] = {}
        quantities: dict[str, Decimal] = {}
        for receipt in rows:
            store = receipt.store or receipt.seller or "Неизвестно"
            stores[store] = stores.get(store, Decimal("0")) + (receipt.total or Decimal("0"))
            for item in receipt.items:
                spend[item.normalized_name] = (
                    spend.get(item.normalized_name, Decimal("0")) + item.total
                )
                quantities[item.normalized_name] = (
                    quantities.get(item.normalized_name, Decimal("0")) + item.quantity
                )
        duration = end - start
        previous_total = self.session.scalar(
            select(func.sum(Receipt.total)).where(
                Receipt.status == ProcessingStatus.PARSED,
                Receipt.purchased_at >= start - duration,
                Receipt.purchased_at < start,
            )
        )

        def sort(values: dict[str, Decimal]) -> list[tuple[str, Decimal]]:
            return sorted(values.items(), key=lambda row: row[1], reverse=True)[:5]

        return PeriodStats(
            start=start,
            end=end,
            total=total,
            receipt_count=len(rows),
            average=(total / len(rows)).quantize(Decimal("0.01")) if rows else Decimal("0"),
            stores=sort(stores),
            top_by_spend=sort(spend),
            top_by_quantity=sort(quantities),
            previous_total=Decimal(previous_total) if previous_total is not None else None,
        )

    def last_receipt(self) -> Receipt | None:
        return self.session.scalar(
            select(Receipt)
            .options(selectinload(Receipt.items))
            .where(Receipt.status == ProcessingStatus.PARSED)
            .order_by(Receipt.purchased_at.desc())
        )

    def price_history(self, query: str) -> list[tuple[datetime, str, Decimal]]:
        statement = (
            select(Receipt.purchased_at, ReceiptItem.normalized_name, ReceiptItem.unit_price)
            .join(ReceiptItem.receipt)
            .where(
                Receipt.status == ProcessingStatus.PARSED,
                ReceiptItem.normalized_name.ilike(f"%{query}%"),
            )
            .order_by(Receipt.purchased_at.desc())
            .limit(20)
        )
        return [
            (date_, name, Decimal(price)) for date_, name, price in self.session.execute(statement)
        ]
