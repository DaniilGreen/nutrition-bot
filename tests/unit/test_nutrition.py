from __future__ import annotations

import pytest

from app.domain.nutrition import macros_for_grams
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
