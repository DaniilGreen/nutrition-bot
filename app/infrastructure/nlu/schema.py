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
    unit: Literal["g", "ml", "piece", "portion", "unknown"]
    quantity: float | None = Field(default=None, description="Число штук для штучных продуктов")
    grams: float | None = Field(default=None, description="Оценка общего веса порции в граммах")
    portion_hint: Literal["small", "medium", "large"] | None = Field(
        default=None, description="размер порции; СТРОГО null, если размер не назван словом"
    )
    unit_weight_g: float | None = Field(default=None, description="Вес одной штуки в граммах")
    ambiguous: bool = Field(
        default=False,
        description="true, если способ/часть/тип не уточнены и заметно влияют на КБЖУ "
        "(«курица», «мясо», «кофе»); false для уточнённых («грудка отварная», «эспрессо»)",
    )
    kcal_per_100g: float = Field(
        description="Оценка КБЖУ на 100 г (нужна только для новых продуктов)"
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
    confidence: float = Field(description="Уверенность по продукту, 0..1")


class NluResult(BaseModel):
    intent: Literal["meal", "weight", "query", "correction", "unknown"]
    items: list[NluItem] = Field(default_factory=list)
    weight_kg: float | None = None
    correction_op: Literal["delete", "replace", "adjust", "other"] | None = Field(
        default=None, description="Тип правки для intent=correction"
    )
    confidence: float = Field(description="Общая уверенность разбора, 0..1")
