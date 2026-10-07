from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from app.config import get_settings
from app.services.sync import sync_receipts
from app.services.weekly_summary import deliver_weekly_summaries


def run_scheduler() -> None:
    settings = get_settings()
    scheduler = BlockingScheduler(timezone=settings.default_timezone)
    scheduler.add_job(
        sync_receipts,
        "interval",
        minutes=settings.sync_interval_minutes,
        id="gmail-sync",
        max_instances=1,
        coalesce=True,
    )
    if settings.weekly_summary_enabled:
        scheduler.add_job(
            deliver_weekly_summaries,
            "cron",
            minute="*",
            id="weekly-summary",
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(ZoneInfo(settings.default_timezone)),
        )
    logging.getLogger(__name__).info(
        "Плановая синхронизация каждые %s минут", settings.sync_interval_minutes
    )
    scheduler.start()
