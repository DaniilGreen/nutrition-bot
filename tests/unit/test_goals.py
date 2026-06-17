from __future__ import annotations

from app.domain.goals import Goal, mifflin_st_jeor, remaining
from app.domain.value_objects import Macros


def test_remaining() -> None:
    rem = remaining(Macros(500, 40, 20, 50), Goal(2000, 150, 65, 250))
    assert rem.kcal_left == 1500
    assert rem.protein_left == 110
    assert rem.fat_left == 45
    assert rem.carb_left == 200


def test_mifflin_male() -> None:
    val = mifflin_st_jeor(sex="male", weight_kg=80, height_cm=180, age=30, activity=1.375)
    # BMR = 10*80 + 6.25*180 - 5*30 + 5 = 1780; * 1.375
    assert val == round(1780 * 1.375)
