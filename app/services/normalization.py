from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ProductAlias, ReceiptItem

CATEGORY_RULES = {
    "молочные продукты": ("молок", "кефир", "йогурт", "творог", "сыр", "сметан"),
    "напитки": ("вода", "сок", "напиток", "кофе", "чай", "кола"),
    "снеки": ("чипс", "сухар", "орех"),
    "овощи и фрукты": ("яблок", "банан", "томат", "огур", "картоф"),
    "мясо и рыба": ("куриц", "индей", "говяд", "свинин", "рыб", "лосос"),
    "хлеб и выпечка": ("хлеб", "булоч", "багет", "круассан"),
    "готовая еда": ("салат", "суп", "сэндвич", "ролл", "пицц"),
    "бытовые товары": ("салфет", "порошок", "мыло", "шампун", "бумага"),
}


@dataclass(frozen=True)
class NormalizedProduct:
    name: str
    category: str


class ProductNormalizer:
    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def clean(name: str) -> str:
        value = re.sub(r"^\s*\d+[.)-]?\s*", "", name)
        value = re.sub(r"^\s*\[м\+\]\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s+", " ", value).strip().casefold()
        return value

    def normalize(self, original_name: str) -> NormalizedProduct:
        clean = self.clean(original_name)
        alias = self.session.scalar(select(ProductAlias).where(ProductAlias.alias == clean))
        if alias:
            return NormalizedProduct(alias.normalized_name, alias.category)
        category = next(
            (
                category
                for category, needles in CATEGORY_RULES.items()
                if any(n in clean for n in needles)
            ),
            "другое",
        )
        return NormalizedProduct(clean, category)

    def set_alias(self, alias: str, normalized_name: str, category: str) -> ProductAlias:
        clean = self.clean(alias)
        row = self.session.scalar(select(ProductAlias).where(ProductAlias.alias == clean))
        if row is None:
            row = ProductAlias(alias=clean, normalized_name=normalized_name, category=category)
            self.session.add(row)
        else:
            row.normalized_name = normalized_name
            row.category = category
        for item in self.session.scalars(select(ReceiptItem)):
            if self.clean(item.original_name) == clean:
                item.normalized_name = normalized_name
                item.category = category
        return row
