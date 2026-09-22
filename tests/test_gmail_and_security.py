import asyncio
import base64
import logging
from types import SimpleNamespace
from unittest.mock import Mock

from googleapiclient.errors import HttpError

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


def test_gmail_fetches_full_content_only_for_new_message_ids():
    messages_api = Mock()
    messages_api.list.return_value.execute.return_value = {
        "messages": [{"id": "known"}, {"id": "new"}]
    }
    messages_api.get.return_value.execute.return_value = {
        "payload": {
            "headers": [
                {"name": "From", "value": "OFD <ofd@example.test>"},
                {"name": "Subject", "value": "Receipt"},
            ],
            "mimeType": "text/html",
            "body": {"data": base64.urlsafe_b64encode(b"<p>receipt</p>").decode()},
        }
    }
    service = Mock()
    service.users.return_value.messages.return_value = messages_api

    messages = list(
        GmailClient(service, Settings(_env_file=None)).iter_messages(exclude_ids={"known"})
    )

    assert [message.message_id for message in messages] == ["new"]
    messages_api.get.assert_called_once_with(userId="me", id="new", format="full")


def test_gmail_rate_limit_is_retried_without_exposing_request(monkeypatch):
    error = HttpError(
        SimpleNamespace(status=403, reason="Forbidden"),
        b'{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}',
    )
    request = Mock()
    request.execute.side_effect = [error, {"messages": []}]
    sleeps = []
    monkeypatch.setattr("app.services.gmail.time.sleep", sleeps.append)

    response = GmailClient(None, Settings(_env_file=None))._execute(request)

    assert response == {"messages": []}
    assert sleeps == [1]
    assert request.execute.call_count == 2


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
