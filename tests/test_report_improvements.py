import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.bot import command_period, period_handler, status_command, stores_command, top_command
from app.models import Receipt, ReceiptItem, SyncRun
from app.services.reports import period_report, status_report
from app.services.stats import ImportStatus, StatsService


def add_receipt(session, when, amount, *, category="другое", status="parsed", store="Магазин"):
    item_total = Decimal(amount)
    receipt = Receipt(
        gmail_message_id=str(session.query(Receipt).count()),
        purchased_at=when,
        total=item_total,
        status=status,
        store=store,
        items=[
            ReceiptItem(
                original_name="Товар",
                normalized_name="товар",
                unit_price=item_total,
                quantity=Decimal("1"),
                total=item_total,
                category=category,
            )
        ],
    )
    session.add(receipt)
    session.commit()
    return receipt


@pytest.mark.parametrize(
    "period,current,previous,included,excluded",
    [
        (
            "today",
            datetime(2026, 10, 6, 15),
            datetime(2026, 10, 5),
            datetime(2026, 10, 5, 14),
            datetime(2026, 10, 5, 15),
        ),
        (
            "week",
            datetime(2026, 10, 6, 15),
            datetime(2026, 9, 28),
            datetime(2026, 9, 29, 14),
            datetime(2026, 9, 29, 15),
        ),
        (
            "month",
            datetime(2026, 10, 6, 15),
            datetime(2026, 9, 1),
            datetime(2026, 9, 6, 14),
            datetime(2026, 9, 6, 15),
        ),
        (
            "month",
            datetime(2026, 1, 6, 15),
            datetime(2025, 12, 1),
            datetime(2025, 12, 6, 14),
            datetime(2025, 12, 6, 15),
        ),
    ],
)
def test_comparison_matches_elapsed_calendar_period(
    session,
    period,
    current,
    previous,
    included,
    excluded,
):
    add_receipt(session, included, "100")
    add_receipt(session, excluded, "900")
    add_receipt(session, current.replace(hour=14), "150")
    add_receipt(session, current, "700")
    stats = StatsService(session).period(period, current)
    assert stats.total == Decimal("150")
    assert stats.previous_total == Decimal("100")
    assert stats.previous_start.replace(tzinfo=None) == previous
    assert "+50,0%" in period_report(stats, "Период")


@pytest.mark.parametrize("year,days", [(2026, 28), (2024, 29)])
def test_month_comparison_caps_both_intervals_at_shorter_month(session, year, days):
    add_receipt(session, datetime(year, 2, days, 23), "100")
    add_receipt(session, datetime(year, 3, days, 23), "150")
    add_receipt(session, datetime(year, 3, 31, 10), "900")
    stats = StatsService(session).period("month", datetime(year, 3, 31, 15))
    assert stats.total == Decimal("1050")
    assert stats.comparison_total == Decimal("150")
    assert stats.previous_total == Decimal("100")
    assert stats.previous_end.date() == datetime(year, 3, 1).date()
    assert stats.comparison_end.day == days + 1
    report = period_report(stats, "Месяц")
    assert "+50,0%" in report
    assert "150,00 ₽ и 100,00 ₽" in report


def test_boundaries_use_configured_timezone(session):
    start, _ = StatsService(session).boundaries(
        "month",
        datetime(2026, 9, 30, 22, tzinfo=UTC),
    )
    assert start == datetime(2026, 10, 1, tzinfo=ZoneInfo("Europe/Moscow"))


def test_categories_use_item_totals_and_keep_other(session):
    first = add_receipt(session, datetime(2026, 10, 2), "30", category="напитки")
    first.total = Decimal("100")
    add_receipt(session, datetime(2026, 10, 3), "70")
    add_receipt(session, datetime(2026, 9, 3), "999")
    add_receipt(session, datetime(2026, 10, 3), "999", status="failed")
    session.commit()
    stats = StatsService(session).period("month", datetime(2026, 10, 6))
    assert stats.total == Decimal("170")
    assert stats.categories == [("другое", Decimal("70")), ("напитки", Decimal("30"))]
    report = period_report(stats, "Месяц", show_categories=True)
    assert "70,00 ₽</b> · 70,0%" in report
    assert "30,00 ₽</b> · 30,0%" in report
    assert "По категориям" not in period_report(stats, "Неделя")


