"""OpenAI vision-адаптер: фото этикетки → КБЖУ/100г (FR-27). Реализует VisionPort.

Читает таблицу пищевой ценности мультиязычно (рус/англ/вьетнам) и переводит вес «на порцию»
в «на 100 г». Финальные числа считает приложение (INV-1) — это лишь извлечение плотности.
"""
from __future__ import annotations

import base64
import logging
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from app.application.dto import ParsedItem, PlateReading, Unit
from app.domain.plate import FAT_KINDS, MAX_COMPONENTS, Component
from app.infrastructure.openai_compat import temperature_for, token_counts

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты читаешь таблицу пищевой ценности (КБЖУ) с фото этикетки продукта.\n"
    "Верни значения НА 100 Г: ккал, белки, жиры, углеводы (граммы).\n"
    "Этикетка может быть на любом языке. Вьетнам: Năng lượng/Calo = ккал, Chất đạm = белки,\n"
    "Chất béo = жиры, Carbohydrate/Tinh bột/Đường = углеводы. Англ.: energy/protein/fat/carb.\n"
    "Если значения даны НА ПОРЦИЮ (serving/khẩu phần), а не на 100 г — ПЕРЕСЧИТАЙ на 100 г.\n"
    "name — КОНКРЕТНО, на русском: бренд (если виден) + тип + признак жирности/вида.\n"
    "Напр.: «молоко коровье 3.6%», «йогурт питьевой 3.8%», «молоко миндальное».\n"
    "НЕ обобщай до «молоко»/«йогурт» — иначе разные продукты сольются в каталоге.\n"
    "package_grams — нетто-вес упаковки в г, если указан. serving_grams — вес ОДНОЙ порции в г,\n"
    "если на этикетке есть («в порции / serving / khẩu phần»). found=false, если таблицы КБЖУ нет."
)


PHOTO_PROMPT = (
    "Ты помогаешь вести дневник питания. На входе — снимок и, может быть, подпись человека.\n"
    "1) kind: 'label' — на снимке упаковка/этикетка с таблицей пищевой ценности; 'dish' — еда "
    "(блюдо на тарелке, в миске, в контейнере, продукт в руке); 'none' — еды нет.\n"
    "Для 'label' и 'none' остальные поля оставь пустыми.\n"
    "2) dish — короткое название по-русски: «паста с курицей в сливочном соусе».\n"
    "3) components — съедобные составляющие (не больше 8), как их записал бы человек в "
    "дневник: КОНКРЕТНО и со способом приготовления («макароны отварные», «куриное филе "
    "жареное», «рис отварной», «огурцы свежие», «соус сливочный»).\n"
    "   - Суп, каша, рагу, плов, оливье и другие смеси, которые на тарелке не разделить, — "
    "ОДНОЙ позицией с конкретным названием («борщ с мясом», «плов с курицей»); видимые "
    "отдельно сметана, хлеб, зелень — отдельными позициями.\n"
    "   - Кость в порции (нога, крыло, рёбра) — в названии «с костью», grams — вместе с костью.\n"
    "   - Масло, на котором готовили, в components НЕ включай — о нём спросит приложение. "
    "Видимый соус, сметану, майонез, заправку — ВКЛЮЧАЙ.\n"
    "   - share — доля позиции в массе всей еды на снимке (сумма около 1).\n"
    "   - grams — твоя оценка массы позиции. Ориентиры: обычная тарелка 24–26 см, глубокая "
    "тарелка супа 300–350 г, столовая ложка, вилка, ладонь. Не занижай: люди на глаз и модели "
    "чаще недооценивают порции.\n"
    "   - Штучное и мелкое (орехи, ягоды, виноград, печенье, конфеты, дольки, кусочки колбасы) "
    "считай ПО ШТУКАМ: число штук × вес одной (миндаль ≈1.2 г, грецкий орех половинка ≈2.5 г, "
    "виноградина ≈5 г, клубника ≈12 г, черри ≈15 г). Плотное занимает мало места и весит "
    "много — не оценивай его по площади на тарелке.\n"
    "   - Кусок целиком (яблоко, котлета, бедро) — по размеру относительно тарелки: среднее "
    "яблоко ≈180 г, куриное бедро с костью ≈150 г.\n"
    "4) Подпись. Если в ней вес ВСЕЙ порции или тарелки («420», «тарелка 420 г») — "
    "caption_total_g. Если вес конкретной позиции («курица 150») — поставь его в grams этой "
    "позиции и fixed=true. Чисел, которых нет в подписи, не выдумывай: нет подписи или "
    "веса в ней — caption_total_g=null и fixed=false везде.\n"
    "5) ask_fat — true, если при готовке могли использовать масло (жареное, тушёное, паста, "
    "крупы, яичница, овощи на сковороде); false — сырые овощи и фрукты, суп, выпечка, "
    "варёное на пару. fat_kind — какое масло вероятнее.\n"
    "6) ask_dressing — true, если это салат, нарезка или каша и заправки не видно.\n"
    "7) liquid — true для супа и напитков."
)


