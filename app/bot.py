from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps

from sqlalchemy import func, select
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import get_settings
from app.db import SessionLocal
from app.models import ProcessingStatus, Receipt, ReceiptItem
from app.services.reports import money, period_report, receipt_report
from app.services.stats import StatsService
from app.services.sync import sync_receipts

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]

HELP = """Команды:
/today — расходы сегодня
/week — текущая неделя
/month — текущий месяц
/top — топ товаров
/stores — расходы и средний чек по магазинам
/prices <запрос> — история цены
/last — последний чек
/sync — синхронизировать Gmail
/help — эта справка"""


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
    await update.effective_message.reply_text(HELP)


def period_handler(period: str, title: str) -> Handler:
    @allowed
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        with SessionLocal() as session:
            stats = StatsService(session, get_settings().default_timezone).period(period)
            text = period_report(stats, title)
        await update.effective_message.reply_text(text)

    return handler


@allowed
async def last_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with SessionLocal() as session:
        text = receipt_report(StatsService(session).last_receipt())
    await update.effective_message.reply_text(text)


@allowed
async def prices_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text("Использование: /prices <название товара>")
        return
    with SessionLocal() as session:
        rows = StatsService(session).price_history(query)
    text = "\n".join(
        [f"История цены: {query}"]
        + [f"• {date_:%d.%m.%Y} — {name}: {money(price)}" for date_, name, price in rows]
    )
    await update.effective_message.reply_text(text if rows else "Ничего не найдено.")


@allowed
async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Запускаю синхронизацию…")
    run = await asyncio.to_thread(sync_receipts)
    await update.effective_message.reply_text(
        f"Готово: разобрано {run.parsed}, пропущено {run.skipped}, ошибок {run.failed}."
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
    lines = ["Топ по расходам:"] + [f"• {name}: {money(value)}" for name, value in spend]
    lines += ["", "Топ по количеству:"] + [f"• {name}: {value}" for name, value in quantity]
    await update.effective_message.reply_text("\n".join(lines))


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
    lines = ["Расходы по магазинам:"]
    lines += [
        f"• {store or 'Неизвестно'}: {money(total)}, чеков {count}, средний {money(avg)}"
        for store, total, count, avg in rows
    ]
    await update.effective_message.reply_text("\n".join(lines))


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
