"""Реализация NluPort через OpenAI structured outputs.

`parse` — извлечение при temperature=0 (стабильность). `parse_samples` — self-consistency
(D-15): для meal с оцениваемыми позициями делает k вызовов при temperature>0, чтобы по
разбросу оценок померить уверенность; для не-meal/стейтед — один вызов (без лишних трат).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from openai import AsyncOpenAI

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.domain.consensus import SAMPLE_TEMPERATURE
from app.infrastructure.nlu.prompts import SYSTEM_PROMPT, build_user_prompt
from app.infrastructure.nlu.schema import NluResult
from app.infrastructure.openai_compat import request_extras, temperature_for, token_counts

# AI-PRINCIPLES §1: сеть моргает, и разбор еды не должен от этого теряться.
# Ретраим всё одинаково — разбираться, что именно отвалилось, дороже, чем повторить.
RETRIES = 2
BACKOFF_S = 1.0
TIMEOUT_S = 30.0
# Один ключ на все вызовы разбора: префикс у них общий (схема + системный промпт + каталог).
CACHE_KEY = "burnfat-nlu"


log = logging.getLogger(__name__)


class OpenAiNlu:
    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        sample_k: int = 5,
        meter: object | None = None,
        timeout: float = TIMEOUT_S,
    ) -> None:
        self._client = client
        self._model = model
        self._k = max(1, sample_k)
        # AI-PRINCIPLES §2: разбор — ОСНОВНАЯ статья трат (пять вызовов на приём
        # с оценкой), и до 08.09.2026 она не считалась вовсе. Счётчик со своим
        # интерфейсом, а не SpendRepo: адаптеру про транзакции знать незачем.
        self._meter = meter
        self._timeout = timeout

    async def _parse_at(
        self, text: str, known_keys: Sequence[str], temperature: float
    ) -> ParsedMessage:
        last: Exception | None = None
        for attempt in range(1 + RETRIES):
            if attempt:
                await asyncio.sleep(BACKOFF_S * attempt)
            try:
                return await self._parse_once(text, known_keys, temperature)
            except Exception as exc:  # noqa: BLE001 — таймаут/сеть/429 ретраим одинаково
                last = exc
                log.warning("разбор не удался, попытка %d из %d: %s",
                            attempt + 1, 1 + RETRIES, exc)
        assert last is not None
        # Наверх, а не молча: вызывающий сохранит сырой ввод и предложит повтор.
        raise last

    async def _parse_once(
        self, text: str, known_keys: Sequence[str], temperature: float
    ) -> ParsedMessage:
        completion = await self._client.beta.chat.completions.parse(
            model=self._model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(text, known_keys)},
            ],
            response_format=NluResult,
            # gpt-5.x отвергает явную temperature (400) — там параметр не шлём вовсе
            temperature=temperature_for(self._model, temperature),
            timeout=self._timeout,
            extra_body=request_extras(self._model, CACHE_KEY) or None,
        )
        await self._meter_usage(completion)
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            return ParsedMessage(intent=Intent.UNKNOWN, confidence=0.0)
        return _to_dto(parsed)

    async def _meter_usage(self, completion: object) -> None:
        """Записать расход вызова. Сбой счётчика не должен ронять разбор:
        потерять запись дороже, чем недосчитать цент."""
        if self._meter is None:
            return
        try:
            tokens_in, cached, tokens_out = token_counts(completion.usage)
            await self._meter.note(
                model=self._model, input_tokens=tokens_in,
                output_tokens=tokens_out, cached_tokens=cached,
            )
        except Exception:  # noqa: BLE001
            log.warning("не удалось записать расход разбора", exc_info=True)

    async def parse(self, text: str, known_keys: Sequence[str]) -> ParsedMessage:
        return await self._parse_at(text, known_keys, 0.0)

    async def parse_samples(
        self, text: str, known_keys: Sequence[str]
    ) -> list[ParsedMessage]:
        first = await self._parse_at(text, known_keys, 0.0)
        # INV-12: сэмплируем только НОВЫЙ продукт. У знакомого плотность берётся из каталога
        # (INV-2), и медиана сэмплов выбрасывалась бы — до 28.09.2026 это были 82% вызовов
        # разбора. Позицию с вариантами тоже не сэмплируем: после выбора её переразберут.
        # Вес, статистика, коррекция и названные КБЖУ оценки не требуют вовсе.
        known = set(known_keys)
        todo = [
            i for i in first.items
            if _needs_estimate(i) and i.canonical_key not in known and not i.clarify_options
        ]
        if self._k <= 1 or first.intent is not Intent.MEAL or not todo:
            return [first]
        extra = await asyncio.gather(
            *[self._parse_at(text, known_keys, SAMPLE_TEMPERATURE) for _ in range(self._k - 1)],
            return_exceptions=True,
        )
        # Упавший сэмпл не отменяет уже оплаченные: медиане хватит остальных.
        return [first, *(s for s in extra if isinstance(s, ParsedMessage))]


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
        day_offset=result.day_offset,
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
                zero_calorie=i.zero_calorie,
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
