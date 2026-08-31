from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ProcessingStatus, Receipt, ReceiptItem, SyncRun, utc_now
from app.parsers.base import ReceiptParseError
from app.parsers.receipts import ParserRegistry
from app.schemas import EmailMessage
from app.services.normalization import ProductNormalizer

logger = logging.getLogger(__name__)


class ReceiptImporter:
    def __init__(self, session: Session, registry: ParserRegistry | None = None):
        self.session = session
        self.registry = registry or ParserRegistry()

    def import_message(self, message: EmailMessage) -> str:
        if self.session.scalar(
            select(Receipt.id).where(Receipt.gmail_message_id == message.message_id)
        ):
            return ProcessingStatus.SKIPPED

        receipt = Receipt(
            gmail_message_id=message.message_id,
            source_sender=message.sender,
            source_subject=message.subject[:500],
            status=ProcessingStatus.IMPORTED,
        )
        self.session.add(receipt)
        self.session.flush()
        try:
            parsed = self.registry.parse(message)
            if parsed.fiscal_fingerprint:
                duplicate = self.session.scalar(
                    select(Receipt.id).where(
                        Receipt.fiscal_fingerprint == parsed.fiscal_fingerprint,
                        Receipt.id != receipt.id,
                    )
                )
                if duplicate:
                    receipt.status = ProcessingStatus.SKIPPED
                    receipt.parse_error = f"Дубликат чека receipt_id={duplicate}"
                    self.session.commit()
                    return ProcessingStatus.SKIPPED
            for field in (
                "ofd_provider",
                "seller",
                "store",
                "inn",
                "location",
                "purchased_at",
                "operation_type",
                "currency",
                "total",
                "payment_method",
                "fiscal_drive_number",
                "fiscal_document_number",
                "fiscal_sign",
                "fiscal_fingerprint",
            ):
                setattr(receipt, field, getattr(parsed, field))
            normalizer = ProductNormalizer(self.session)
            for item in parsed.items:
                product = normalizer.normalize(item.original_name)
                receipt.items.append(
                    ReceiptItem(
                        original_name=item.original_name,
                        normalized_name=product.name,
                        unit_price=item.unit_price,
                        quantity=item.quantity,
                        unit=item.unit,
                        total=item.total,
                        category=product.category,
                        vat_rate=item.vat_rate,
                    )
                )
            receipt.status = ProcessingStatus.PARSED
            receipt.parse_error = None
            receipt.updated_at = utc_now()
            self.session.commit()
            return ProcessingStatus.PARSED
        except (ReceiptParseError, ValueError) as exc:
            receipt.status = ProcessingStatus.FAILED
            receipt.parse_error = str(exc)[:2000]
            self.session.commit()
            logger.warning("Не удалось разобрать Gmail message id=%s: %s", message.message_id, exc)
            return ProcessingStatus.FAILED
        except IntegrityError:
            self.session.rollback()
            logger.info("Чек уже импортирован: Gmail message id=%s", message.message_id)
            return ProcessingStatus.SKIPPED

    def import_messages(self, messages: list[EmailMessage]) -> SyncRun:
        run = SyncRun()
        self.session.add(run)
        self.session.commit()
        try:
            for message in messages:
                run.messages_seen += 1
                result = self.import_message(message)
                if result == ProcessingStatus.PARSED:
                    run.parsed += 1
                elif result == ProcessingStatus.SKIPPED:
                    run.skipped += 1
                else:
                    run.failed += 1
            run.status = "completed"
        except Exception as exc:
            logger.exception("Синхронизация завершилась с ошибкой")
            run.status = "failed"
            run.error = str(exc)[:2000]
        finally:
            run.finished_at = utc_now()
            self.session.commit()
        return run
