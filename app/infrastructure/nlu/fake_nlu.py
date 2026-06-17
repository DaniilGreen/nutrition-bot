"""Локальный детерминированный парсер без LLM (USE_FAKE_NLU=true).

Грубая эвристика для smoke-теста бота без ключа OpenAI. В юнит/e2e-тестах
используется отдельный StubNlu со сценарными ответами.
"""
from __future__ import annotations

import re
from collections.abc import Sequence

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit

_WEIGHT_RE = re.compile(r"(\d{2,3}(?:[.,]\d)?)")
_GRAMS_RE = re.compile(r"(\d+)\s*(?:г|гр|грамм|g)\b", re.IGNORECASE)


class FakeNlu:
    async def parse(self, text: str, known_keys: Sequence[str]) -> ParsedMessage:
        low = text.lower().strip()

        if low.startswith("вес") or low.startswith("сегодня"):
            m = _WEIGHT_RE.search(low)
            if m:
                return ParsedMessage(
                    intent=Intent.WEIGHT,
                    weight_kg=float(m.group(1).replace(",", ".")),
                    confidence=0.9,
                )

        if any(w in low for w in ("статист", "осталось", "сколько")):
            return ParsedMessage(intent=Intent.QUERY, confidence=0.9)

        g = _GRAMS_RE.search(low)
        if g:
            name = low[: g.start()].strip() or "еда"
            return ParsedMessage(
                intent=Intent.MEAL,
                confidence=0.8,
                items=[
                    ParsedItem(
                        name=name,
                        canonical_key=re.sub(r"\s+", "_", name),
                        unit=Unit.GRAM,
                        grams=float(g.group(1)),
                        kcal_per_100g=120,
                        protein_per_100g=10,
                        fat_per_100g=5,
                        carb_per_100g=8,
                        confidence=0.8,
                    )
                ],
            )

        return ParsedMessage(intent=Intent.UNKNOWN, confidence=0.3)
