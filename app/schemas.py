from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(slots=True)
class ParsedItem:
    original_name: str
    unit_price: Decimal
    quantity: Decimal
    total: Decimal
    unit: str | None = None
    vat_rate: str | None = None


@dataclass(slots=True)
class ParsedReceipt:
    ofd_provider: str
    seller: str | None
    store: str | None
    inn: str | None
    location: str | None
    purchased_at: datetime | None
    operation_type: str | None
    currency: str
    total: Decimal
    payment_method: str | None
    fiscal_drive_number: str | None
    fiscal_document_number: str | None
    fiscal_sign: str | None
    items: list[ParsedItem] = field(default_factory=list)

    @property
    def fiscal_fingerprint(self) -> str | None:
        parts = (self.fiscal_drive_number, self.fiscal_document_number, self.fiscal_sign)
        return ":".join(parts) if all(parts) else None


@dataclass(slots=True)
class EmailMessage:
    message_id: str
    sender: str
    subject: str
    html: str
