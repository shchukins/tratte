import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

from app.bot import (
    BACK_TO_MENU,
    MAIN_MENU,
    QUICK_ACTIONS,
    build_application,
    menu_command,
    navigation_callback,
    price_input,
    reply_html,
)


@pytest.fixture
def bot_context():
    settings = SimpleNamespace(
        telegram_allowed_user_ids={1},
        default_timezone="Europe/Moscow",
        telegram_bot_token="123456:test-token",
    )
    with patch("app.bot.get_settings", return_value=settings):
        yield SimpleNamespace(args=[], user_data={})


def make_update(action=None, text=None, user=1, chat=10, reply_to=None):
    message = SimpleNamespace(
        text=text,
        message_id=100,
        reply_to_message=reply_to,
        reply_markup=MAIN_MENU,
        reply_text=AsyncMock(),
    )
    callback = (
        None
        if action is None
        else SimpleNamespace(
            data=f"nav:{action}",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user),
        effective_chat=SimpleNamespace(id=chat),
        effective_message=message,
        callback_query=callback,
    )


def actions(markup):
    return {button.callback_data for row in markup.inline_keyboard for button in row}


def test_start_menu_and_registered_handlers(bot_context):
    update = make_update(text="/start")
    asyncio.run(menu_command(update, bot_context))
    assert update.effective_message.reply_text.call_args.kwargs["reply_markup"] == MAIN_MENU
    app = build_application()
    handlers = app.handlers[0]
    commands = {
        command
        for handler in handlers
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }
    assert commands == {
        "start",
        "menu",
        "help",
        "today",
        "week",
        "month",
        "top",
        "stores",
        "prices",
        "last",
        "sync",
        "status",
    }
    assert any(isinstance(handler, CallbackQueryHandler) for handler in handlers)
    assert any(isinstance(handler, MessageHandler) for handler in handlers)


@pytest.mark.parametrize("action", ["top", "stores"])
def test_period_menu_and_back_edit_same_message(bot_context, action):
    update = make_update(action)
    asyncio.run(navigation_callback(update, bot_context))
    markup = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
    assert actions(markup) == {
        f"nav:{action}:{period}" for period in ("today", "week", "month", "all")
    } | {"nav:menu"}
    update.callback_query.data = "nav:menu"
    asyncio.run(navigation_callback(update, bot_context))
    assert update.callback_query.edit_message_text.call_args.kwargs["reply_markup"] == MAIN_MENU
    update.effective_message.reply_text.assert_not_awaited()
    assert update.callback_query.answer.await_count == 2


@pytest.mark.parametrize("command", ["top", "stores"])
@pytest.mark.parametrize("period", ["today", "week", "month", "all"])
def test_period_buttons_dispatch_existing_commands(bot_context, command, period):
    update = make_update(f"{command}:{period}")
    with patch(f"app.bot.{command}_command", new_callable=AsyncMock) as handler:
        asyncio.run(navigation_callback(update, bot_context))
    handler.assert_awaited_once_with(update, bot_context)
    assert bot_context.args == ([] if period == "all" else [period])


@pytest.mark.parametrize("command", ["last", "status", "sync"])
def test_action_buttons_dispatch_existing_commands(bot_context, command):
    update = make_update(command)
    with patch(f"app.bot.{command}_command", new_callable=AsyncMock) as handler:
        asyncio.run(navigation_callback(update, bot_context))
    handler.assert_awaited_once_with(update, bot_context)
    update.callback_query.answer.assert_awaited_once()


@pytest.mark.parametrize("period", ["today", "week", "month"])
def test_expense_buttons_dispatch_correct_period(bot_context, period):
    update = make_update(period)
    handler = AsyncMock()
    with patch("app.bot.period_handler", return_value=handler) as factory:
        asyncio.run(navigation_callback(update, bot_context))
    assert factory.call_args.args[0] == period
    handler.assert_awaited_once_with(update, bot_context)


def test_prices_accept_text_once_in_origin_chat(bot_context):
    prompt = make_update("prices")
    asyncio.run(navigation_callback(prompt, bot_context))
    assert prompt.callback_query.edit_message_text.call_args.kwargs["reply_markup"] == BACK_TO_MENU
    with patch("app.bot.prices_command", new_callable=AsyncMock) as handler:
        asyncio.run(price_input(make_update(text="молоко", chat=20), bot_context))
        asyncio.run(
            price_input(
                make_update(text="молоко", reply_to=SimpleNamespace(message_id=99)), bot_context
            )
        )
        handler.assert_not_awaited()
        update = make_update(text="  молоко 3,2%  ")
        asyncio.run(price_input(update, bot_context))
        handler.assert_awaited_once_with(update, bot_context)
        assert bot_context.args == ["молоко 3,2%"]
        asyncio.run(price_input(make_update(text="хлеб"), bot_context))
        assert handler.await_count == 1
    assert bot_context.user_data["price_prompts"] == {}


