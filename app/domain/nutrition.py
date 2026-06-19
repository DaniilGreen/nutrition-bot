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


def atwater_kcal(protein: float, fat: float, carb: float) -> float:
    """Ккал по Атвотеру: 4·белок + 9·жир + 4·углеводы (на те же 100 г)."""
    return 4.0 * protein + 9.0 * fat + 4.0 * carb


def reconcile_density(d: Density) -> Density:
    """Авто-сверка правдоподобия LLM-оценки: ккал должны ≈ Атвотер(Б,Ж,У).

    Расходятся сверх допуска (0.3·ккал+20, как в seed.py) → доверяем макросам и пересчитываем
    ккал из них (Атвотер физичен: ккал ВЫВОДЯТСЯ из Б/Ж/У). Согласованные оценки не трогаем.
    Пустую оценку (все нули) не трогаем — это отдельный путь (blank-retry в assistant).
    """
    expected = atwater_kcal(d.protein_per_100g, d.fat_per_100g, d.carb_per_100g)
    if expected == 0:
        return d
    tol = 0.3 * max(d.kcal_per_100g, expected) + 20
    if abs(d.kcal_per_100g - expected) <= tol:
        return d
    return Density(
        kcal_per_100g=round(expected, 1),
        protein_per_100g=d.protein_per_100g,
        fat_per_100g=d.fat_per_100g,
        carb_per_100g=d.carb_per_100g,
    )
