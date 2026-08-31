from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import ProcessingStatus, Receipt, SyncRun
from app.services.gmail import GmailIntegrationService
from app.services.importer import ReceiptImporter
from app.services.secrets import FernetSecretStorage


def sync_receipts(since: date | None = None) -> SyncRun:
    settings = get_settings()
    with SessionLocal() as session:
        integration = GmailIntegrationService(
            session, settings, FernetSecretStorage(settings.token_encryption_key)
        )
        messages = integration.client().fetch_messages(since)
        return ReceiptImporter(session).import_messages(messages)


def retry_failed() -> SyncRun:
    settings = get_settings()
    with SessionLocal() as session:
        integration = GmailIntegrationService(
            session, settings, FernetSecretStorage(settings.token_encryption_key)
        )
        client = integration.client()
        failed = list(
            session.scalars(select(Receipt).where(Receipt.status == ProcessingStatus.FAILED))
        )
        messages = []
        for receipt in failed:
            message = client.fetch_message(receipt.gmail_message_id)
            if message:
                session.delete(receipt)
                session.commit()
                messages.append(message)
        return ReceiptImporter(session).import_messages(messages)
