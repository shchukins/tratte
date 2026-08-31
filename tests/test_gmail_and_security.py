import asyncio
import logging
from types import SimpleNamespace

from app.bot import allowed
from app.cli import _logging
from app.config import Settings
from app.services.gmail import GmailClient


def test_csv_environment_settings(monkeypatch):
    monkeypatch.setenv("GMAIL_SENDERS", "a@example.test,b@example.test")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1,2")
    settings = Settings(_env_file=None)
    assert settings.gmail_senders == ["a@example.test", "b@example.test"]
    assert settings.telegram_allowed_user_ids == {1, 2}


def test_http_client_request_urls_are_not_logged_at_info():
    _logging()
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


def test_gmail_query_uses_label_not_inbox():
    settings = Settings(
        gmail_label="чеки",
        gmail_senders=["a@example.test", "b@example.test", "info@ofd-magnit.ru"],
    )
    query = GmailClient(None, settings).query()
    assert 'label:"чеки"' in query
    assert "from:a@example.test" in query
    assert "from:info@ofd-magnit.ru" in query
    assert "in:inbox" not in query
    assert "in:trash" not in query


def test_telegram_allowlist_denies_unknown_user(monkeypatch):
    settings = Settings(telegram_allowed_user_ids={42})
    monkeypatch.setattr("app.bot.get_settings", lambda: settings)
    replies = []

    class Message:
        async def reply_text(self, text):
            replies.append(text)

    update = SimpleNamespace(effective_user=SimpleNamespace(id=7), effective_message=Message())
    called = []

    @allowed
    async def handler(update, context):
        called.append(True)

    asyncio.run(handler(update, None))
    assert not called
    assert replies == ["Доступ запрещён."]
