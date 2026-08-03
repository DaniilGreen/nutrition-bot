"""Цели по КБЖУ: расчёт остатка на день и вывод цели из профиля (Mifflin)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.domain.value_objects import Macros

# Коэффициенты активности (множитель к BMR).
ACTIVITY_FACTORS = {"low": 1.2, "medium": 1.375, "high": 1.55}
# Коэффициенты цели: дефицит для похудения / поддержание / профицит.
TARGET_FACTORS = {"cut": 0.85, "maintain": 1.0, "bulk": 1.1}


@dataclass(frozen=True, slots=True)
class Goal:
    daily_kcal: float
    protein_g: float
    fat_g: float
    carb_g: float
    target: str = "maintain"  # cut | maintain | bulk — от неё зависит раскладка (см. rebalance)

    @property
    def atwater_kcal(self) -> float:
        """Калории, сходящиеся с макросами: 4Б + 9Ж + 4У. ИНВАРИАНТ: == daily_kcal (см. INV-15)."""
        return self.protein_g * 4 + self.fat_g * 9 + self.carb_g * 4


@dataclass(frozen=True, slots=True)
class Remaining:
    consumed: Macros
    goal: Goal

    @property
    def kcal_left(self) -> float:
        return round(self.goal.daily_kcal - self.consumed.kcal, 1)

    @property
    def protein_left(self) -> float:
        return round(self.goal.protein_g - self.consumed.protein, 1)

    @property
    def fat_left(self) -> float:
        return round(self.goal.fat_g - self.consumed.fat, 1)

    @property
    def carb_left(self) -> float:
        return round(self.goal.carb_g - self.consumed.carb, 1)


def remaining(consumed: Macros, goal: Goal) -> Remaining:
    return Remaining(consumed=consumed, goal=goal)


# «День в цели»: коридор калорий как доля дневной цели — ЕДИНЫЙ критерий для /streak,
# хитмапа и /month (иначе соседние экраны считают «дни в цели» по-разному).
IN_GOAL_LOW, IN_GOAL_HIGH = 0.5, 1.1


def in_goal(kcal: float, goal_kcal: float) -> bool:
    """День «в цели» = 0.5–1.1× дневной цели. Без заданной цели — любой залогированный день."""
    if goal_kcal <= 0:
        return kcal > 0
    return IN_GOAL_LOW * goal_kcal <= kcal <= IN_GOAL_HIGH * goal_kcal


def mifflin_st_jeor(
    *,
    sex: Literal["male", "female"],
    weight_kg: float,
    height_cm: float,
    age: int,
    activity: float = 1.375,
) -> float:
    """Дневная норма калорий (BMR × коэффициент активности)."""
    bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + (5 if sex == "male" else -161)
    return round(bmr * activity, 0)


# --- Раскладка калорий на макросы: БЕЛОК — ЯКОРЬ, ЖИР — ПОЛ, УГЛЕВОДЫ — ОСТАТОК ---
#
# Белок якорим на МАССУ ТЕЛА, а не на долю калорий. Доля калорий означала бы: срезал калории —
# срезал белок, а это ровно то, из-за чего на дефиците уходят мышцы (старый /goal так и делал:
# 30% ккал).
#
# Больше всего белка нужно на СУШКЕ — вопреки распространённой интуиции «на массе больше». В
# дефиците организм добирает аминокислоты из мышц, и высокий белок — единственная защита
# (Longland 2016: 2.4 vs 1.2 г/кг при дефиците 40% — первая группа НАБРАЛА сухую массу). На массе
# калорий вдоволь, потребность на кг ниже: прирост выходит на плато ~1.6 г/кг (Morton 2018).
PROTEIN_PER_KG = {"cut": 2.2, "maintain": 1.8, "bulk": 2.0}
# Жир — доля калорий, но НЕ ниже пола: гормоны и жирорастворимые витамины. На сушке долю поднимаем
# (сытость на низких калориях), на массе опускаем — освобождаем место углеводам под тренировки.
FAT_RATIO = {"cut": 0.30, "maintain": 0.28, "bulk": 0.25}
FAT_FLOOR_PER_KG = 0.8

# Углеводы — ОСТАТОК. Отсюда само собой выходит то, чего и ждёшь: на сушке калорий мало → углеводов
# мало; на массе много → углеводов много. Специально их не крутим.


def rebalance(kcal: float, weight_kg: float, target: str = "maintain") -> Goal:
    """Разложить калории на макросы под цель. ИНВАРИАНТ: 4Б+9Ж+4У == daily_kcal (INV-15).

    Итоговые ккал берём ОТ РЕАЛЬНЫХ макросов, а не от запрошенных: после округления граммов
    сумма разошлась бы с числом на экране, и бот бы врал. Если запрошенные калории ниже, чем
    белок+пол жира (нереалистичная цель), — вернём честный минимум, а не углеводы в минус.
    """
    protein_g = round(PROTEIN_PER_KG.get(target, 1.8) * weight_kg)
    fat_g = round(max(kcal * FAT_RATIO.get(target, 0.28) / 9, FAT_FLOOR_PER_KG * weight_kg))
    carb_g = round(max(0.0, kcal - protein_g * 4 - fat_g * 9) / 4)
    return goal_from_macros(protein_g, fat_g, carb_g, target)


def goal_from_macros(
    protein_g: float, fat_g: float, carb_g: float, target: str = "maintain"
) -> Goal:
    """Цель от макросов: калории СЧИТАЮТСЯ (4Б+9Ж+4У), а не задаются. Обратное к rebalance."""
    kcal = round(protein_g * 4 + fat_g * 9 + carb_g * 4)
    return Goal(daily_kcal=kcal, protein_g=protein_g, fat_g=fat_g, carb_g=carb_g, target=target)


def set_macro(goal: Goal, macro: Literal["protein", "fat", "carb"], grams: float) -> Goal:
    """Подвинуть ОДИН макрос, сохранив калорийность: разницу съедает другой (FR-16.1).

    Кто съедает — по иерархии защиты (та же, что в `rebalance`): белок якорь, жир имеет пол,
    углеводы остаток. Двигаем белок/жир → платят углеводы; двигаем углеводы → платит жир
    (белок самый защищённый), а если жир упёрся в ноль — тогда белок.

    Невыполнимую цель не подделываем: поглотитель зажимается в 0, а ккал пересчитываются ЧЕСТНО
    от получившихся макросов — иначе поехал бы INV-15 (4Б+9Ж+4У == daily_kcal).
    """
    per_g = {"protein": 4.0, "fat": 9.0, "carb": 4.0}
    new = {"protein": goal.protein_g, "fat": goal.fat_g, "carb": goal.carb_g}
    new[macro] = max(0.0, round(grams))

    absorbers = ["fat", "protein"] if macro == "carb" else ["carb"]
    for absorber in absorbers:
        delta = sum(new[m] * per_g[m] for m in new) - goal.daily_kcal
        if abs(delta) < 0.5:  # уже сошлось — второй поглотитель не трогаем
            break
        new[absorber] = max(0.0, round(new[absorber] - delta / per_g[absorber]))

    # ккал берём ОТ макросов, а не оставляем запрошенные: после округления и зажима в 0 они
    # разошлись бы с числом на экране, и бот бы врал.
    return goal_from_macros(new["protein"], new["fat"], new["carb"], goal.target)


def goal_from_profile(
    *,
    sex: Literal["male", "female"],
    weight_kg: float,
    height_cm: float,
    age: int,
    activity: str,
    target: str,
) -> Goal:
    """Цель из профиля: Mifflin × активность × цель → та же раскладка, что и у /goal (rebalance).

    ОДНА схема на весь бот: раньше /goal считал белок как 30% калорий, а визард — 2 г/кг, и цель
    зависела от того, каким путём её задали.
    """
    tdee = mifflin_st_jeor(
        sex=sex,
        weight_kg=weight_kg,
        height_cm=height_cm,
        age=age,
        activity=ACTIVITY_FACTORS.get(activity, 1.375),
    )
    return rebalance(round(tdee * TARGET_FACTORS.get(target, 1.0)), weight_kg, target)