class PlateComponentOut(BaseModel):
    name: str
    share: float
    grams: float
    fixed: bool


class PhotoReadingOut(BaseModel):
    kind: Literal["label", "dish", "none"]
    dish: str
    components: list[PlateComponentOut]
    total_grams: float
    caption_total_g: float | None
    ask_fat: bool
    fat_kind: Literal["масло подсолнечное", "масло сливочное", "масло оливковое"]
    ask_dressing: bool
    liquid: bool


class LabelReading(BaseModel):
    found: bool = Field(description="распознана ли таблица КБЖУ на фото")
    name: str = Field(description="название продукта на русском")
    kcal_per_100g: float
    protein_per_100g: float
    fat_per_100g: float
    carb_per_100g: float
    package_grams: float | None = Field(default=None, description="нетто-вес упаковки, г")
    serving_grams: float | None = Field(default=None, description="вес одной порции, г")


class OpenAiVision:
    def __init__(self, client: AsyncOpenAI, model: str, meter: object | None = None) -> None:
        self._client = client
        self._model = model
        # До 16.09.2026 фото не считались вовсе — самый дорогой вызов шёл мимо
        # лимита (AI-PRINCIPLES §2).
        self._meter = meter

    async def _meter_usage(self, completion: object) -> None:
        if self._meter is None:
            return
        try:
            tokens_in, cached, tokens_out = token_counts(completion.usage)
            await self._meter.note(
                model=self._model, input_tokens=tokens_in,
                output_tokens=tokens_out, cached_tokens=cached,
            )
        except Exception:  # noqa: BLE001 — недосчитать цент дешевле, чем потерять фото
            log.warning("не удалось записать расход фото", exc_info=True)

    async def read_photo(self, image: bytes, caption: str = "") -> PlateReading:
        """FR-34: что на снимке и из чего блюдо. Считает приложение, модель только
        раскладывает тарелку (INV-1)."""
        b64 = base64.b64encode(image).decode()
        text = f"Подпись человека: «{caption.strip()[:200]}»" if caption.strip() else "Подписи нет."
        completion = await self._client.beta.chat.completions.parse(
            model=self._model,
            temperature=temperature_for(self._model, 0.0),
            messages=[
                {"role": "system", "content": PHOTO_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text},
                        # high: оценке порции нужны детали — ложка рядом, край тарелки.
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}},
                    ],
                },
            ],
            response_format=PhotoReadingOut,
        )
        await self._meter_usage(completion)
        r = completion.choices[0].message.parsed
        if r is None:
            return PlateReading(kind="none")
        return PlateReading(
            kind=r.kind,
            dish=r.dish.strip(),
            components=[
                Component(c.name.strip(), c.share, c.grams, c.fixed)
                for c in r.components[:MAX_COMPONENTS]
                if c.name.strip()
            ],
            caption_total_g=r.caption_total_g,
            ask_fat=r.ask_fat,
            fat_kind=r.fat_kind if r.fat_kind in FAT_KINDS else FAT_KINDS[0],
            ask_dressing=r.ask_dressing,
            liquid=r.liquid,
        )

    async def read_label(self, image: bytes) -> ParsedItem | None:
        b64 = base64.b64encode(image).decode()
        data_url = f"data:image/jpeg;base64,{b64}"
        completion = await self._client.beta.chat.completions.parse(
            model=self._model,
            # gpt-5.x отвергает явную temperature (400) — там параметр не шлём вовсе
            temperature=temperature_for(self._model, 0.0),
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
        await self._meter_usage(completion)
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
            serving_g=r.serving_grams,
            from_label=True,
            confidence=1.0,
        )
