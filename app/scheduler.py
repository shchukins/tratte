from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from app.config import get_settings
from app.services.sync import sync_receipts


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
    logging.getLogger(__name__).info(
        "Плановая синхронизация каждые %s минут", settings.sync_interval_minutes
    )
    scheduler.start()
