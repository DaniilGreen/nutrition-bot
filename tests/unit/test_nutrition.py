from __future__ import annotations

import pytest

from app.domain.nutrition import atwater_kcal, macros_for_grams, reconcile_density
from app.domain.value_objects import Density

CHICKEN = Density(kcal_per_100g=165, protein_per_100g=31, fat_per_100g=3.6, carb_per_100g=0)


def test_basic_multiplication() -> None:
    m = macros_for_grams(CHICKEN, 250)
    assert m.kcal == 412.5
    assert m.protein == 77.5
    assert m.fat == 9.0
    assert m.carb == 0.0


def test_zero_grams() -> None:
    m = macros_for_grams(CHICKEN, 0)
    assert m.kcal == 0
    assert m.protein == 0


def test_deterministic() -> None:
    assert macros_for_grams(CHICKEN, 137) == macros_for_grams(CHICKEN, 137)


def test_negative_raises() -> None:
    with pytest.raises(ValueError):
        macros_for_grams(CHICKEN, -1)


def test_reconcile_fixes_inconsistent_kcal() -> None:
    # LLM выдал 0 ккал, но макросы есть (круассан) → пересчёт из Атвотера
    bad = Density(kcal_per_100g=0, protein_per_100g=8, fat_per_100g=21, carb_per_100g=45)
    fixed = reconcile_density(bad)
    assert fixed.kcal_per_100g == atwater_kcal(8, 21, 45)  # 4*8+9*21+4*45 = 401
    assert (fixed.protein_per_100g, fixed.fat_per_100g, fixed.carb_per_100g) == (8, 21, 45)


def test_reconcile_keeps_consistent_estimate() -> None:
    # курица: ккал в пределах допуска от Атвотера → не трогаем
    assert reconcile_density(CHICKEN) == CHICKEN


def test_reconcile_ignores_blank() -> None:
    blank = Density(kcal_per_100g=0, protein_per_100g=0, fat_per_100g=0, carb_per_100g=0)
    assert reconcile_density(blank) == blank  # пустую отдаём как есть (blank-retry разберётся)
