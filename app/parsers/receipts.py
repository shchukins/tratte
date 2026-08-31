from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from app.config import get_settings
from app.parsers.base import ReceiptParseError, ReceiptParser
from app.schemas import EmailMessage, ParsedItem, ParsedReceipt


def _decimal(value: str) -> Decimal:
    cleaned = re.sub(r"[^0-9,.-]", "", value).replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ReceiptParseError(f"Некорректное число: {value!r}") from exc


def _text(node: Tag | None) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def _lines(soup: BeautifulSoup) -> list[str]:
    for unwanted in soup.select("script, style, .advertisement, .promo, [data-ad]"):
        unwanted.decompose()
    return [" ".join(value.split()) for value in soup.stripped_strings if value.strip()]


def _payment_from_lines(lines: list[str]) -> str | None:
    for label, method in (("БЕЗНАЛИЧНЫ", "безналичный"), ("НАЛИЧНЫ", "наличный")):
        for index, line in enumerate(lines):
            if line.upper().startswith(label) and index + 1 < len(lines):
                try:
                    if _decimal(lines[index + 1]) > 0:
                        return method
                except ReceiptParseError:
                    continue
    return None


class StructuredHtmlParser(ReceiptParser):
    sender: str | None = None
    provider = "unknown"

    def supports(self, message: EmailMessage) -> bool:
        return self.sender is None or self.sender.lower() in message.sender.lower()

    def parse(self, message: EmailMessage) -> ParsedReceipt:
        soup = BeautifulSoup(message.html, "html.parser")
        for unwanted in soup.select("script, style, .advertisement, .promo, [data-ad]"):
            unwanted.decompose()
        full_text = soup.get_text("\n", strip=True)
        items = self._items(soup)
        total = self._field_decimal(full_text, [r"(?:итог|итого|всего)\s*[:№]?\s*([\d ,.]+)"])
        if total is None or not items:
            raise ReceiptParseError("Не найдены итог или товарные позиции")

        seller = self._field(full_text, [r"(?:продавец|пользователь)\s*[:№]?\s*([^\n]+)"])
        store = self._store(seller, message.subject, full_text)
        purchased_at = self._date(full_text) or self._date(message.subject)
        return ParsedReceipt(
            ofd_provider=self.provider,
            seller=seller,
            store=store,
            inn=self._field(full_text, [r"(?:инн)\s*[:№]?\s*(\d{10,12})"]),
            location=self._field(
                full_text, [r"(?:место расч[её]та|адрес расч[её]та|адрес)\s*[:]?\s*([^\n]+)"]
            ),
            purchased_at=purchased_at,
            operation_type=self._field(
                full_text, [r"(?:признак расч[её]та|операция)\s*[:]?\s*([^\n]+)"]
            ),
            currency="RUB",
            total=total,
            payment_method=self._field(
                full_text, [r"(?:форма расч[её]та|оплата)\s*[:]?\s*([^\n]+)"]
            ),
            fiscal_drive_number=self._field(full_text, [r"(?:фн|фн №)\s*[:№]?\s*(\d{8,})"]),
            fiscal_document_number=self._field(full_text, [r"(?:фд|фд №)\s*[:№]?\s*(\d+)"]),
            fiscal_sign=self._field(full_text, [r"(?:фпд|фп|фпд №)\s*[:№]?\s*(\d+)"]),
            items=items,
        )

    def _items(self, soup: BeautifulSoup) -> list[ParsedItem]:
        result: list[ParsedItem] = []
        nodes = soup.select("[data-receipt-item], .receipt-item, tr.item")
        for node in nodes:
            name = _text(node.select_one("[data-name], .name, .item-name"))
            price = _text(node.select_one("[data-price], .price, .item-price"))
            quantity = _text(node.select_one("[data-quantity], .quantity, .qty")) or "1"
            amount = _text(node.select_one("[data-total], .sum, .amount, .item-total"))
            unit = _text(node.select_one("[data-unit], .unit")) or None
            vat = _text(node.select_one("[data-vat], .vat")) or None
            if not name and node.name == "tr":
                cells = [_text(cell) for cell in node.select("td")]
                if len(cells) >= 4:
                    name, price, quantity, amount = cells[:4]
            if not name or not price or not amount:
                continue
            lowered = name.casefold()
            if any(word in lowered for word in ("реклама", "акция", "подписывайтесь")):
                continue
            result.append(
                ParsedItem(
                    original_name=name,
                    unit_price=_decimal(price),
                    quantity=_decimal(quantity),
                    total=_decimal(amount),
                    unit=unit,
                    vat_rate=vat,
                )
            )
        return result

    @staticmethod
    def _field(text: str, patterns: list[str]) -> str | None:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return " ".join(match.group(1).split()).strip()
        return None

    def _field_decimal(self, text: str, patterns: list[str]) -> Decimal | None:
        value = self._field(text, patterns)
        return _decimal(value) if value else None

    @staticmethod
    def _date(text: str) -> datetime | None:
        match = re.search(r"(\d{2}\.\d{2}\.\d{4})(?:\s+[г.]*)?\s*(\d{2}:\d{2}(?::\d{2})?)?", text)
        if not match:
            return None
        value = f"{match.group(1)} {match.group(2) or '00:00'}"
        fmt = "%d.%m.%Y %H:%M:%S" if value.count(":") == 2 else "%d.%m.%Y %H:%M"
        return datetime.strptime(value, fmt).replace(
            tzinfo=ZoneInfo(get_settings().default_timezone)
        )

    @staticmethod
    def _store(seller: str | None, subject: str, text: str) -> str | None:
        haystack = " ".join(filter(None, [seller, subject, text[:500]])).casefold()
        names = {
            "пятёроч": "Пятёрочка",
            "пятероч": "Пятёрочка",
            "агроторг": "Пятёрочка",
            "вкусвилл": "ВкусВилл",
            "магнит": "Магнит",
            "тандер": "Магнит",
        }
        return next((label for needle, label in names.items() if needle in haystack), seller)


