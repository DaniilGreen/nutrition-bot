from __future__ import annotations

from app.domain.goals import Goal, goal_from_profile, in_goal, mifflin_st_jeor, remaining
from app.domain.value_objects import Macros


def test_in_goal_corridor() -> None:
    # единый критерий «день в цели» = 0.5–1.1× цели (для /streak, хитмапа, /month)
    assert in_goal(2000, 2000) is True
    assert in_goal(1000, 2000) is True  # ровно 50% — нижняя граница
    assert in_goal(2200, 2000) is True  # ровно 110% — верхняя граница
    assert in_goal(999, 2000) is False  # <50% — недобор
    assert in_goal(2300, 2000) is False  # >110% — перебор
    assert in_goal(0, 2000) is False  # нет лога
    assert in_goal(300, 0) is True  # без цели любой kcal>0 засчитан


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


def test_goal_from_profile_cut() -> None:
    g = goal_from_profile(
        sex="male", weight_kg=80, height_cm=180, age=30, activity="medium", target="cut"
    )
    # TDEE = round(1780*1.375)=2448; cut = round(2448*0.85)=2081
    assert g.daily_kcal == 2081
    assert g.protein_g == 160  # 2 г/кг * 80
    assert g.fat_g == round(2081 * 0.25 / 9)
    assert g.carb_g >= 0
