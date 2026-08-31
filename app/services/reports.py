from __future__ import annotations

from decimal import Decimal

from app.models import Receipt
from app.services.stats import PeriodStats


def money(value: Decimal) -> str:
    return f"{value:,.2f} ₽".replace(",", " ")


def period_report(stats: PeriodStats, title: str) -> str:
    lines = [
        title,
        f"Всего: {money(stats.total)}",
        f"Чеков: {stats.receipt_count}; средний чек: {money(stats.average)}",
    ]
    if stats.previous_total is not None:
        if stats.previous_total:
            change = (stats.total - stats.previous_total) / stats.previous_total * 100
            lines.append(f"К прошлому периоду: {change:+.1f}%")
        else:
            lines.append("В прошлом периоде расходов не было")
    if stats.stores:
        lines.extend(
            ["", "По магазинам:"] + [f"• {name}: {money(value)}" for name, value in stats.stores]
        )
    if stats.top_by_spend:
        lines.extend(
            ["", "Топ по сумме:"]
            + [f"• {name}: {money(value)}" for name, value in stats.top_by_spend]
        )
    if stats.top_by_quantity:
        lines.extend(
            ["", "Топ по количеству:"]
            + [f"• {name}: {value.normalize()}" for name, value in stats.top_by_quantity]
        )
    return "\n".join(lines)


def receipt_report(receipt: Receipt | None) -> str:
    if receipt is None:
        return "Разобранных чеков пока нет."
    lines = [
        receipt.store or receipt.seller or "Неизвестный магазин",
        receipt.purchased_at.strftime("%d.%m.%Y %H:%M")
        if receipt.purchased_at
        else "Дата неизвестна",
        f"Итого: {money(receipt.total or Decimal('0'))}",
        "",
    ]
    lines.extend(f"• {item.original_name} — {money(item.total)}" for item in receipt.items[:30])
    return "\n".join(lines)
