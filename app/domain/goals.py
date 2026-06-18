"""Цели по КБЖУ: расчёт остатка на день и вывод цели из профиля (Mifflin)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.domain.value_objects import Macros

# Коэффициенты активности (множитель к BMR).
ACTIVITY_FACTORS = {"low": 1.2, "medium": 1.375, "high": 1.55}
# Коэффициенты цели: дефицит для похудения / поддержание / профицит.
TARGET_FACTORS = {"cut": 0.85, "maintain": 1.0, "bulk": 1.1}


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
    """Дневная норма калорий (BMR × коэффициент активности)."""
    bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + (5 if sex == "male" else -161)
    return round(bmr * activity, 0)


def goal_from_profile(
    *,
    sex: Literal["male", "female"],
    weight_kg: float,
    height_cm: float,
    age: int,
    activity: str,
    target: str,
) -> Goal:
    """Цель из профиля: Mifflin × активность × цель, с разбивкой макросов.

    Белок фиксируем ~2 г/кг (важно при дефиците), жиры 25% ккал, углеводы — остаток.
    """
    tdee = mifflin_st_jeor(
        sex=sex,
        weight_kg=weight_kg,
        height_cm=height_cm,
        age=age,
        activity=ACTIVITY_FACTORS.get(activity, 1.375),
    )
    kcal = round(tdee * TARGET_FACTORS.get(target, 1.0))
    protein_g = round(2.0 * weight_kg)
    fat_g = round(kcal * 0.25 / 9)
    carb_g = round(max(0.0, kcal - protein_g * 4 - fat_g * 9) / 4)
    return Goal(daily_kcal=kcal, protein_g=protein_g, fat_g=fat_g, carb_g=carb_g)
