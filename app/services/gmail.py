from __future__ import annotations

import base64
import json
from datetime import date
from email.utils import parseaddr
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import GmailIntegration
from app.schemas import EmailMessage
from app.services.secrets import SecretStorage

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _html_part(payload: dict[str, Any]) -> str:
    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        return _decode(payload["body"]["data"])
    for part in payload.get("parts", []):
        html = _html_part(part)
        if html:
            return html
    return ""


class GmailClient:
    def __init__(self, service: Any, settings: Settings):
        self.service = service
        self.settings = settings

    def query(self, since: date | None = None) -> str:
        parts = [f'label:"{self.settings.gmail_label}"'] if self.settings.gmail_label else []
        if self.settings.gmail_senders:
            senders = " ".join(f"from:{sender}" for sender in self.settings.gmail_senders)
            parts.append("{" + senders + "}")
        if since:
            parts.append(f"after:{since.strftime('%Y/%m/%d')}")
        return " ".join(parts)

    def fetch_messages(self, since: date | None = None) -> list[EmailMessage]:
        ids: list[str] = []
        page_token = None
        while True:
            response = (
                self.service.users()
                .messages()
                .list(userId="me", q=self.query(since), pageToken=page_token, maxResults=500)
                .execute()
            )
            ids.extend(row["id"] for row in response.get("messages", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return [message for message_id in ids if (message := self.fetch_message(message_id))]

    def fetch_message(self, message_id: str) -> EmailMessage | None:
        raw = (
            self.service.users().messages().get(userId="me", id=message_id, format="full").execute()
        )
        headers = {h["name"].lower(): h["value"] for h in raw["payload"].get("headers", [])}
        html = _html_part(raw["payload"])
        if not html:
            return None
        return EmailMessage(
            message_id=message_id,
            sender=parseaddr(headers.get("from", ""))[1],
            subject=headers.get("subject", ""),
            html=html,
        )


class GmailIntegrationService:
    def __init__(self, session: Session, settings: Settings, secrets: SecretStorage):
        self.session = session
        self.settings = settings
        self.secrets = secrets

    def authorize_interactively(
        self,
        host: str = "localhost",
        bind_address: str | None = None,
        port: int = 0,
        open_browser: bool = True,
    ) -> GmailIntegration:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.settings.gmail_client_secret_file), [GMAIL_READONLY_SCOPE]
        )
        credentials = flow.run_local_server(
            host=host,
            bind_addr=bind_address,
            port=port,
            open_browser=open_browser,
            access_type="offline",
            prompt="consent",
        )
        from googleapiclient.discovery import build

        profile = (
            build("gmail", "v1", credentials=credentials).users().getProfile(userId="me").execute()
        )
        email = profile["emailAddress"]
        encrypted = self.secrets.encrypt(credentials.to_json())
        integration = self.session.scalar(
            select(GmailIntegration).where(GmailIntegration.email == email)
        )
        if integration is None:
            integration = GmailIntegration(email=email, encrypted_token=encrypted)
            self.session.add(integration)
        else:
            integration.encrypted_token = encrypted
        self.session.commit()
        return integration

    def client(self) -> GmailClient:
        integration = self.session.scalar(select(GmailIntegration).order_by(GmailIntegration.id))
        if integration is None:
            raise RuntimeError("Gmail не подключён. Сначала выполните: app gmail-auth")
        info = json.loads(self.secrets.decrypt(integration.encrypted_token))
        credentials = Credentials.from_authorized_user_info(info, [GMAIL_READONLY_SCOPE])
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            integration.encrypted_token = self.secrets.encrypt(credentials.to_json())
            self.session.commit()
        from googleapiclient.discovery import build

        return GmailClient(build("gmail", "v1", credentials=credentials), self.settings)
