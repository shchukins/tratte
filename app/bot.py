from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps

from sqlalchemy import func, select
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import get_settings
from app.db import SessionLocal
from app.models import ProcessingStatus, Receipt, ReceiptItem
from app.services.reports import (
    message_chunks,
    period_report,
    prices_report,
    receipt_report,
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
/top — топ товаров
/stores — расходы и средний чек по магазинам
/prices название — история цены
/last — последний чек

<b>Управление</b>
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
            text = period_report(stats, title)
        await reply_html(update, text)

    return handler


@allowed
async def last_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with SessionLocal() as session:
        text = receipt_report(StatsService(session).last_receipt())
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
    await reply_html(update, prices_report(query, rows))


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


@allowed
async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with SessionLocal() as session:
        spend = session.execute(
            select(ReceiptItem.normalized_name, func.sum(ReceiptItem.total))
            .join(ReceiptItem.receipt)
            .where(Receipt.status == ProcessingStatus.PARSED)
            .group_by(ReceiptItem.normalized_name)
            .order_by(func.sum(ReceiptItem.total).desc())
            .limit(10)
        ).all()
        quantity = session.execute(
            select(ReceiptItem.normalized_name, func.sum(ReceiptItem.quantity))
            .join(ReceiptItem.receipt)
            .where(Receipt.status == ProcessingStatus.PARSED)
            .group_by(ReceiptItem.normalized_name)
            .order_by(func.sum(ReceiptItem.quantity).desc())
            .limit(10)
        ).all()
    await reply_html(update, top_report(spend, quantity))


@allowed
async def stores_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with SessionLocal() as session:
        rows = session.execute(
            select(
                Receipt.store,
                func.sum(Receipt.total),
                func.count(Receipt.id),
                func.avg(Receipt.total),
            )
            .where(Receipt.status == ProcessingStatus.PARSED)
            .group_by(Receipt.store)
            .order_by(func.sum(Receipt.total).desc())
        ).all()
    await reply_html(update, stores_report(rows))


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
    return application


def run_bot() -> None:
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)
