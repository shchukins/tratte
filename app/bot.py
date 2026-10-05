from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import get_settings
from app.db import SessionLocal
from app.services.reports import (
    message_chunks,
    period_report,
    prices_report,
    receipt_report,
    status_report,
    stores_report,
    top_report,
)
from app.services.stats import StatsService
from app.services.sync import sync_receipts

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]

HELP = """👋 <b>Tratte · ваши покупки</b>

<b>Расходы</b>
/today — расходы сегодня
/week — текущая неделя
/month — текущий месяц

<b>Покупки и магазины</b>
/top [today|week|month] — топ товаров
/stores [today|week|month] — расходы по магазинам
/prices название — история цены
/last — последний чек

<b>Управление</b>
/status — состояние импорта чеков
/sync — синхронизировать Gmail
/help — эта справка"""


async def reply_html(update: Update, text: str) -> None:
    for chunk in message_chunks(text):
        await update.effective_message.reply_text(
            chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )


def allowed(handler: Handler) -> Handler:
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id if update.effective_user else None
        if user_id not in get_settings().telegram_allowed_user_ids:
            if update.effective_message:
                await update.effective_message.reply_text("Доступ запрещён.")
            return
        await handler(update, context)

    return wrapper


@allowed
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_html(update, HELP)


def period_handler(period: str, title: str) -> Handler:
    @allowed
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        with SessionLocal() as session:
            stats = StatsService(session, get_settings().default_timezone).period(period)
            text = period_report(stats, title, show_categories=period == "month")
        await reply_html(update, text)

    return handler


@allowed
async def last_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with SessionLocal() as session:
        text = receipt_report(StatsService(session).last_receipt(), get_settings().default_timezone)
    await reply_html(update, text)


@allowed
async def prices_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await reply_html(
            update,
            "🏷 <b>История цены</b>\n\nВведите название товара после команды.\n"
            "Например: <code>/prices молоко</code>",
        )
        return
    with SessionLocal() as session:
        rows = StatsService(session).price_history(query)
    await reply_html(update, prices_report(query, rows, get_settings().default_timezone))


@allowed
async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_html(update, "🔄 <b>Синхронизация Gmail</b>\n\nПроверяю новые чеки…")
    run = await asyncio.to_thread(sync_receipts)
    title = "⚠️ Синхронизация завершена с ошибками" if run.failed else "✅ Синхронизация завершена"
    await reply_html(
        update,
        f"<b>{title}</b>\n\n"
        f"• Разобрано: <b>{run.parsed}</b>\n"
        f"• Пропущено: <b>{run.skipped}</b>\n"
        f"• Ошибок: <b>{run.failed}</b>",
    )


PERIOD_TITLES = {"today": "Сегодня", "week": "Текущая неделя", "month": "Текущий месяц"}


def command_period(args: list[str]) -> str | None:
    if not args:
        return None
    if len(args) == 1 and args[0].lower() in PERIOD_TITLES:
        return args[0].lower()
    raise ValueError("Укажите период: today, week или month. Без периода — за всё время.")


@allowed
async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        period = command_period(context.args)
    except ValueError as exc:
        await reply_html(update, str(exc))
        return
    with SessionLocal() as session:
        spend, quantity = StatsService(session, get_settings().default_timezone).top(period)
    await reply_html(update, top_report(spend, quantity, PERIOD_TITLES.get(period, "За всё время")))


@allowed
async def stores_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        period = command_period(context.args)
    except ValueError as exc:
        await reply_html(update, str(exc))
        return
    with SessionLocal() as session:
        rows = StatsService(session, get_settings().default_timezone).stores(period)
    await reply_html(update, stores_report(rows, PERIOD_TITLES.get(period, "За всё время")))


@allowed
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with SessionLocal() as session:
        status = StatsService(session).import_status()
        text = status_report(status, get_settings().default_timezone)
    await reply_html(update, text)


def build_application() -> Application:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler(["start", "help"], help_command))
    application.add_handler(CommandHandler("today", period_handler("today", "Сегодня")))
    application.add_handler(CommandHandler("week", period_handler("week", "Текущая неделя")))
    application.add_handler(CommandHandler("month", period_handler("month", "Текущий месяц")))
    application.add_handler(CommandHandler("top", top_command))
    application.add_handler(CommandHandler("stores", stores_command))
    application.add_handler(CommandHandler("prices", prices_command))
    application.add_handler(CommandHandler("last", last_command))
    application.add_handler(CommandHandler("sync", sync_command))
    application.add_handler(CommandHandler("status", status_command))
    return application


def run_bot() -> None:
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)
