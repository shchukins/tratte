from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select

from app.models import Receipt, ReceiptItem
from app.schemas import EmailMessage
from app.services.importer import ReceiptImporter
from app.services.normalization import ProductNormalizer
from app.services.stats import StatsService


def message(html, message_id="b1"):
    return EmailMessage(message_id, "ofdreceipt@beeline.ru", "Чек АГРОТОРГ", html("beeline.html"))


def test_repeated_import_is_idempotent(session, html):
    importer = ReceiptImporter(session)
    assert importer.import_message(message(html)) == "parsed"
    assert importer.import_message(message(html)) == "skipped"
    assert session.scalar(select(func.count(Receipt.id))) == 1
    assert session.scalar(select(func.count(ReceiptItem.id))) == 2


def test_fiscal_duplicate_with_other_message_id_has_no_duplicate_items(session, html):
    importer = ReceiptImporter(session)
    importer.import_message(message(html, "first"))
    importer.import_message(message(html, "copy"))
    assert session.scalar(select(func.count(Receipt.id))) == 2
    assert session.scalar(select(func.count(ReceiptItem.id))) == 2
    copy = session.scalar(select(Receipt).where(Receipt.gmail_message_id == "copy"))
    assert copy.status == "skipped"


def test_full_html_database_statistics_report_path(session, html):
    ReceiptImporter(session).import_message(message(html))
    now = datetime(2026, 8, 28, 22, 0)
    stats = StatsService(session).period("month", now)
    assert stats.total == Decimal("290.61")
    assert stats.receipt_count == 1
    assert stats.average == Decimal("290.61")
    assert stats.stores == [("Пятёрочка", Decimal("290.61"))]
    assert stats.top_by_quantity[0] == ("молоко 3,2% 930 мл", Decimal("2.000"))


def test_failed_message_is_recorded(session):
    result = ReceiptImporter(session).import_message(
        EmailMessage("bad", "unknown@example.test", "not a receipt", "<p>nothing</p>")
    )
    assert result == "failed"
    receipt = session.scalar(select(Receipt))
    assert receipt.status == "failed"
    assert receipt.parse_error


def test_manual_alias_updates_existing_items(session, html):
    ReceiptImporter(session).import_message(message(html))
    ProductNormalizer(session).set_alias(
        "[М+] Молоко 3,2% 930 мл", "молоко питьевое 3,2%", "молочные продукты"
    )
    session.commit()
    item = session.scalar(
        select(ReceiptItem).where(ReceiptItem.original_name == "[М+] Молоко 3,2% 930 мл")
    )
    assert item.normalized_name == "молоко питьевое 3,2%"
    assert item.category == "молочные продукты"
