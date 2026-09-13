import asyncio
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree import ElementTree

from app.bot import HELP, reply_html
from app.models import Receipt, ReceiptItem
from app.schemas import EmailMessage
from app.services.importer import ReceiptImporter
from app.services.reports import (
    message_chunks,
    period_report,
    prices_report,
    receipt_report,
    stores_report,
    top_report,
)
from app.services.stats import StatsService


def assert_valid_html(text):
    root = ElementTree.fromstring(f"<root>{text}</root>")
    assert {node.tag for node in root.iter()} <= {"root", "b", "i", "code"}
    return "".join(root.itertext())


def test_imported_receipt_totals_survive_html_rendering(session, html):
    ReceiptImporter(session).import_message(
        EmailMessage("example", "ofdreceipt@beeline.ru", "Чек", html("beeline.html"))
    )
    service = StatsService(session)
    report = period_report(service.period("month", datetime(2026, 8, 28)), "Текущий месяц")
    visible = assert_valid_html(report)
    assert "Всего потрачено: 290,61 ₽" in visible
    assert "Чеков: 1" in visible
    assert "01.08.2026 — 31.08.2026" in visible
    assert "молоко 3,2% 930 мл — 2" in visible
    assert "Итого: 290,61 ₽" in assert_valid_html(receipt_report(service.last_receipt()))


def test_external_names_are_text_in_every_report(session):
    name = '<b>Молоко</b> & "сыр"\nновая строка'
    value = Decimal("1000.00")
    receipt = Receipt(store=name, total=value, items=[ReceiptItem(original_name=name, total=value)])
    stats = StatsService(session).period("today", datetime(2026, 9, 13))
    reports = [
        HELP,
        period_report(stats, name),
        prices_report(name, [(None, name, value)]),
        receipt_report(receipt),
        stores_report([(name, value, 1, value)]),
        top_report([(name, value)], [(name, value)]),
    ]
    for report in reports:
        assert_valid_html(report)
        if report != HELP:
            assert "&lt;b&gt;Молоко&lt;/b&gt; &amp; &quot;сыр&quot; новая строка" in report
    assert "1E+3" not in reports[-1]
    assert "1 000,00 ₽" in reports[-1]


def test_long_reports_send_valid_html_chunks_without_losing_rows():
    rows = [
        (f"Магазин {index} " + '😀<&"' * 200, Decimal("1"), 1, Decimal("1")) for index in range(40)
    ]
    text = stores_report(rows)
    message = SimpleNamespace(reply_text=AsyncMock())
    asyncio.run(reply_html(SimpleNamespace(effective_message=message), text))
    calls = message.reply_text.call_args_list
    assert len(calls) > 1
    visible = ""
    for call in calls:
        chunk = call.args[0]
        assert len(chunk.encode("utf-16-le")) // 2 <= 4096
        visible += assert_valid_html(chunk)
        assert call.kwargs["parse_mode"] == "HTML"
        assert call.kwargs["disable_web_page_preview"] is True
    for index in range(40):
        assert f"Магазин {index} " in visible
    assert visible.count("Всего: 1,00 ₽") == 40


def test_empty_reports_and_receipt_item_limit_are_explicit(session):
    stats = StatsService(session).period("today", datetime(2026, 9, 13))
    assert "расходов пока нет" in period_report(stats, "Сегодня")
    assert "Чеков:" not in period_report(stats, "Сегодня")
    assert "чеков пока нет" in receipt_report(None)
    assert "Ничего не найдено" in prices_report("молоко", [])
    assert "покупках пока нет" in top_report([], [])
    assert "покупках пока нет" in stores_report([])
    receipt = Receipt(
        total=Decimal("31"),
        items=[
            ReceiptItem(original_name=f"Товар {index}", total=Decimal("1")) for index in range(31)
        ],
    )
    report = receipt_report(receipt)
    assert "Показаны 30 из 31 позиций" in assert_valid_html(report)
    assert "Итого: <b>31,00 ₽</b>" in report
    assert len(list(message_chunks(report))) == 1
