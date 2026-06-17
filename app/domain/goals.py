"""Цели по КБЖУ и расчёт остатка на день."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.domain.value_objects import Macros


@dataclass(frozen=True, slots=True)
class Goal:
    daily_kcal: float
    protein_g: float
    fat_g: float
    carb_g: float


@dataclass(frozen=True, slots=True)
class Remaining:
    consumed: Macros
    goal: Goal

    @property
    def kcal_left(self) -> float:
        return round(self.goal.daily_kcal - self.consumed.kcal, 1)

    @property
    def protein_left(self) -> float:
        return round(self.goal.protein_g - self.consumed.protein, 1)

    @property
    def fat_left(self) -> float:
        return round(self.goal.fat_g - self.consumed.fat, 1)

    @property
    def carb_left(self) -> float:
        return round(self.goal.carb_g - self.consumed.carb, 1)


def remaining(consumed: Macros, goal: Goal) -> Remaining:
    return Remaining(consumed=consumed, goal=goal)


def mifflin_st_jeor(
    *,
    sex: Literal["male", "female"],
    weight_kg: float,
    height_cm: float,
    age: int,
    activity: float = 1.375,
) -> float:
    """Дневная норма калорий (BMR × коэффициент активности). Используется в /goal из профиля."""
    bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + (5 if sex == "male" else -161)
    return round(bmr * activity, 0)
