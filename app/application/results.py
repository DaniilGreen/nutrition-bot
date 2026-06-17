"""Результаты работы Assistant — данные для презентационного слоя (бот форматирует)."""
from __future__ import annotations

from dataclasses import dataclass

from app.application.entities import Meal, WeightLog
from app.domain.goals import Remaining


@dataclass
class MealLogged:
    meal: Meal
    remaining: Remaining


@dataclass
class WeightLogged:
    log: WeightLog
    delta_kg: float | None


@dataclass
class DailyStats:
    remaining: Remaining
    meals_count: int


@dataclass
class Unknown:
    reason: str = ""


Result = MealLogged | WeightLogged | DailyStats | Unknown
