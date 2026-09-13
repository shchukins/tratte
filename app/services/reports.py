from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from html import escape

from app.models import Receipt
from app.services.stats import PeriodStats


def money(value: Decimal) -> str:
    return f"{value:,.2f} ₽".replace(",", " ").replace(".", ",")


def quantity(value: Decimal) -> str:
    return format(value.normalize(), "f").replace(".", ",")


def label(value: str) -> str:
    """Keep external text on one line and bound its display length before escaping."""
    text = " ".join(value.split())
    if len(text) > 160:
        text = text[:159] + "…"
    return escape(text)


def message_chunks(text: str) -> Iterable[str]:
    """Split self-contained HTML lines without breaking tags or entities.

    Counting the encoded source in UTF-16 is conservative for Telegram's limit.
    External labels are bounded, so every individual report line fits.
    """
    lines: list[str] = []
    size = 0
    for line in text.splitlines():
        line_size = len(line.encode("utf-16-le")) // 2
        if line_size > 4096:
            raise ValueError("Report line exceeds Telegram's message limit")
        if lines and size + 1 + line_size > 4096:
            yield "\n".join(lines).strip()
            lines, size = [], 0
        size += line_size + bool(lines)
        lines.append(line)
    if lines:
        chunk = "\n".join(lines).strip()
        if chunk:
            yield chunk


def ranking(title: str, rows: list[tuple[str, Decimal]], *, by_quantity: bool = False) -> str:
    formatter = quantity if by_quantity else money
    return "\n".join(
        [f"<b>{title}</b>"]
        + [
            f"{index}. {label(name)} — <b>{formatter(value)}</b>"
            for index, (name, value) in enumerate(rows, 1)
        ]
    )


def period_report(stats: PeriodStats, title: str) -> str:
    last_day = stats.end - timedelta(days=1)
    dates = stats.start.strftime("%d.%m.%Y")
    if last_day.date() != stats.start.date():
        dates += f" — {last_day:%d.%m.%Y}"
    lines = [f"📊 <b>{label(title)}</b>", f"<i>{dates}</i>", ""]
    if not stats.receipt_count:
        lines.append("За этот период расходов пока нет.")
        return "\n".join(lines)
    lines.extend(
        [
            f"Всего потрачено: <b>{money(stats.total)}</b>",
            f"Чеков: <b>{stats.receipt_count}</b>",
            f"Средний чек: <b>{money(stats.average)}</b>",
        ]
    )
    if stats.previous_total is not None:
        if stats.previous_total:
            change = (stats.total - stats.previous_total) / stats.previous_total * 100
            change_text = f"{change:+.1f}%".replace(".", ",")
            lines.append(f"<i>К прошлому периоду: {change_text}</i>")
        else:
            lines.append("<i>В прошлом периоде расходов не было</i>")
    if stats.stores:
        lines.extend(["", "<b>По магазинам</b>"])
        lines.extend(f"• {label(name)} — <b>{money(value)}</b>" for name, value in stats.stores)
    if stats.top_by_spend:
        lines.extend(["", ranking("Топ по расходам", stats.top_by_spend)])
    if stats.top_by_quantity:
        lines.extend(["", ranking("Топ по количеству", stats.top_by_quantity, by_quantity=True)])
    return "\n".join(lines)


def receipt_report(receipt: Receipt | None) -> str:
    lines = ["🧾 <b>Последний чек</b>", ""]
    if receipt is None:
        return "\n".join(lines + ["Разобранных чеков пока нет."])
    lines.extend(
        [
            f"<b>{label(receipt.store or receipt.seller or 'Неизвестный магазин')}</b>",
            f"<i>{receipt.purchased_at:%d.%m.%Y %H:%M}</i>"
            if receipt.purchased_at
            else "<i>Дата неизвестна</i>",
            "",
            f"Итого: <b>{money(receipt.total or Decimal('0'))}</b>",
            "",
            "<b>Покупки</b>",
        ]
    )
    lines.extend(
        f"• {label(item.original_name)} — <b>{money(item.total)}</b>" for item in receipt.items[:30]
    )
    if len(receipt.items) > 30:
        lines.append(f"<i>Показаны 30 из {len(receipt.items)} позиций.</i>")
    return "\n".join(lines)


def prices_report(query: str, rows: list[tuple[datetime | None, str, Decimal]]) -> str:
    lines = ["🏷 <b>История цены</b>", f"Поиск: {label(query)}", ""]
    if not rows:
        return "\n".join(lines + ["Ничего не найдено."])
    for date_, name, price in rows:
        date_text = date_.strftime("%d.%m.%Y") if date_ else "Дата неизвестна"
        lines.extend([f"<b>{money(price)}</b> · {date_text}", label(name), ""])
    return "\n".join(lines).rstrip()


def top_report(spend: list[tuple[str, Decimal]], counts: list[tuple[str, Decimal]]) -> str:
    lines = ["🛒 <b>Топ товаров</b>", "<i>За всё время</i>", ""]
    if not spend and not counts:
        return "\n".join(lines + ["Данных о покупках пока нет."])
    if spend:
        lines.extend([ranking("По расходам", spend), ""])
    if counts:
        lines.append(ranking("По количеству", counts, by_quantity=True))
    return "\n".join(lines).rstrip()


def stores_report(rows: list[tuple[str | None, Decimal, int, Decimal]]) -> str:
    lines = ["🏪 <b>Расходы по магазинам</b>", "<i>За всё время</i>", ""]
    if not rows:
        return "\n".join(lines + ["Данных о покупках пока нет."])
    for store, total, count, average in rows:
        lines.extend(
            [
                f"<b>{label(store or 'Неизвестный магазин')}</b>",
                f"Всего: <b>{money(total)}</b>",
                f"Чеков: {count} · Средний чек: {money(average)}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()
