from __future__ import annotations

from app.domain.goals import (
    Goal,
    goal_from_macros,
    goal_from_profile,
    in_goal,
    mifflin_st_jeor,
    rebalance,
    remaining,
    set_macro,
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


# --- FR-16.1: подвинуть один макрос, не трогая калорийность (баг-репорт #25) ---

_BASE = goal_from_macros(150, 65, 250)  # 2185 ккал — считаются от макросов, INV-15


def test_set_protein_keeps_kcal_and_carbs_pay() -> None:
    """Просили ровно это: «выставить белок 180, калории оставить как есть»."""
    g = set_macro(_BASE, "protein", 180)
    assert g.protein_g == 180
    assert g.fat_g == _BASE.fat_g  # жир не трогали
    assert g.carb_g == 220  # +30 г белка = 120 ккал → −30 г углеводов
    assert g.daily_kcal == _BASE.daily_kcal


def test_set_carb_is_paid_by_fat_not_protein() -> None:
    """Двигаем углеводы → платит жир: белок самый защищённый, его не трогаем.

    Ккал держатся с точностью до округления граммов: жир 9 ккал/г, целыми граммами дельту
    −200 ккал не поглотить (22 г = 198). Показываем ЧЕСТНУЮ сумму от реальных макросов,
    а не заявленную — тот же принцип, что в `rebalance`.
    """
    g = set_macro(_BASE, "carb", 200)
    assert g.carb_g == 200
    assert g.protein_g == _BASE.protein_g  # белок неприкосновенен
    assert g.fat_g == round(_BASE.fat_g + 200 / 9)  # −200 ккал углеводов ушли в жир
    assert abs(g.daily_kcal - _BASE.daily_kcal) <= 9  # не больше одного грамма жира


def test_impossible_target_does_not_push_carbs_negative() -> None:
    """Белок больше, чем весь дневной бюджет: углеводы в 0, ккал честно вырастают."""
    g = set_macro(_BASE, "protein", 600)
    assert g.protein_g == 600
    assert g.carb_g == 0  # не уходим в минус
    assert g.daily_kcal > _BASE.daily_kcal  # и не врём, что уложились в прежние ккал


def test_set_macro_never_breaks_atwater_invariant() -> None:
    """INV-15: 4Б+9Ж+4У == daily_kcal — на любом входе, включая абсурдный."""
    for macro in ("protein", "fat", "carb"):
        for grams in (0, 1, 77, 250, 999):
            g = set_macro(_BASE, macro, grams)  # type: ignore[arg-type]
            assert abs(g.atwater_kcal - g.daily_kcal) < 1, (macro, grams)


def test_set_macro_to_current_value_changes_nothing() -> None:
    """Ставим то же значение — цель обязана остаться прежней (нет дрейфа от округлений)."""
    g = set_macro(_BASE, "protein", _BASE.protein_g)
    assert (g.protein_g, g.fat_g, g.carb_g, g.daily_kcal) == (
        _BASE.protein_g,
        _BASE.fat_g,
        _BASE.carb_g,
        _BASE.daily_kcal,
    )


def test_форма_один_макрос_калории_те_же():
    """Баг 17.09.2026: белок 190 → 90 сохранялся, а калории и углеводы стояли."""
    from app.domain.goals import edit_goal

    cur = Goal(2176, 190, 72, 188, "cut")
    g, what = edit_goal(cur, kcal=2176, protein_g=90, fat_g=72, carb_g=188)
    assert what == "protein"
    # (2176 − 4·90 − 9·72) / 4 = 292: калории сохраняются, даже если старая цель
    # сама была несогласована (2176 при макросах на 2160).
    assert (g.protein_g, g.fat_g) == (90, 72) and g.carb_g == 292
    assert g.daily_kcal == 2176
    assert g.daily_kcal == 4 * g.protein_g + 9 * g.fat_g + 4 * g.carb_g


def test_форма_только_калории_берут_углеводы():
    from app.domain.goals import edit_goal

    cur = Goal(2176, 190, 72, 188, "cut")
    g, what = edit_goal(cur, kcal=1900, protein_g=190, fat_g=72, carb_g=188)
    assert what == "kcal" and (g.protein_g, g.fat_g) == (190, 72) and g.carb_g == 123
    # Калорий меньше, чем белок и жир, — углеводы 0, жир уступает, но не в минус.
    low, _ = edit_goal(cur, kcal=900, protein_g=190, fat_g=72, carb_g=188)
    assert low.carb_g == 0 and low.fat_g == 16 and low.protein_g == 190
    assert low.daily_kcal == 4 * 190 + 9 * 16


def test_форма_несколько_чисел_калории_от_макросов():
    from app.domain.goals import edit_goal

    cur = Goal(2176, 190, 72, 188, "cut")
    g, what = edit_goal(cur, kcal=3000, protein_g=150, fat_g=60, carb_g=200)
    assert what == "macros" and g.daily_kcal == 150 * 4 + 60 * 9 + 200 * 4
    ok = Goal(2160, 190, 72, 188, "cut")      # согласованная: 760 + 648 + 752
    same, what = edit_goal(ok, kcal=2160, protein_g=190, fat_g=72, carb_g=188)
    assert what == "none" and same == ok


def test_несогласованная_цель_выравнивается_при_сохранении():
    """Цель владельца после бага: 2176 ккал при макросах на 1760."""
    from app.domain.goals import edit_goal

    bad = Goal(2176, 90, 72, 188, "cut")
    g, what = edit_goal(bad, kcal=2176, protein_g=90, fat_g=72, carb_g=188)
    assert what == "kcal" and g.carb_g == 292 and g.daily_kcal == 2176
