import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from telegram.error import Forbidden

from app.config import Settings
from app.models import Receipt, ReceiptItem, WeeklySummaryDelivery
from app.scheduler import run_scheduler
from app.services.reports import weekly_summary_report
from app.services.stats import StatsService
from app.services.weekly_summary import send_weekly_summaries


def receipt(session, when, amount, *, category="другое", status="parsed", item_total=None):
    value = Decimal(amount)
    session.add(
        Receipt(
            gmail_message_id=str(session.query(Receipt).count()),
            purchased_at=when,
            total=value,
            status=status,
            items=[
                ReceiptItem(
                    original_name="Товар",
                    normalized_name="товар",
                    unit_price=value,
                    quantity=Decimal("1"),
                    total=Decimal(item_total) if item_total is not None else value,
                    category=category,
                )
            ],
        )
    )
    session.commit()


def settings(**overrides):
    return Settings(
        _env_file=None,
        telegram_bot_token="123:test-token",
        telegram_allowed_user_ids={1, 2},
        **overrides,
    )


def deliveries(session):
    return list(session.scalars(select(WeeklySummaryDelivery.telegram_user_id)))


def run_delivery(session, bot, current, config=None):
    with (
        patch("app.services.weekly_summary.get_settings", return_value=config or settings()),
        patch(
            "app.services.weekly_summary.SessionLocal",
            sessionmaker(bind=session.get_bind(), expire_on_commit=False),
        ),
        patch("app.services.weekly_summary.Bot") as factory,
    ):
        factory.return_value.__aenter__ = AsyncMock(return_value=bot)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        asyncio.run(send_weekly_summaries(current))
        return factory


def test_completed_week_includes_sunday_and_excludes_next_monday(session):
    receipt(session, datetime(2026, 9, 28), "100")
    receipt(session, datetime(2026, 10, 4, 23, 59, 59), "50")
    receipt(session, datetime(2026, 10, 5), "999")
    receipt(session, datetime(2026, 9, 21), "100")
    receipt(session, datetime(2026, 9, 27, 23, 59, 59), "200")
    receipt(session, datetime(2026, 9, 28, 1), "900", status="failed")
    receipt(session, datetime(2026, 9, 20, 23, 59, 59), "900")
    stats = StatsService(session).completed_week(datetime(2026, 10, 5, 9))
    assert stats.total == Decimal("150")
    assert stats.receipt_count == 2
    assert stats.average == Decimal("75")
    assert stats.previous_total == Decimal("300")
    report = weekly_summary_report(stats)
    assert "28.09.2026 — 04.10.2026" in report
    assert "21.09.2026 — 27.09.2026" in report
    assert "−150,00 ₽ · -50,0%" in report
    assert "до 00:00" not in report


@pytest.mark.parametrize(
    "current,start,end",
    [
        (datetime(2026, 1, 5, 9), date(2025, 12, 29), date(2026, 1, 5)),
        (datetime(2026, 10, 4, 22, tzinfo=UTC), date(2026, 9, 28), date(2026, 10, 5)),
        (datetime(2026, 10, 5, 9), date(2026, 9, 28), date(2026, 10, 5)),
    ],
)
def test_completed_week_local_calendar_boundaries(session, current, start, end):
    stats = StatsService(session).completed_week(current)
    assert stats.start.date() == start
    assert stats.end.date() == end
    assert stats.end.hour == 0
    assert stats.start.tzinfo == ZoneInfo("Europe/Moscow")


def test_completed_week_spans_dst_in_local_calendar(session):
    stats = StatsService(session, "Europe/Berlin").completed_week(datetime(2026, 10, 26, 9))
    assert stats.start == datetime(2026, 10, 19, tzinfo=ZoneInfo("Europe/Berlin"))
    assert stats.end == datetime(2026, 10, 26, tzinfo=ZoneInfo("Europe/Berlin"))
    assert stats.start.utcoffset() != stats.end.utcoffset()


def test_summary_categories_use_all_item_totals_for_shares_and_escape_html(session):
    for category in ["a", "b", "c", "d", "e", "<другое>&"]:
        receipt(session, datetime(2026, 10, 1), "100", category=category, item_total="10")
    stats = StatsService(session).completed_week(datetime(2026, 10, 5, 9))
    report = weekly_summary_report(stats)
    assert "600,00 ₽" in report
    assert "&lt;другое&gt;&amp;" in report
    assert report.count("10,00 ₽</b> · 16,7%") == 5
    assert "Остальные категории — <b>10,00 ₽" in report


def test_empty_week_is_reported_and_compared_to_previous(session):
    report = weekly_summary_report(StatsService(session).completed_week(datetime(2026, 10, 5)))
    assert "За эту неделю разобранных чеков нет" in report
    assert "Нет данных для сравнения" in report
    receipt(session, datetime(2026, 9, 22), "100")
    report = weekly_summary_report(StatsService(session).completed_week(datetime(2026, 10, 5)))
    assert "−100,00 ₽ · -100,0%" in report


