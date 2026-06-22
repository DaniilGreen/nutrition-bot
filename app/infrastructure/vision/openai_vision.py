"""OpenAI vision-адаптер: фото этикетки → КБЖУ/100г (FR-27). Реализует VisionPort.

Читает таблицу пищевой ценности мультиязычно (рус/англ/вьетнам) и переводит вес «на порцию»
в «на 100 г». Финальные числа считает приложение (INV-1) — это лишь извлечение плотности.
"""
from __future__ import annotations

import base64

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from app.application.dto import ParsedItem, Unit

SYSTEM_PROMPT = (
    "Ты читаешь таблицу пищевой ценности (КБЖУ) с фото этикетки продукта.\n"
    "Верни значения НА 100 Г: ккал, белки, жиры, углеводы (граммы).\n"
    "Этикетка может быть на любом языке. Вьетнам: Năng lượng/Calo = ккал, Chất đạm = белки,\n"
    "Chất béo = жиры, Carbohydrate/Tinh bột/Đường = углеводы. Англ.: energy/protein/fat/carb.\n"
    "Если значения даны НА ПОРЦИЮ (serving/khẩu phần), а не на 100 г — ПЕРЕСЧИТАЙ на 100 г.\n"
    "name — название продукта на РУССКОМ (переведи). package_grams — нетто-вес упаковки в г,\n"
    "если указан. found=false, если таблицы КБЖУ на фото НЕТ. Бери только реально видимое."
)


class LabelReading(BaseModel):
    found: bool = Field(description="распознана ли таблица КБЖУ на фото")
    name: str = Field(description="название продукта на русском")
    kcal_per_100g: float
    protein_per_100g: float
    fat_per_100g: float
    carb_per_100g: float
    package_grams: float | None = Field(default=None, description="нетто-вес упаковки, г")


class OpenAiVision:
    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    async def read_label(self, image: bytes) -> ParsedItem | None:
        b64 = base64.b64encode(image).decode()
        data_url = f"data:image/jpeg;base64,{b64}"
        completion = await self._client.beta.chat.completions.parse(
            model=self._model,
            temperature=0,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Прочитай таблицу КБЖУ с этой этикетки."},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            response_format=LabelReading,
        )
        r = completion.choices[0].message.parsed
        if r is None or not r.found:
            return None
        if r.kcal_per_100g == 0 and r.protein_per_100g == 0 and r.fat_per_100g == 0:
            return None  # пустое чтение — этикетку не разобрали
        return ParsedItem(
            name=r.name.strip() or "продукт с этикетки",
            canonical_key="",  # meal_service сам сделает slug из имени (INV-4)
            unit=Unit.GRAM,
            grams=None,
            kcal_per_100g=r.kcal_per_100g,
            protein_per_100g=r.protein_per_100g,
            fat_per_100g=r.fat_per_100g,
            carb_per_100g=r.carb_per_100g,
            unit_weight_g=r.package_grams,
            from_label=True,
            confidence=1.0,
        )
