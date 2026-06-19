"""INV-11 gate: согласованность ккал↔БЖУ, границы, фолбэк."""
from __future__ import annotations

from app.domain.nutrition import atwater_kcal
from app.domain.plausibility import (
    FALLBACK_DENSITY,
    MAX_ITEM_KCAL,
    MAX_KCAL_PER_100G,
    MAX_PORTION_G,
    clamp_density,
    clamp_grams,
    clamp_macros,
    vet_macros,
)
from app.domain.value_objects import Density, Macros


def test_vet_reconciles_inconsistent_kcal() -> None:
    # поке: 350 ккал, но БЖУ на 577 → доверяем макросам
    m = vet_macros(Macros(kcal=350, protein=35, fat=17.5, carb=70))
    assert m.kcal == atwater_kcal(35, 17.5, 70)  # 577.5


def test_vet_keeps_consistent() -> None:
    m = Macros(kcal=410, protein=30, fat=10, carb=50)  # Атвотер ровно 410
    assert vet_macros(m) == m


def test_vet_keeps_kcal_only() -> None:
    # «X 400 ккал» без БЖУ → ккал сохраняется, не обнуляется Атвотером
    assert vet_macros(Macros(400, 0, 0, 0)).kcal == 400


def test_vet_clamps_kcal_only_absurd() -> None:
    assert vet_macros(Macros(99999, 0, 0, 0)).kcal == MAX_ITEM_KCAL


def test_vet_floors_negative_macro() -> None:
    assert vet_macros(Macros(kcal=130, protein=-5, fat=10, carb=10)).protein == 0


def test_clamp_macros_trusts_but_bounds() -> None:
    # clamp_macros НЕ сверяет Атвотер (доверяем юзеру), но режет абсурд
    assert clamp_macros(Macros(350, 35, 17.5, 70)).kcal == 350  # несогласованные — оставлены
    assert clamp_macros(Macros(99999, -5, 10, 10)).kcal == MAX_ITEM_KCAL
    assert clamp_macros(Macros(100, -5, 10, 10)).protein == 0


def test_clamp_density_bounds() -> None:
    d = clamp_density(Density(1500, 200, -3, 50))
    assert d.kcal_per_100g == MAX_KCAL_PER_100G
    assert d.protein_per_100g == 100
    assert d.fat_per_100g == 0


def test_clamp_grams_bounds() -> None:
    assert clamp_grams(5000) == MAX_PORTION_G
    assert clamp_grams(0) == 1.0
    assert clamp_grams(250) == 250


def test_fallback_density_self_consistent() -> None:
    d = FALLBACK_DENSITY
    expected = atwater_kcal(d.protein_per_100g, d.fat_per_100g, d.carb_per_100g)
    assert abs(expected - d.kcal_per_100g) <= 0.3 * d.kcal_per_100g + 20
    assert d.kcal_per_100g > 0  # фолбэк не нулевой
