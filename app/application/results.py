"""Результаты работы Assistant — данные для презентационного слоя (бот форматирует)."""
from __future__ import annotations

from dataclasses import dataclass

from app.application.dto import ParsedMessage
from app.application.entities import Meal, MealItem, WeightLog
from app.domain.goals import Goal, Remaining
from app.domain.value_objects import Macros


@dataclass
class MealLogged:
    meal: Meal
    remaining: Remaining


@dataclass
class ClarificationNeeded:
    """Нужен один уточняющий вопрос (M2): размер порции для конкретного продукта."""

    draft: ParsedMessage
    item_index: int
    item_name: str


@dataclass
class VariantClarificationNeeded:
    """D-19: запрос неточен по КБЖУ-важной оси — LLM предложил варианты (строки) для кнопок."""

    draft: ParsedMessage
    item_index: int
    item_name: str
    options: list[str]  # готовые замены имени от LLM («с курицей», «с говядиной»)


@dataclass
class WeightLogged:
    log: WeightLog
    delta_kg: float | None


@dataclass
class WeightTrend:
    logs: list[WeightLog]  # newest-first
    timezone: str


@dataclass
class DailyStats:
    remaining: Remaining
    meals_count: int


@dataclass
class WeeklyStats:
    consumed: Macros
    days_logged: int
    meals_count: int
    goal: Goal


@dataclass
class History:
    meals: list[Meal]
    timezone: str


@dataclass
class MealDeleted:
    meal: Meal | None


@dataclass
class ItemRemoved:
    """Коррекция «удали X»: позиции сняты, показываем обновлённый остаток дня."""

    items: list[MealItem]
    remaining: Remaining


@dataclass
class ItemAdjusted:
    """Коррекция «поправь граммы у X»: позиция пересчитана под новый вес, остаток обновлён."""

    item: MealItem
    old_grams: float
    remaining: Remaining


@dataclass
class CorrectionFailed:
    """Коррекция не выполнена: не нашли цель (not_found) или op не поддержан (unsupported)."""

    target: str
    reason: str  # not_found | unsupported


@dataclass
class Unknown:
    reason: str = ""


Result = (
    MealLogged
    | ClarificationNeeded
    | VariantClarificationNeeded
    | WeightLogged
    | WeightTrend
    | DailyStats
    | WeeklyStats
    | History
    | MealDeleted
    | ItemRemoved
    | ItemAdjusted
    | CorrectionFailed
    | Unknown
)