@pytest.mark.parametrize("cancel", ["button", "command"])
def test_leaving_prices_cancels_text_input(bot_context, cancel):
    asyncio.run(navigation_callback(make_update("prices"), bot_context))
    if cancel == "button":
        asyncio.run(navigation_callback(make_update("menu"), bot_context))
    else:
        asyncio.run(menu_command(make_update(text="/menu"), bot_context))
    with patch("app.bot.prices_command", new_callable=AsyncMock) as handler:
        asyncio.run(price_input(make_update(text="молоко"), bot_context))
    handler.assert_not_awaited()


def test_prices_are_scoped_to_user(bot_context):
    asyncio.run(navigation_callback(make_update("prices"), bot_context))
    second_context = SimpleNamespace(args=[], user_data={})
    with patch("app.bot.prices_command", new_callable=AsyncMock) as handler:
        asyncio.run(price_input(make_update(text="хлеб"), second_context))
    handler.assert_not_awaited()
    assert bot_context.user_data["price_prompts"] == {10: 100}


def test_unauthorized_buttons_and_text_never_query_database(bot_context):
    update = make_update("sync", user=99)
    bot_context.user_data["price_prompts"] = {10: 100}
    text_update = make_update(text="молоко", user=99)
    with patch("app.bot.SessionLocal") as database, patch("app.bot.sync_receipts") as sync:
        asyncio.run(navigation_callback(update, bot_context))
        asyncio.run(price_input(text_update, bot_context))
    database.assert_not_called()
    sync.assert_not_called()
    update.callback_query.answer.assert_awaited_once_with("Доступ запрещён.", show_alert=True)
    update.callback_query.edit_message_text.assert_not_awaited()
    text_update.effective_message.reply_text.assert_awaited_once_with("Доступ запрещён.")


def test_long_report_has_buttons_only_on_last_chunk(bot_context):
    update = make_update()
    with patch("app.bot.message_chunks", return_value=["часть 1", "часть 2"]):
        asyncio.run(reply_html(update, "отчёт"))
    calls = update.effective_message.reply_text.call_args_list
    assert calls[0].kwargs["reply_markup"] is None
    assert calls[1].kwargs["reply_markup"] == QUICK_ACTIONS


@pytest.mark.parametrize(
    "error,raises",
    [
        ("Message is not modified", False),
        ("Message to edit not found", True),
    ],
)
def test_repeated_menu_taps_ignore_only_unchanged_message(bot_context, error, raises):
    update = make_update("menu")
    update.callback_query.edit_message_text.side_effect = BadRequest(error)
    if raises:
        with pytest.raises(BadRequest):
            asyncio.run(navigation_callback(update, bot_context))
    else:
        asyncio.run(navigation_callback(update, bot_context))


def test_opening_menu_preserves_report(bot_context):
    update = make_update("menu")
    update.effective_message.reply_markup = QUICK_ACTIONS
    asyncio.run(navigation_callback(update, bot_context))
    update.callback_query.edit_message_text.assert_not_awaited()
    assert update.effective_message.reply_text.call_args.kwargs["reply_markup"] == MAIN_MENU


def test_status_button_renders_database_report(bot_context, session):
    update = make_update("status")
    with patch("app.bot.SessionLocal", return_value=session):
        asyncio.run(navigation_callback(update, bot_context))
    reply = update.effective_message.reply_text.call_args
    assert "Синхронизация ещё не запускалась" in reply.args[0]
    assert reply.kwargs["parse_mode"] == "HTML"
    assert reply.kwargs["reply_markup"] == QUICK_ACTIONS
    update.callback_query.edit_message_text.assert_not_awaited()


def test_price_text_renders_database_report_and_escapes_query(bot_context, session):
    asyncio.run(navigation_callback(make_update("prices"), bot_context))
    update = make_update(text="молоко <3>")
    with patch("app.bot.SessionLocal", return_value=session):
        asyncio.run(price_input(update, bot_context))
    reply = update.effective_message.reply_text.call_args
    assert "молоко &lt;3&gt;" in reply.args[0]
    assert "Ничего не найдено" in reply.args[0]
    assert reply.kwargs["reply_markup"] == QUICK_ACTIONS
