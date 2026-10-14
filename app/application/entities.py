"""Доменные сущности, пересекающие границу репозиториев.

Намеренно отделены от ORM-моделей (infrastructure/db/models.py): сервисы и тесты
работают с этими dataclass'ами, не зная про SQLAlchemy. Маппинг ORM<->dataclass
живёт в репозиториях.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
    onboarded_at: datetime | None = None  # None → первый запуск, показать визард (FR-24)
    ui_prefs: dict = field(default_factory=dict)  # оформление, §6.5
    created_at: datetime | None = None  # с какого дня считать «N из 7» (§6.23)
    scan_nudged_at: datetime | None = None  # когда напоминали о замере (§6.24)


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
    photo_file_id: str | None = None  # FR-27: Telegram file_id фото этикетки (если скан)
    id: int | None = None


@dataclass
class RecipeItem:
    """FR-39.7: ингредиент рецепта. `density` — снимок плотности на момент сохранения состава;
    пересчёт берёт ТЕКУЩУЮ плотность продукта, а снимок — только если продукт удалён."""

    name: str
    grams: float
    density: Density
    product_id: int | None = None
    is_estimate: bool = True
    # INV-14 решаем по словам человека («пиво Heineken»), а не по имени в каталоге («Heineken»):
    # иначе этанол уходил бы в Атвотер. Решение фиксируем вместе с составом.
    alcohol: bool = False


@dataclass
class Recipe:
    items: list[RecipeItem]
    yield_g: float | None = None  # вес готового; None — сумма граммов состава


@dataclass
class MealItem:
    name_snapshot: str
    grams: float
    macros: Macros  # снапшот на момент логирования — история не «плывёт» при правках каталога
    product_id: int | None = None
    is_estimate: bool = False  # AI-оценка плотности (снапшот) → метка «≈» в карточке (INV-13)
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
    estimated: bool = False  # транзиентный флаг: хоть одна позиция — AI-оценка (не персистится)
    template_name: str | None = None  # FR-39.2: имя блюда своей сборки, по нему идёт повтор
    id: int | None = None
    created_at: datetime | None = None


@dataclass
class StatedUse:
    """Позиция «своими цифрами» в «Недавних» (§6.25): в каталоге её нет, плотности на 100 г
    тоже. Последний раз — чтобы повторить его снимок (MA-INV-67)."""

    name: str
    last_at: datetime
    uses: int
    meal_id: int
    item_id: int
    grams: float
    macros: Macros


@dataclass
class WeightLog:
    user_id: int
    measured_at: datetime
    weight_kg: float
    id: int | None = None
