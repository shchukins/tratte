from __future__ import annotations

from abc import ABC, abstractmethod

from app.schemas import EmailMessage, ParsedReceipt


class ReceiptParseError(ValueError):
    pass


class ReceiptParser(ABC):
    @abstractmethod
    def supports(self, message: EmailMessage) -> bool: ...

    @abstractmethod
    def parse(self, message: EmailMessage) -> ParsedReceipt: ...
