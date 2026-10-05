from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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
/menu — меню с кнопками
/help — эта справка"""


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=f"nav:{action}") for label, action in row]
            for row in rows
        ]
    )


QUICK_ACTIONS = keyboard(
    [
        [("Сегодня", "today"), ("Этот месяц", "month")],
        [("Последний чек", "last"), ("☰ Меню", "menu")],
    ]
)
BACK_TO_MENU = keyboard([[("← Назад в меню", "menu")]])
MAIN_MENU = keyboard(
    [
        [("Сегодня", "today"), ("Неделя", "week"), ("Месяц", "month")],
        [("Топ товаров", "top"), ("Магазины", "stores")],
        [("История цены", "prices"), ("Последний чек", "last")],
        [("Статус", "status"), ("Синхронизировать", "sync")],
        [("Справка", "help")],
    ]
)
MENU_TEXT = "☰ <b>Tratte · меню</b>\n\nВыберите действие:"


async def reply_html(
    update: Update, text: str, reply_markup: InlineKeyboardMarkup = QUICK_ACTIONS
) -> None:
    chunks = list(message_chunks(text))
    for index, chunk in enumerate(chunks):
        await update.effective_message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup if index == len(chunks) - 1 else None,
        )


def clear_price_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and context.user_data is not None:
        context.user_data.get("price_prompts", {}).pop(update.effective_chat.id, None)


def allowed(handler: Handler) -> Handler:
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id if update.effective_user else None
        if user_id not in get_settings().telegram_allowed_user_ids:
            callback = getattr(update, "callback_query", None)
            if callback:
                await callback.answer("Доступ запрещён.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("Доступ запрещён.")
            return
        if (getattr(update.effective_message, "text", None) or "").startswith("/"):
            clear_price_prompt(update, context)
        await handler(update, context)

    return wrapper


@allowed
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_html(update, HELP, BACK_TO_MENU)


@allowed
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_html(update, MENU_TEXT, MAIN_MENU)


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


async def edit_navigation(update: Update, text: str, reply_markup: InlineKeyboardMarkup) -> None:
    try:
        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        # Repeated taps on a menu already displaying the requested screen are harmless.
        if "message is not modified" not in str(exc).lower():
            raise


@allowed
async def navigation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    callback = update.callback_query
    await callback.answer()
    clear_price_prompt(update, context)
    action = callback.data.removeprefix("nav:")
    if action == "menu":
        if update.effective_message.reply_markup == QUICK_ACTIONS:
            # Keep the report in chat when opening its navigation menu.
            await reply_html(update, MENU_TEXT, MAIN_MENU)
        else:
            await edit_navigation(update, MENU_TEXT, MAIN_MENU)
    elif action == "help":
        await edit_navigation(update, HELP, BACK_TO_MENU)
    elif action in {"top", "stores"}:
        title = "Топ товаров" if action == "top" else "Магазины"
        markup = keyboard(
            [
                [("Сегодня", f"{action}:today"), ("Неделя", f"{action}:week")],
                [("Месяц", f"{action}:month"), ("Всё время", f"{action}:all")],
                [("← Назад в меню", "menu")],
            ]
        )
        await edit_navigation(update, f"<b>{title}</b>\n\nВыберите период:", markup)
    elif action == "prices":
        await edit_navigation(
            update,
            "🏷 <b>История цены</b>\n\nНапишите название товара следующим сообщением."
            "\nНапример: <b>молоко</b>",
            BACK_TO_MENU,
        )
        context.user_data.setdefault("price_prompts", {})[update.effective_chat.id] = (
            update.effective_message.message_id
        )
    elif action in PERIOD_TITLES:
        await period_handler(action, PERIOD_TITLES[action])(update, context)
    elif action in {"last", "status", "sync"}:
        handler = {"last": last_command, "status": status_command, "sync": sync_command}[action]
        await handler(update, context)
    elif action in {
        f"{command}:{period}" for command in ("top", "stores") for period in (*PERIOD_TITLES, "all")
    }:
        command, period = action.split(":")
        context.args = [] if period == "all" else [period]
        handler = top_command if command == "top" else stores_command
        await handler(update, context)
    else:
        await reply_html(update, "Откройте меню и выберите действие.", BACK_TO_MENU)


@allowed
async def price_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prompts = context.user_data.get("price_prompts", {})
    prompt_id = prompts.get(update.effective_chat.id)
    if prompt_id is None:
        return
    message = update.effective_message
    if message.reply_to_message and message.reply_to_message.message_id != prompt_id:
        return
    query = message.text.strip()
    if not query:
        await reply_html(update, "Напишите название товара или вернитесь в меню.", BACK_TO_MENU)
        return
    clear_price_prompt(update, context)
    context.args = [query]
    await prices_command(update, context)


def build_application() -> Application:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler(["start", "menu"], menu_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("today", period_handler("today", "Сегодня")))
    application.add_handler(CommandHandler("week", period_handler("week", "Текущая неделя")))
    application.add_handler(CommandHandler("month", period_handler("month", "Текущий месяц")))
    application.add_handler(CommandHandler("top", top_command))
    application.add_handler(CommandHandler("stores", stores_command))
    application.add_handler(CommandHandler("prices", prices_command))
    application.add_handler(CommandHandler("last", last_command))
    application.add_handler(CommandHandler("sync", sync_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CallbackQueryHandler(navigation_callback, pattern=r"^nav:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, price_input))
    return application


def run_bot() -> None:
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)