@pytest.mark.parametrize(
    "period,expected", [(None, "1111"), ("month", "111"), ("week", "11"), ("today", "1")]
)
def test_rankings_filter_receipts_without_multiplying_store_totals(session, period, expected):
    for when, value in [
        (datetime(2026, 9, 1), "1000"),
        (datetime(2026, 10, 1), "100"),
        (datetime(2026, 10, 5), "10"),
        (datetime(2026, 10, 6), "1"),
    ]:
        add_receipt(session, when, value)
    add_receipt(session, datetime(2026, 10, 6, 16), "999", status="failed")
    service = StatsService(session)
    spend, _ = service.top(period, datetime(2026, 10, 6, 15))
    stores = service.stores(period, datetime(2026, 10, 6, 15))
    assert spend[0][1] == Decimal(expected)
    assert stores[0][1] == Decimal(expected)


def test_status_keeps_success_separate_from_latest_errors(session):
    success = SyncRun(
        started_at=datetime(2026, 10, 5),
        finished_at=datetime(2026, 10, 5, 1),
        status="completed",
        failed=0,
    )
    partial = SyncRun(
        started_at=datetime(2026, 10, 6),
        finished_at=datetime(2026, 10, 6, 1),
        status="completed",
        failed=2,
        parsed=3,
        error="PRIVATE_ERROR",
    )
    session.add_all([success, partial])
    add_receipt(session, datetime(2026, 10, 4), "1")
    add_receipt(session, datetime(2026, 10, 5), "1", status="failed")
    status = StatsService(session).import_status()
    assert status.last_success == success.finished_at
    assert status.latest_run.id == partial.id
    assert status.failed_receipts == 1
    assert status.last_purchase == datetime(2026, 10, 4)
    report = status_report(status)
    assert "завершён с ошибками" in report
    assert "PRIVATE_ERROR" not in report
    session.add(SyncRun(started_at=datetime(2026, 10, 6, 2), status="running"))
    session.commit()
    assert "нет отметки о завершении" in status_report(StatsService(session).import_status())


def test_status_empty_and_timezone(session):
    assert "Синхронизация ещё не запускалась" in status_report(
        StatsService(session).import_status()
    )
    status = ImportStatus(datetime(2026, 10, 5, 22, tzinfo=UTC), None, 0, None)
    assert "06.10.2026 01:00" in status_report(status)


@pytest.mark.parametrize("args,expected", [([], None), (["month"], "month"), (["WEEK"], "week")])
def test_period_arguments(args, expected):
    assert command_period(args) == expected


@pytest.mark.parametrize("args", [["year"], ["month", "extra"]])
def test_invalid_period_arguments(args):
    with pytest.raises(ValueError):
        command_period(args)


@pytest.mark.parametrize("handler", [top_command, stores_command, status_command])
def test_new_commands_keep_allowlist(handler):
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=99), effective_message=message)
    with patch("app.bot.get_settings", return_value=SimpleNamespace(telegram_allowed_user_ids={1})):
        asyncio.run(handler(update, SimpleNamespace(args=[])))
    message.reply_text.assert_awaited_once_with("Доступ запрещён.")


@pytest.mark.parametrize("handler", [top_command, stores_command])
def test_invalid_command_period_replies_without_querying_database(handler):
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_message=message)
    with (
        patch("app.bot.get_settings", return_value=SimpleNamespace(telegram_allowed_user_ids={1})),
        patch("app.bot.SessionLocal") as database,
    ):
        asyncio.run(handler(update, SimpleNamespace(args=["year"])))
    database.assert_not_called()
    assert "today, week или month" in message.reply_text.call_args.args[0]


@pytest.mark.parametrize(
    "handler,args,expected",
    [
        (top_command, ["month"], "Текущий месяц"),
        (stores_command, ["week"], "Текущая неделя"),
        (period_handler("month", "Текущий месяц"), [], "По категориям"),
        (status_command, [], "Неразобранных чеков с ошибкой"),
    ],
)
def test_authorized_commands_render_database_results(session, handler, args, expected):
    add_receipt(session, datetime(2026, 10, 6, 10), "123")
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_message=message)
    settings = SimpleNamespace(telegram_allowed_user_ids={1}, default_timezone="Europe/Moscow")
    with (
        patch("app.bot.get_settings", return_value=settings),
        patch("app.bot.SessionLocal", return_value=session),
        patch.object(
            StatsService,
            "local_now",
            return_value=datetime(
                2026,
                10,
                6,
                15,
                tzinfo=ZoneInfo("Europe/Moscow"),
            ),
        ),
    ):
        asyncio.run(handler(update, SimpleNamespace(args=args)))
    text = message.reply_text.call_args.args[0]
    assert expected in text
    if handler != status_command:
        assert "123,00 ₽" in text
    assert message.reply_text.call_args.kwargs["parse_mode"] == "HTML"
