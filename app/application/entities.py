"""Доменные сущности, пересекающие границу репозиториев.

Намеренно отделены от ORM-моделей (infrastructure/db/models.py): сервисы и тесты
работают с этими dataclass'ами, не зная про SQLAlchemy. Маппинг ORM<->dataclass
живёт в репозиториях.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.domain.value_objects import Density, Macros


@dataclass
class User:
    telegram_id: int
    name: str | None
    locale: str
    timezone: str
    role: str = "user"
    is_active: bool = True
    id: int | None = None


@dataclass
class Product:
    canonical_key: str
    display_name: str
    density: Density
    category: str | None = None
    unit_weight_g: float | None = None
    default_portion_g: float | None = None
    portion_sizes: dict[str, float] | None = None
    source: str = "llm"
    is_estimate: bool = True
    id: int | None = None


@dataclass
class MealItem:
    name_snapshot: str
    grams: float
    macros: Macros  # снапшот на момент логирования — история не «плывёт» при правках каталога
    product_id: int | None = None
    id: int | None = None


@dataclass
class Meal:
    user_id: int
    eaten_at: datetime
    input_type: str
    raw_input: str
    items: list[MealItem]
    source_confidence: float
    totals: Macros
    id: int | None = None
    created_at: datetime | None = None


@dataclass
class WeightLog:
    user_id: int
    measured_at: datetime
    weight_kg: float
    id: int | None = None
