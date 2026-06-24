"""Результаты работы Assistant — данные для презентационного слоя (бот форматирует)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

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
class LabelRead:
    """FR-27: распознана этикетка — показываем КБЖУ/100г и ждём вес порции (через continue_meal)."""

    draft: ParsedMessage
    item_index: int
    atwater_warning: bool = False  # FR-27: Б/Ж/У ≠ ккал на этикетке → вероятен misread, показать ⚠️


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
    meals: list[Meal] = field(default_factory=list)  # сегодняшние приёмы (список съеденного)
    timezone: str = "UTC"  # для рендера времени приёмов


@dataclass
class DayView:
    """Вар. A: приёмы за один день + навигация ← / → по дням."""

    meals: list[Meal]  # хронологически (утро→вечер)
    timezone: str
    days_ago: int  # 0 = сегодня
    day: date  # дата дня в TZ юзера (для заголовка)


@dataclass
class WeeklyStats:
    consumed: Macros
    days_logged: int
    meals_count: int
    goal: Goal


@dataclass
class MonthReport:
    """Отчёт за период (FR-29): средние/день, дни в цели, Δ веса — для картинки /month."""

    total_days: int
    days_logged: int
    days_in_goal: int
    avg_kcal: float
    avg_macros: Macros
    weight_delta: float | None
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
    | LabelRead
    | WeightLogged
    | WeightTrend
    | DailyStats
    | WeeklyStats
    | History
    | DayView
    | MealDeleted
    | ItemRemoved
    | ItemAdjusted
    | CorrectionFailed
    | Unknown
)
