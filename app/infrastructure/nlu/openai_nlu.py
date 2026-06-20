"""Реализация NluPort через OpenAI structured outputs.

`parse` — извлечение при temperature=0 (стабильность). `parse_samples` — self-consistency
(D-15): для meal с оцениваемыми позициями делает k вызовов при temperature>0, чтобы по
разбросу оценок померить уверенность; для не-meal/стейтед — один вызов (без лишних трат).
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence

from openai import AsyncOpenAI

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.domain.consensus import SAMPLE_TEMPERATURE
from app.infrastructure.nlu.prompts import SYSTEM_PROMPT, build_user_prompt
from app.infrastructure.nlu.schema import NluResult


class OpenAiNlu:
    def __init__(self, client: AsyncOpenAI, model: str, sample_k: int = 5) -> None:
        self._client = client
        self._model = model
        self._k = max(1, sample_k)

    async def _parse_at(
        self, text: str, known_keys: Sequence[str], temperature: float
    ) -> ParsedMessage:
        completion = await self._client.beta.chat.completions.parse(
            model=self._model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(text, known_keys)},
            ],
            response_format=NluResult,
            temperature=temperature,
        )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            return ParsedMessage(intent=Intent.UNKNOWN, confidence=0.0)
        return _to_dto(parsed)

    async def parse(self, text: str, known_keys: Sequence[str]) -> ParsedMessage:
        return await self._parse_at(text, known_keys, 0.0)

    async def parse_samples(
        self, text: str, known_keys: Sequence[str]
    ) -> list[ParsedMessage]:
        first = await self._parse_at(text, known_keys, 0.0)
        # Сэмплируем ТОЛЬКО когда есть что оценивать (meal с не-стейтед позициями) — иначе
        # один вызов: вес/статистика/коррекция/готовые КБЖУ оценки не требуют.
        if (
            self._k <= 1
            or first.intent is not Intent.MEAL
            or not any(_needs_estimate(i) for i in first.items)
        ):
            return [first]
        extra = await asyncio.gather(
            *[self._parse_at(text, known_keys, SAMPLE_TEMPERATURE) for _ in range(self._k - 1)]
        )
        return [first, *extra]


def _needs_estimate(item: ParsedItem) -> bool:
    """Позиция требует оценки плотности/веса (а не названных юзером тоталов FR-22)."""
    return not item.total_kcal


def _portion_options(i: object) -> dict[str, float] | None:
    """Тройка реальных размеров порции (S/M/L грамм) от LLM → dict; None, если не заданы."""
    opts = {
        size: grams
        for size, grams in (
            ("small", i.portion_small_g),
            ("medium", i.portion_medium_g),
            ("large", i.portion_large_g),
        )
        if grams is not None
    }
    return opts or None


def _to_dto(result: NluResult) -> ParsedMessage:
    return ParsedMessage(
        intent=Intent(result.intent),
        weight_kg=result.weight_kg,
        correction_op=result.correction_op,
        confidence=result.confidence,
        items=[
            ParsedItem(
                name=i.name,
                canonical_key=i.canonical_key,
                unit=Unit(i.unit),
                quantity=i.quantity,
                grams=i.grams,
                portion_hint=i.portion_hint,
                portion_options=_portion_options(i),
                unit_weight_g=i.unit_weight_g,
                kcal_per_100g=i.kcal_per_100g,
                protein_per_100g=i.protein_per_100g,
                fat_per_100g=i.fat_per_100g,
                carb_per_100g=i.carb_per_100g,
                total_kcal=i.total_kcal,
                total_protein=i.total_protein,
                total_fat=i.total_fat,
                total_carb=i.total_carb,
                clarify_options=i.clarify_options or None,
                confidence=i.confidence,
            )
            for i in result.items
        ],
    )
