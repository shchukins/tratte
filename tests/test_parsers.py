from decimal import Decimal

import pytest

from app.parsers.base import ReceiptParseError
from app.parsers.receipts import ParserRegistry
from app.schemas import EmailMessage


def test_beeline_parser_extracts_weight_fiscal_data_and_ignores_ad(html):
    result = ParserRegistry().parse(
        EmailMessage("b1", "ofdreceipt@beeline.ru", "Чек ООО АГРОТОРГ", html("beeline.html"))
    )
    assert result.ofd_provider == "Билайн ОФД"
    assert result.store == "Пятёрочка"
    assert result.total == Decimal("290.61")
    assert result.fiscal_fingerprint == "9999000011112222:12345:987654321"
    assert len(result.items) == 2
    assert result.items[1].quantity == Decimal("0.740")
    assert all("реклам" not in item.original_name.casefold() for item in result.items)


def test_first_ofd_parser(html):
    result = ParserRegistry().parse(
        EmailMessage(
            "f1", "echeck@1-ofd.ru", "АО Вкусвилл 29.08.2026 13:01", html("first_ofd.html")
        )
    )
    assert result.ofd_provider == "Первый ОФД"
    assert result.store == "ВкусВилл"
    assert result.total == Decimal("328.50")
    assert [item.original_name for item in result.items] == [
        "Салат овощной 200 г,шт",
        "Напиток овсяный ваниль,шт",
    ]


def test_magnit_parser_recognizes_sender_and_store(html):
    result = ParserRegistry().parse(
        EmailMessage("m1", "info@ofd-magnit.ru", "Ваш чек", html("magnit_generic.html"))
    )
    assert result.ofd_provider == "Магнит ОФД"
    assert result.store == "Магнит"
    assert result.total == Decimal("75.00")
    assert result.seller == 'АО "Тандер"'
    assert result.location == "000000, г. Тестов, ул. Обезличенная, дом 3"
    assert result.payment_method == "безналичный"
    assert result.fiscal_fingerprint == "7777000011112222:777:555444333"
    assert result.items[0].unit == "шт. или ед."
    assert result.items[0].vat_rate == "НДС 10%"


def test_unknown_format_fails_without_stopping_registry():
    with pytest.raises(ReceiptParseError):
        ParserRegistry().parse(EmailMessage("x", "unknown@example.test", "hello", "<p>hello</p>"))
