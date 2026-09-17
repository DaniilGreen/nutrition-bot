"""Энергобаланс между замерами состава тела (MINIAPP-SPEC §6.24, «Реальный расход»).

Два вопроса: сколько человек на самом деле тратит в день и сошёлся ли дневник
с тем, что ушло из тела. Числа — Hall 2008: жировая масса 9 400 ккал/кг,
безжировая 1 800 ккал/кг; для веса смешанного состава — 7 700.

Главное здесь — погрешность. InBody между днями расходится до 0.7 кг жира в
строгих условиях; в жизни берём килограмм. Погрешность расхода считается честно
и решает, можно ли предлагать цель (MA-INV-53).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

KCAL_PER_KG_FAT = 9400.0
KCAL_PER_KG_LEAN = 1800.0
KCAL_PER_KG_WEIGHT = 7700.0

SCAN_FAT_ERROR_KG = 1.0
MIN_WINDOW_DAYS = 14
MIN_LOGGED_SHARE = 0.8
MAX_PROPOSE_ERROR = 250.0
# «Сходится», если разница не больше этого: шум двух замеров ±1.4 кг.
AGREE_KG = 1.5
# День для расчёта расхода — съедено хотя бы половину цели: недописанный день
# занизил бы расход, и цель предложилась бы ниже нужной.
FULL_DAY_SHARE = 0.5
# Окно расхода: не старше, но и не короче.
WINDOW_MAX_DAYS = 120
WINDOW_MIN_DAYS = 28


@dataclass(frozen=True)
class Expenditure:
    verdict: str                 # ok | short_window | few_logs
    days: int
    logged_days: int
    intake_kcal: float           # в записанный день
    tdee_kcal: float | None
    error_kcal: float | None
    proposable: bool


def expenditure(
    *, days: int, logged_days: int, logged_kcal_total: float,
    d_fat_kg: float, d_lean_kg: float,
) -> Expenditure:
    intake = logged_kcal_total / logged_days if logged_days else 0.0
    if days < MIN_WINDOW_DAYS:
        return Expenditure("short_window", days, logged_days, round(intake), None, None, False)
    if logged_days < MIN_LOGGED_SHARE * days:
        return Expenditure("few_logs", days, logged_days, round(intake), None, None, False)
    stored = d_fat_kg * KCAL_PER_KG_FAT + d_lean_kg * KCAL_PER_KG_LEAN
    tdee = intake - stored / days
    error = math.sqrt(2) * SCAN_FAT_ERROR_KG * KCAL_PER_KG_FAT / days
    return Expenditure(
        "ok", days, logged_days, round(intake),
        float(round(tdee / 10) * 10), float(round(error / 10) * 10),
        error <= MAX_PROPOSE_ERROR,
    )


def window(scan_times: list[datetime]) -> tuple[datetime, datetime] | None:
    """Окно расхода: от самого раннего замера за 120 дней до последнего, если
    между ними хотя бы 28 дней. Расход падает по мере похудения — полугодовое
    окно считало бы прошлого человека."""
    if len(scan_times) < 2:
        return None
    times = sorted(scan_times)
    last = times[-1]
    fits = [t for t in times[:-1] if WINDOW_MIN_DAYS <= (last - t).days <= WINDOW_MAX_DAYS]
    return (fits[0], last) if fits else None


@dataclass(frozen=True)
class DiaryCheck:
    verdict: str                 # waiting | agree | less_lost | more_lost | few_logs
    deficit_kcal: float          # по записанным дням, плюс — дефицит
    expected_kg: float           # ожидаемое изменение веса, минус — потеря
    actual_kg: float | None
    logged_days: int
    days: int


def diary_check(
    *, days: int, logged_days: int, logged_kcal_total: float,
    burn_kcal: float, actual_weight_kg: float | None,
) -> DiaryCheck:
    """Сколько дневник обещал и сколько показали весы.

    `burn_kcal` — расход в день (обмен × активность). Дефицит — только по
    записанным дням: пустой день неизвестен, выдумывать его не будем.
    """
    deficit = burn_kcal * logged_days - logged_kcal_total
    expected = -deficit / KCAL_PER_KG_WEIGHT
    if actual_weight_kg is None:
        verdict = "waiting"
    elif days >= MIN_WINDOW_DAYS and logged_days < MIN_LOGGED_SHARE * days:
        verdict = "few_logs"
    else:
        gap = actual_weight_kg - expected
        if abs(gap) <= max(AGREE_KG, 0.3 * abs(expected)):
            verdict = "agree"
        elif gap > 0:
            verdict = "less_lost"
        else:
            verdict = "more_lost"
    return DiaryCheck(verdict, float(round(deficit)), round(expected, 1),
                      actual_weight_kg, logged_days, days)


def demo() -> None:
    # Месяц между замерами: погрешность ±440, цель не предлагаем (AC-MA-57).
    m = expenditure(days=30, logged_days=28, logged_kcal_total=2100 * 28,
                    d_fat_kg=-1.5, d_lean_kg=-0.2)
    assert m.verdict == "ok" and m.error_kcal == 440 and not m.proposable
    assert m.tdee_kcal == round((2100 + (1.5 * 9400 + 0.2 * 1800) / 30) / 10) * 10

    # Два месяца — погрешность 220, можно предлагать.
    two = expenditure(days=60, logged_days=55, logged_kcal_total=2100 * 55,
                      d_fat_kg=-3.0, d_lean_kg=-0.5)
    assert two.error_kcal == 220 and two.proposable
    assert two.tdee_kcal == 2580, two.tdee_kcal    # 2100 + (28200 + 900) / 60 = 2585

    # AC-MA-58: записано 60% дней — числа нет.
    few = expenditure(days=60, logged_days=36, logged_kcal_total=2000 * 36,
                      d_fat_kg=-3, d_lean_kg=0)
    assert few.verdict == "few_logs" and few.tdee_kcal is None
    assert expenditure(days=7, logged_days=7, logged_kcal_total=1,
                       d_fat_kg=0, d_lean_kg=0).verdict == "short_window"

    assert window([datetime(2026, 5, 3), datetime(2026, 6, 2), datetime(2026, 7, 5)]) == (
        datetime(2026, 5, 3), datetime(2026, 7, 5))
    # Последняя пара ближе 28 дней — берём более ранний.
    assert window([datetime(2026, 5, 3), datetime(2026, 5, 20)]) is None
    assert window([datetime(2026, 1, 1), datetime(2026, 5, 3), datetime(2026, 6, 10)]) == (
        datetime(2026, 5, 3), datetime(2026, 6, 10))
    assert window([datetime(2026, 5, 3)]) is None

    # Сверка: дефицит 41 000 → около −5.3 кг, пока без второго замера.
    w = diary_check(days=60, logged_days=50, logged_kcal_total=50 * 1827,
                    burn_kcal=2647, actual_weight_kg=None)
    assert w.verdict == "waiting" and w.deficit_kcal == 41000 and w.expected_kg == -5.3
    # Ушло 5 кг из 5.3 — сходится; ушло 1 кг — меньше ожидаемого.
    assert diary_check(days=60, logged_days=50, logged_kcal_total=50 * 1827,
                       burn_kcal=2647, actual_weight_kg=-5.0).verdict == "agree"
    assert diary_check(days=60, logged_days=50, logged_kcal_total=50 * 1827,
                       burn_kcal=2647, actual_weight_kg=-1.0).verdict == "less_lost"
    assert diary_check(days=60, logged_days=50, logged_kcal_total=50 * 1827,
                       burn_kcal=2647, actual_weight_kg=-9.0).verdict == "more_lost"
    # Маленький ожидаемый сдвиг — допуск не меньше полутора килограммов (шум замеров).
    assert diary_check(days=30, logged_days=30, logged_kcal_total=30 * 2600,
                       burn_kcal=2647, actual_weight_kg=1.3).verdict == "agree"
    assert diary_check(days=30, logged_days=10, logged_kcal_total=10 * 2000,
                       burn_kcal=2647, actual_weight_kg=-1).verdict == "few_logs"
    print("энергобаланс: ок")


if __name__ == "__main__":
    demo()
