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


def reconcile_kcal(kcal: float, protein: float, fat: float, carb: float) -> float:
    """Ккал, согласованные с макросами по Атвотеру. Расхождение сверх допуска (0.3·ккал+20) →
    доверяем макросам (физика: ккал ВЫВОДЯТСЯ из Б/Ж/У). Пустые макросы (Атвотер 0) — не трогаем.

    Единый источник правила сверки (используют reconcile_density и gate INV-11).
    """
    expected = atwater_kcal(protein, fat, carb)
    if expected <= 0:
        return kcal
    tol = 0.3 * max(kcal, expected) + 20
    return round(expected, 1) if abs(kcal - expected) > tol else kcal


def reconcile_density(d: Density) -> Density:
    """Авто-сверка правдоподобия LLM-оценки плотности (ккал ≈ Атвотер(Б,Ж,У))."""
    return Density(
        reconcile_kcal(d.kcal_per_100g, d.protein_per_100g, d.fat_per_100g, d.carb_per_100g),
        d.protein_per_100g,
        d.fat_per_100g,
        d.carb_per_100g,
    )
