from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import ProcessingStatus, Receipt, ReceiptItem, SyncRun


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
    categories: list[tuple[str, Decimal]]
    comparison_total: Decimal
    comparison_end: datetime
    previous_start: datetime
    previous_end: datetime


@dataclass(frozen=True)
class ImportStatus:
    last_success: datetime | None
    latest_run: SyncRun | None
    failed_receipts: int
    last_purchase: datetime | None


class StatsService:
    def __init__(self, session: Session, timezone: str = "Europe/Moscow"):
        self.session = session
        self.tz = ZoneInfo(timezone)

    def local_now(self, now: datetime | None = None) -> datetime:
        current = now or datetime.now(self.tz)
        return (
            current.replace(tzinfo=self.tz)
            if current.tzinfo is None
            else current.astimezone(self.tz)
        )

    def boundaries(self, period: str, now: datetime | None = None) -> tuple[datetime, datetime]:
        current = self.local_now(now)
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
        current = self.local_now(now)
        start, _ = self.boundaries(period, current)
        end = current
        if period == "month":
            previous_start = (start - timedelta(days=1)).replace(day=1)
        else:
            previous_start = start - timedelta(days=1 if period == "today" else 7)
        # Match elapsed local calendar time, capped by the shorter calendar month.
        elapsed = min(end - start, start - previous_start)
        comparison_end = start + elapsed
        previous_end = previous_start + elapsed
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
        categories: dict[str, Decimal] = {}
        for receipt in rows:
            store = receipt.store or receipt.seller or "Неизвестно"
            stores[store] = stores.get(store, Decimal("0")) + (receipt.total or Decimal("0"))
            for item in receipt.items:
                category = item.category or "другое"
                categories[category] = categories.get(category, Decimal("0")) + item.total
                spend[item.normalized_name] = (
                    spend.get(item.normalized_name, Decimal("0")) + item.total
                )
                quantities[item.normalized_name] = (
                    quantities.get(item.normalized_name, Decimal("0")) + item.quantity
                )
        previous_total = self.session.scalar(
            select(func.sum(Receipt.total)).where(
                Receipt.status == ProcessingStatus.PARSED,
                Receipt.purchased_at >= previous_start,
                Receipt.purchased_at < previous_end,
            )
        )

        comparison_total = total
        if comparison_end < end:
            comparison_total = self.session.scalar(
                select(func.sum(Receipt.total)).where(
                    Receipt.status == ProcessingStatus.PARSED,
                    Receipt.purchased_at >= start,
                    Receipt.purchased_at < comparison_end,
                )
            ) or Decimal("0")

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
            categories=sorted(categories.items(), key=lambda row: (-row[1], row[0])),
            comparison_total=Decimal(comparison_total),
            comparison_end=comparison_end,
            previous_start=previous_start,
            previous_end=previous_end,
        )

    def receipt_filters(self, period: str | None = None, now: datetime | None = None):
        filters = [Receipt.status == ProcessingStatus.PARSED]
        if period is not None:
            current = self.local_now(now)
            start, _ = self.boundaries(period, current)
            filters.extend([Receipt.purchased_at >= start, Receipt.purchased_at < current])
        return filters

    def top(self, period: str | None = None, now: datetime | None = None):
        filters = self.receipt_filters(period, now)
        rankings = []
        for field in (ReceiptItem.total, ReceiptItem.quantity):
            rankings.append(
                self.session.execute(
                    select(ReceiptItem.normalized_name, func.sum(field))
                    .join(ReceiptItem.receipt)
                    .where(*filters)
                    .group_by(ReceiptItem.normalized_name)
                    .order_by(func.sum(field).desc(), ReceiptItem.normalized_name)
                    .limit(10)
                ).all()
            )
        return tuple(rankings)

    def stores(self, period: str | None = None, now: datetime | None = None):
        return self.session.execute(
            select(
                Receipt.store,
                func.sum(Receipt.total),
                func.count(Receipt.id),
                func.avg(Receipt.total),
            )
            .where(*self.receipt_filters(period, now))
            .group_by(Receipt.store)
            .order_by(func.sum(Receipt.total).desc(), Receipt.store)
        ).all()

    def import_status(self) -> ImportStatus:
        return ImportStatus(
            last_success=self.session.scalar(
                select(func.max(SyncRun.finished_at)).where(
                    SyncRun.status == "completed",
                    SyncRun.failed == 0,
                )
            ),
            latest_run=self.session.scalar(
                select(SyncRun).order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).limit(1)
            ),
            failed_receipts=self.session.scalar(
                select(func.count(Receipt.id)).where(Receipt.status == ProcessingStatus.FAILED)
            )
            or 0,
            last_purchase=self.session.scalar(
                select(func.max(Receipt.purchased_at)).where(
                    Receipt.status == ProcessingStatus.PARSED
                )
            ),
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
