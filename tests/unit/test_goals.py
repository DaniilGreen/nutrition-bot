from __future__ import annotations

from app.domain.goals import (
    Goal,
    goal_from_macros,
    goal_from_profile,
    in_goal,
    mifflin_st_jeor,
    rebalance,
    remaining,
)
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
    # TDEE = round(1780*1.375)=2448; cut = round(2448*0.85)=2081 → раскладка через rebalance
    assert g.protein_g == round(2.2 * 80)  # белок ЯКОРЬ на массу, на сушке самый высокий
    assert g.fat_g == round(2081 * 0.30 / 9)  # 30% ккал, выше пола 0.8 г/кг
    assert g.carb_g >= 0
    assert g.target == "cut"
    assert g.daily_kcal == g.atwater_kcal  # INV-15: ккал сходятся с макросами


def test_atwater_invariant_holds_for_every_target() -> None:
    """ИНВАРИАНТ: 4Б+9Ж+4У == daily_kcal. Иначе бот показывает ккал, не сходящиеся с макросами."""
    for target in ("cut", "maintain", "bulk"):
        g = rebalance(2200, 86, target)
        assert g.daily_kcal == g.atwater_kcal, target


def test_cut_has_less_carbs_and_bulk_more() -> None:
    """Интуиция владельца: на сушке углеводов меньше, на массе больше, и белка на массе больше,
    чем на поддержании. Выпадает само: белок якорь, жир пол, углеводы остаток."""
    cut = rebalance(1900, 86, "cut")
    maintain = rebalance(2400, 86, "maintain")
    bulk = rebalance(2900, 86, "bulk")
    assert cut.carb_g < maintain.carb_g < bulk.carb_g  # углеводы — буфер калорий
    assert bulk.protein_g > maintain.protein_g  # на массе белка больше, чем на поддержании


def test_protein_does_not_shrink_when_cutting_calories() -> None:
    """Главный фикс: раньше /goal считал белок как 30% калорий — срезал калории, срезал белок.
    Теперь белок якорится на вес и от калорий не зависит."""
    high = rebalance(2600, 86, "cut")
    low = rebalance(1600, 86, "cut")
    assert high.protein_g == low.protein_g == round(2.2 * 86)


def test_goal_from_macros_computes_kcal() -> None:
    g = goal_from_macros(180, 80, 200, "cut")
    assert g.daily_kcal == 2240 == 180 * 4 + 80 * 9 + 200 * 4
    assert g.daily_kcal == g.atwater_kcal


def test_fat_never_below_floor_on_low_kcal() -> None:
    """Пол жира: на низких калориях 30% дали бы меньше 0.8 г/кг — гормоны страдают."""
    g = rebalance(1200, 86, "cut")
    assert g.fat_g >= round(0.8 * 86)
