"""Реализация NluPort через OpenAI structured outputs."""
from __future__ import annotations

from collections.abc import Sequence

from openai import AsyncOpenAI

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.infrastructure.nlu.prompts import SYSTEM_PROMPT, build_user_prompt
from app.infrastructure.nlu.schema import NluResult


class OpenAiNlu:
    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    async def parse(self, text: str, known_keys: Sequence[str]) -> ParsedMessage:
        completion = await self._client.beta.chat.completions.parse(
            model=self._model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(text, known_keys)},
            ],
            response_format=NluResult,
            temperature=0,  # стабильность оценок плотности на cache-miss
        )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            return ParsedMessage(intent=Intent.UNKNOWN, confidence=0.0)
        return _to_dto(parsed)


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
                unit_weight_g=i.unit_weight_g,
                ambiguous=i.ambiguous,
                kcal_per_100g=i.kcal_per_100g,
                protein_per_100g=i.protein_per_100g,
                fat_per_100g=i.fat_per_100g,
                carb_per_100g=i.carb_per_100g,
                total_kcal=i.total_kcal,
                total_protein=i.total_protein,
                total_fat=i.total_fat,
                total_carb=i.total_carb,
                confidence=i.confidence,
            )
            for i in result.items
        ],
    )
