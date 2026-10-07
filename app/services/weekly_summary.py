from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from app.bot import QUICK_ACTIONS
from app.config import get_settings
from app.db import SessionLocal
from app.models import WeeklySummaryDelivery
from app.services.reports import weekly_summary_report
from app.services.stats import StatsService

logger = logging.getLogger(__name__)


async def send_weekly_summaries(now: datetime | None = None) -> None:
    settings = get_settings()
    if (
        not settings.weekly_summary_enabled
        or not settings.telegram_bot_token
        or not settings.telegram_allowed_user_ids
    ):
        return
    tz = ZoneInfo(settings.default_timezone)
    current = now or datetime.now(tz)
    current = current.replace(tzinfo=tz) if current.tzinfo is None else current.astimezone(tz)
    with SessionLocal() as session:
        service = StatsService(session, settings.default_timezone)
        monday, _ = service.boundaries("week", current)
        due = monday.replace(
            hour=settings.weekly_summary_hour, minute=settings.weekly_summary_minute
        )
        if current < due:
            return
        stats = service.completed_week(current)
        already_sent = set(
            session.scalars(
                select(WeeklySummaryDelivery.telegram_user_id).where(
                    WeeklySummaryDelivery.week_start == stats.start.date()
                )
            )
        )
        recipients = sorted(settings.telegram_allowed_user_ids - already_sent)
        report = weekly_summary_report(stats)
    if not recipients:
        return
    async with Bot(settings.telegram_bot_token) as bot:
        for user_id in recipients:
            with SessionLocal() as session:
                # Serialize overlapping schedulers on PostgreSQL for this recipient.
                # SQLite is supported for local use with a single scheduler process.
                if session.get_bind().dialect.name == "postgresql":
                    session.execute(
                        text("SELECT pg_advisory_xact_lock(:namespace, :recipient)"),
                        {"namespace": 74621, "recipient": user_id % (2**31)},
                    )
                delivered = session.scalar(
                    select(WeeklySummaryDelivery.id).where(
                        WeeklySummaryDelivery.telegram_user_id == user_id,
                        WeeklySummaryDelivery.week_start == stats.start.date(),
                    )
                )
                if delivered is not None:
                    continue
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text=report,
                        parse_mode=ParseMode.HTML,
                        reply_markup=QUICK_ACTIONS,
                        disable_web_page_preview=True,
                    )
                except TelegramError as exc:
                    # Do not log token-bearing URLs, recipient IDs or report data.
                    logger.warning("Weekly summary delivery failed: %s", type(exc).__name__)
                    continue
                session.add(
                    WeeklySummaryDelivery(telegram_user_id=user_id, week_start=stats.start.date())
                )
                session.commit()


def deliver_weekly_summaries() -> None:
    asyncio.run(send_weekly_summaries())
