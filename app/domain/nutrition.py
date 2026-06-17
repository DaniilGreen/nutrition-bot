"""Расчёт КБЖУ. Это и есть «приложение считает, не LLM».

Чистая функция: одни и те же (плотность, граммы) всегда дают один результат.
"""
from __future__ import annotations

from app.domain.value_objects import Density, Macros


def macros_for_grams(density: Density, grams: float) -> Macros:
    if grams < 0:
        raise ValueError("grams must be non-negative")
    factor = grams / 100.0
    return Macros(
        kcal=density.kcal_per_100g * factor,
        protein=density.protein_per_100g * factor,
        fat=density.fat_per_100g * factor,
        carb=density.carb_per_100g * factor,
    ).rounded(1)
