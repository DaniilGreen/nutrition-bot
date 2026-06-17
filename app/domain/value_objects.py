"""Базовые value-объекты нутрициологии. Неизменяемые, без I/O."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Macros:
    """Готовые КБЖУ для конкретной порции (уже умноженные на граммы)."""

    kcal: float
    protein: float
    fat: float
    carb: float

    def __add__(self, other: Macros) -> Macros:
        return Macros(
            kcal=self.kcal + other.kcal,
            protein=self.protein + other.protein,
            fat=self.fat + other.fat,
            carb=self.carb + other.carb,
        )

    def rounded(self, ndigits: int = 1) -> Macros:
        return Macros(
            round(self.kcal, ndigits),
            round(self.protein, ndigits),
            round(self.fat, ndigits),
            round(self.carb, ndigits),
        )


ZERO_MACROS = Macros(0.0, 0.0, 0.0, 0.0)


def sum_macros(items: Iterable[Macros]) -> Macros:
    total = ZERO_MACROS
    for m in items:
        total = total + m
    return total.rounded(1)


@dataclass(frozen=True, slots=True)
class Density:
    """КБЖУ на 100 г — единственный источник истины для продукта.

    Зафиксирована в БД при первом появлении продукта и больше не меняется
    автоматически (см. модель «LLM наполняет, БД фиксирует»).
    """

    kcal_per_100g: float
    protein_per_100g: float
    fat_per_100g: float
    carb_per_100g: float
