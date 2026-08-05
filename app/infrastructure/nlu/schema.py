"""Pydantic-схема для structured output OpenAI. Зеркалит application.dto.ParsedMessage."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class NluItem(BaseModel):
    name: str = Field(description="Название продукта на русском, как понято из сообщения")
    canonical_key: str = Field(
        description="Стабильный английский slug в snake_case, одинаковый для разных "
        "формулировок одного продукта (напр. chicken_breast_grilled)"
    )
    est_note: str = Field(
        description="СНАЧАЛА (до всех чисел) вспомни РЕАЛЬНЫЙ продукт из своих знаний: вес одной "
        "типичной штуки и плотность КБЖУ. 4–12 слов. Числа ниже ДОЛЖНЫ следовать отсюда, а НЕ из "
        "шаблонов/примеров. Напр.: «личи — мелкая ягода, ~15 г/шт, ~66 ккал/100 г, вода 82%»."
    )
    unit: Literal["g", "ml", "piece", "portion", "unknown"]
    quantity: float | None = Field(default=None, description="Число штук для штучных продуктов")
    grams: float | None = Field(default=None, description="Оценка общего веса порции в граммах")
    portion_hint: Literal["small", "medium", "large"] | None = Field(
        default=None, description="размер порции; СТРОГО null, если размер не назван словом"
    )
    portion_small_g: float | None = Field(
        default=None,
        description="Вес МАЛЕНЬКОЙ одиночной порции ИМЕННО этого блюда, г. Заполняй для "
        "unit=portion (нет явного веса): реалистично для блюда (круассан ~80, тарелка супа ~250).",
    )
    portion_medium_g: float | None = Field(
        default=None, description="Вес СРЕДНЕЙ одиночной порции этого блюда, г (круассан ~120, суп ~350)"
    )
    portion_large_g: float | None = Field(
        default=None, description="Вес БОЛЬШОЙ одиночной порции этого блюда, г (круассан ~160, суп ~450)"
    )
    unit_weight_g: float | None = Field(default=None, description="Вес одной штуки в граммах")
    kcal_per_100g: float = Field(
        description="Оценка КБЖУ на 100 г (нужна только для новых продуктов)"
    )
    zero_calorie: bool = Field(
        default=False,
        description="true, если продукт ДЕЙСТВИТЕЛЬНО бескалорийный (вода, кола зеро, чёрный "
        "кофе без сахара, чай без сахара, подсластитель). Отличает честный ноль от «не смог "
        "оценить»: без флага ноль считается промахом и заменяется фолбэком ~150 ккал/100 г.",
    )
    protein_per_100g: float
    fat_per_100g: float
    carb_per_100g: float
    total_kcal: float | None = Field(
        default=None,
        description="Явно названный пользователем ИТОГ КБЖУ всей порции (меню/этикетка). "
        "Если задан — приложение берёт его как есть, *_per_100g не нужны.",
    )
    total_protein: float | None = None
    total_fat: float | None = None
    total_carb: float | None = None
    clarify_options: list[str] | None = Field(
        default=None,
        description="2–4 варианта-кнопки для УТОЧНЕНИЯ, когда запрос неточен по оси, заметно "
        "меняющей КБЖУ. Каждый — готовая замена имени («шаверма с говядиной»). Правила — в "
        "системном промте. null, если уточнять нечего.",
    )
    confidence: float = Field(description="Уверенность по продукту, 0..1")


class NluResult(BaseModel):
    intent: Literal["meal", "weight", "query", "correction", "unknown"]
    items: list[NluItem] = Field(default_factory=list)
    weight_kg: float | None = None
    correction_op: Literal["delete", "replace", "adjust", "other"] | None = Field(
        default=None, description="Тип правки для intent=correction"
    )
    day_offset: int = Field(
        default=0,
        description="Сдвиг дня приёма относительно сегодня: «вчера»=-1, «позавчера»=-2, "
        "«завтра»=+1. Если день НЕ назван — строго 0 (сегодня). Диапазон -30..+7.",
    )
    confidence: float = Field(description="Общая уверенность разбора, 0..1")