def test_previous_zero_does_not_divide_by_zero(session):
    receipt(session, datetime(2026, 9, 22), "0")
    receipt(session, datetime(2026, 10, 1), "100")
    report = weekly_summary_report(StatsService(session).completed_week(datetime(2026, 10, 5)))
    assert "+100,00 ₽" in report
    assert "процент не рассчитывается" in report


def test_success_is_durable_per_recipient_and_week(session):
    bot = MagicMock(send_message=AsyncMock())
    run_delivery(session, bot, datetime(2026, 10, 5, 9))
    assert bot.send_message.await_count == 2
    assert set(deliveries(session)) == {1, 2}
    call = bot.send_message.call_args.kwargs
    assert call["parse_mode"] == "HTML"
    assert call["reply_markup"] is not None
    assert call["chat_id"] in {1, 2}
    factory = run_delivery(session, bot, datetime(2026, 10, 7, 12))
    factory.assert_not_called()
    assert bot.send_message.await_count == 2
    run_delivery(session, bot, datetime(2026, 10, 12, 9))
    assert bot.send_message.await_count == 4


def test_failed_recipient_does_not_block_others_and_retries(session, caplog):
    bot = MagicMock(send_message=AsyncMock(side_effect=[Forbidden("PRIVATE"), None]))
    run_delivery(session, bot, datetime(2026, 10, 5, 9))
    assert deliveries(session) == [2]
    assert "PRIVATE" not in caplog.text
    bot.send_message.side_effect = None
    run_delivery(session, bot, datetime(2026, 10, 5, 9, 1))
    assert bot.send_message.await_count == 3
    assert bot.send_message.call_args.kwargs["chat_id"] == 1
    assert set(deliveries(session)) == {1, 2}


@pytest.mark.parametrize(
    "current",
    [datetime(2026, 10, 5, 8, 59), datetime(2026, 10, 12, 8, 59)],
)
def test_no_send_before_monday_due_time(session, current):
    bot = MagicMock(send_message=AsyncMock())
    factory = run_delivery(session, bot, current)
    factory.assert_not_called()
    assert not deliveries(session)


def test_delivery_time_uses_timezone_and_configured_hour_minute(session):
    bot = MagicMock(send_message=AsyncMock())
    config = settings(weekly_summary_hour=10, weekly_summary_minute=30)
    run_delivery(session, bot, datetime(2026, 10, 5, 7, 29, tzinfo=UTC), config)
    assert bot.send_message.await_count == 0
    run_delivery(session, bot, datetime(2026, 10, 5, 7, 30, tzinfo=UTC), config)
    assert bot.send_message.await_count == 2


def test_restart_catchup_sends_only_latest_completed_week(session):
    bot = MagicMock(send_message=AsyncMock())
    run_delivery(session, bot, datetime(2026, 10, 8, 18))
    assert bot.send_message.await_count == 2
    assert "28.09.2026 — 04.10.2026" in bot.send_message.call_args.kwargs["text"]
    starts = set(session.scalars(select(WeeklySummaryDelivery.week_start)))
    assert starts == {date(2026, 9, 28)}


@pytest.mark.parametrize(
    "field,value",
    [
        ("weekly_summary_enabled", False),
        ("telegram_bot_token", ""),
        ("telegram_allowed_user_ids", set()),
    ],
)
def test_disabled_or_unconfigured_summary_does_not_query_or_send(field, value):
    config = settings().model_copy(update={field: value})
    with (
        patch("app.services.weekly_summary.get_settings", return_value=config),
        patch("app.services.weekly_summary.SessionLocal") as database,
        patch("app.services.weekly_summary.Bot") as bot,
    ):
        asyncio.run(send_weekly_summaries(datetime(2026, 10, 5, 9)))
    database.assert_not_called()
    bot.assert_not_called()


@pytest.mark.parametrize("enabled", [True, False])
def test_scheduler_registers_recovery_check_and_keeps_sync(enabled):
    with (
        patch("app.scheduler.get_settings", return_value=settings(weekly_summary_enabled=enabled)),
        patch("app.scheduler.BlockingScheduler") as factory,
    ):
        run_scheduler()
    jobs = {call.kwargs["id"]: call for call in factory.return_value.add_job.call_args_list}
    assert jobs["gmail-sync"].kwargs["minutes"] == 15
    assert ("weekly-summary" in jobs) == enabled
    if enabled:
        assert jobs["weekly-summary"].kwargs["max_instances"] == 1
        assert jobs["weekly-summary"].kwargs["minute"] == "*"
        assert jobs["weekly-summary"].kwargs["next_run_time"].tzinfo is not None
    factory.return_value.start.assert_called_once()


@pytest.mark.parametrize(
    "field,value",
    [
        ("weekly_summary_hour", -1),
        ("weekly_summary_hour", 24),
        ("weekly_summary_minute", -1),
        ("weekly_summary_minute", 60),
    ],
)
def test_invalid_summary_time_is_rejected(field, value):
    with pytest.raises(ValidationError):
        settings(**{field: value})
