from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class ProcessingStatus(StrEnum):
    IMPORTED = "imported"
    PARSED = "parsed"
    SKIPPED = "skipped"
    FAILED = "failed"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class GmailIntegration(Base):
    __tablename__ = "gmail_integrations"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    encrypted_token: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Receipt(Base):
    __tablename__ = "receipts"
    __table_args__ = (UniqueConstraint("fiscal_fingerprint", name="uq_receipt_fiscal"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    gmail_message_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    ofd_provider: Mapped[str | None] = mapped_column(String(100))
    seller: Mapped[str | None] = mapped_column(String(255))
    store: Mapped[str | None] = mapped_column(String(100), index=True)
    inn: Mapped[str | None] = mapped_column(String(20))
    location: Mapped[str | None] = mapped_column(Text)
    purchased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    operation_type: Mapped[str | None] = mapped_column(String(40))
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    total: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    payment_method: Mapped[str | None] = mapped_column(String(100))
    fiscal_drive_number: Mapped[str | None] = mapped_column(String(64))
    fiscal_document_number: Mapped[str | None] = mapped_column(String(64))
    fiscal_sign: Mapped[str | None] = mapped_column(String(64))
    fiscal_fingerprint: Mapped[str | None] = mapped_column(String(220))
    status: Mapped[str] = mapped_column(String(20), default=ProcessingStatus.IMPORTED)
    parse_error: Mapped[str | None] = mapped_column(Text)
    source_sender: Mapped[str | None] = mapped_column(String(320))
    source_subject: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    items: Mapped[list[ReceiptItem]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan", order_by="ReceiptItem.id"
    )


class ReceiptItem(Base):
    __tablename__ = "receipt_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("receipts.id", ondelete="CASCADE"), index=True
    )
    original_name: Mapped[str] = mapped_column(Text)
    normalized_name: Mapped[str] = mapped_column(Text, index=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    unit: Mapped[str | None] = mapped_column(String(20))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    category: Mapped[str] = mapped_column(String(80), default="другое")
    vat_rate: Mapped[str | None] = mapped_column(String(30))
    receipt: Mapped[Receipt] = relationship(back_populates="items")


class ProductAlias(Base):
    __tablename__ = "product_aliases"
    id: Mapped[int] = mapped_column(primary_key=True)
    alias: Mapped[str] = mapped_column(Text, unique=True)
    normalized_name: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(80), default="другое")


class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="running")
    messages_seen: Mapped[int] = mapped_column(default=0)
    parsed: Mapped[int] = mapped_column(default=0)
    skipped: Mapped[int] = mapped_column(default=0)
    failed: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