class BeelineOfdParser(StructuredHtmlParser):
    sender = "ofdreceipt@beeline.ru"
    provider = "Билайн ОФД"

    def parse(self, message: EmailMessage) -> ParsedReceipt:
        parsed = super().parse(message)
        lines = _lines(BeautifulSoup(message.html, "html.parser"))
        seller_index = next(
            (
                index
                for index, line in enumerate(lines)
                if re.fullmatch(r'(?:АО|ООО)\s+"[^"]+"', line)
            ),
            None,
        )
        if seller_index is not None:
            parsed.seller = lines[seller_index]
            if seller_index + 1 < len(lines):
                parsed.location = lines[seller_index + 1]
        parsed.operation_type = (
            "ПРИХОД" if any(line.casefold() == "приход" for line in lines) else None
        )
        parsed.payment_method = _payment_from_lines(lines) or parsed.payment_method
        return parsed

    def _items(self, soup: BeautifulSoup) -> list[ParsedItem]:
        items = super()._items(soup)
        if items:
            return items
        lines = _lines(soup)
        equation = re.compile(r"^([\d.,]+)\s*\*\s*([\d.,]+)\s*([^\s=]+)\s*=\s*([\d.,]+)$")
        for index, line in enumerate(lines):
            match = equation.fullmatch(line)
            if not match:
                continue
            name_index = index - 1
            while name_index >= 0 and (
                lines[name_index] == "Цена * Кол"
                or lines[name_index] == "."
                or re.fullmatch(r"\d+", lines[name_index])
            ):
                name_index -= 1
            if name_index < 0:
                continue
            vat = (
                lines[index + 1]
                if index + 1 < len(lines) and lines[index + 1].startswith("НДС")
                else None
            )
            items.append(
                ParsedItem(
                    original_name=lines[name_index],
                    unit_price=_decimal(match.group(1)),
                    quantity=_decimal(match.group(2)),
                    unit=match.group(3),
                    total=_decimal(match.group(4)),
                    vat_rate=vat,
                )
            )
        return items


class FirstOfdParser(StructuredHtmlParser):
    sender = "echeck@1-ofd.ru"
    provider = "Первый ОФД"

    def parse(self, message: EmailMessage) -> ParsedReceipt:
        parsed = super().parse(message)
        lines = _lines(BeautifulSoup(message.html, "html.parser"))
        parsed.seller = next(
            (line for line in lines if re.fullmatch(r'(?:АО|ООО)\s+"[^"]+"', line)),
            parsed.seller,
        )
        parsed.location = next(
            (line for line in lines if re.match(r"^\d{6},\s+", line)), parsed.location
        )
        parsed.operation_type = (
            "ПРИХОД" if any(line.casefold() == "приход" for line in lines) else None
        )
        parsed.payment_method = _payment_from_lines(lines) or parsed.payment_method
        return parsed

    def _items(self, soup: BeautifulSoup) -> list[ParsedItem]:
        items = super()._items(soup)
        if items:
            return items
        lines = _lines(soup)
        try:
            start = lines.index("Наименование")
        except ValueError:
            return items
        end = next(
            (index for index in range(start + 1, len(lines)) if lines[index].startswith("ИТОГО")),
            len(lines),
        )
        numeric = re.compile(r"^[\d.,]+$")
        for index in range(start + 1, end - 4):
            if not re.fullmatch(r"\d+\.", lines[index]):
                continue
            name, price, quantity, amount = lines[index + 1 : index + 5]
            if not all(numeric.fullmatch(value) for value in (price, quantity, amount)):
                continue
            unit_match = re.search(r",\s*(шт|кг|г|л|мл)\.?$", name, flags=re.IGNORECASE)
            vat = next(
                (value for value in lines[index + 5 : index + 10] if value.startswith("НДС")),
                None,
            )
            items.append(
                ParsedItem(
                    original_name=name,
                    unit_price=_decimal(price),
                    quantity=_decimal(quantity),
                    unit=unit_match.group(1) if unit_match else None,
                    total=_decimal(amount),
                    vat_rate=vat,
                )
            )
        return items


class MagnitOfdParser(StructuredHtmlParser):
    sender = "info@ofd-magnit.ru"
    provider = "Магнит ОФД"

    def parse(self, message: EmailMessage) -> ParsedReceipt:
        parsed = super().parse(message)
        soup = BeautifulSoup(message.html, "html.parser")
        lines = _lines(soup)
        parsed.seller = next(
            (line for line in lines if re.fullmatch(r'(?:АО|ООО)\s+"[^"]+"', line)),
            parsed.seller,
        )
        inn_index = next(
            (index for index, line in enumerate(lines) if line.startswith("ИНН ")), None
        )
        if inn_index is not None and inn_index + 1 < len(lines):
            parsed.location = lines[inn_index + 1]
        parsed.operation_type = "ПРИХОД" if "ПРИХОД" in lines else parsed.operation_type
        parsed.payment_method = _payment_from_lines(lines) or parsed.payment_method
        return parsed

    def _items(self, soup: BeautifulSoup) -> list[ParsedItem]:
        items = super()._items(soup)
        if items:
            return items
        for row in soup.find_all("tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) != 4:
                continue
            values = [_text(cell) for cell in cells]
            if not values[0] or not all(
                re.fullmatch(r"\d+(?:[.,]\d+)?", value) for value in values[1:]
            ):
                continue
            vat_rate = None
            unit = None
            for sibling in row.find_next_siblings("tr", limit=8):
                sibling_cells = sibling.find_all("td", recursive=False)
                sibling_values = [_text(cell) for cell in sibling_cells]
                if len(sibling_values) == 4 and all(
                    re.fullmatch(r"\d+(?:[.,]\d+)?", value) for value in sibling_values[1:]
                ):
                    break
                if sibling_values and sibling_values[0].upper().startswith("НДС"):
                    vat_rate = sibling_values[0]
                if sibling_values and sibling_values[0].upper().startswith("МЕРА КОЛИЧЕСТВА"):
                    unit = sibling_values[1] if len(sibling_values) > 1 else None
            items.append(
                ParsedItem(
                    original_name=values[0],
                    quantity=_decimal(values[1]),
                    unit_price=_decimal(values[2]),
                    total=_decimal(values[3]),
                    unit=unit,
                    vat_rate=vat_rate,
                )
            )
        return items


class GenericReceiptParser(StructuredHtmlParser):
    provider = "Неизвестный ОФД"

    def supports(self, message: EmailMessage) -> bool:
        text = BeautifulSoup(message.html, "html.parser").get_text(" ", strip=True).casefold()
        return "итог" in text and any(marker in text for marker in ("фн", "фд", "кассовый чек"))


class ParserRegistry:
    def __init__(self, parsers: list[ReceiptParser] | None = None):
        self.parsers = parsers or [
            BeelineOfdParser(),
            FirstOfdParser(),
            MagnitOfdParser(),
            GenericReceiptParser(),
        ]

    def parse(self, message: EmailMessage) -> ParsedReceipt:
        for parser in self.parsers:
            if parser.supports(message):
                return parser.parse(message)
        raise ReceiptParseError(f"Неизвестный формат письма от {message.sender}")
