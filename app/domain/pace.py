"""Темп дня: сравнение сегодняшнего набора калорий с личной нормой к этому часу (§6.20).

Дневник теряет не дни целиком, а вторую половину дня: полный день владельца идёт до 20:24
и состоит из семи приёмов, неполный обрывается в 15:20 на трёх. Ужин — самый крупный приём —
не попадает в учёт. Здесь считается, как выглядит его обычный день по часам и насколько
сегодняшний от него отстал.

Чистая арифметика: ни БД, ни модели. Решение «показывать ли карточку» стоит дорого в обе
стороны — промолчать значит потерять запись, сказать зря значит попрекнуть человека
ни за что, — поэтому оно проверяется тестом, а не глазами на проде.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

HOURS = 24
# Доля цели, при которой считаем, что день довели до конца и он годится в норму.
COMPLETE_SHARE = 0.7
# Меньше пяти дошедших дней — нормы нет (MA-INV-37). Норма по двум дням это совпадение.
MIN_DAYS = 5
# Отставание считается настоящим, только если сработали ОБА порога: доля ловит
# крупные дни, абсолют не даёт придираться к сотне килокалорий.
BEHIND_SHARE = 0.7
BEHIND_KCAL = 300.0
# Человек, который записывает прямо сейчас, в подсказке не нуждается.
QUIET_MINUTES = 90


@dataclass(frozen=True)
class Pace:
    """Норма по часам и то, что нужно, чтобы решить про карточку."""

    norm: list[float]          # накопленные ккал к концу часа 0..23
    days_used: int


def _cumulative(meals: Sequence[tuple[int, float]]) -> list[float]:
    """Накопленные калории к концу каждого часа одного дня."""
    per_hour = [0.0] * HOURS
    for hour, kcal in meals:
        if 0 <= hour < HOURS:
            per_hour[hour] += kcal
    total = 0.0
    out = []
    for value in per_hour:
        total += value
        out.append(total)
    return out


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def norm_curve(days: Sequence[Sequence[tuple[int, float]]], goal_kcal: float) -> Pace:
    """Личная норма: медиана накопленных калорий по дням, ДОШЕДШИМ до конца.

    Порог по цели обязателен. Без него норма строилась бы в том числе по брошенным
    дням — то есть по недозаписи, — и сравнивать сегодняшний день было бы не с чем:
    норма опустилась бы ровно до уровня проблемы.

    Медиана, а не среднее: один день с 4000 ккал не должен задирать планку, по которой
    человека потом спрашивают.
    """
    if goal_kcal <= 0:
        return Pace(norm=[0.0] * HOURS, days_used=0)
    complete = [
        _cumulative(day) for day in days
        if sum(kcal for _, kcal in day) >= goal_kcal * COMPLETE_SHARE
    ]
    if len(complete) < MIN_DAYS:
        return Pace(norm=[0.0] * HOURS, days_used=len(complete))
    norm = [_median([day[h] for day in complete]) for h in range(HOURS)]
    return Pace(norm=norm, days_used=len(complete))


def behind(pace: Pace, hour: int, today_kcal: float) -> bool:
    """Отстал ли сегодняшний день от нормы настолько, что стоит спросить."""
    if pace.days_used < MIN_DAYS or not 0 <= hour < HOURS:
        return False
    expected = pace.norm[hour]
    if expected <= 0:
        return False
    gap = expected - today_kcal
    return gap >= BEHIND_KCAL and today_kcal < expected * BEHIND_SHARE


def quiet_enough(minutes_since_last: float | None) -> bool:
    """True, если пора спрашивать: с последней записи прошло достаточно.

    None — записей сегодня нет вовсе; это как раз тот день, ради которого всё
    и делается, и молчать про него нельзя.
    """
    return minutes_since_last is None or minutes_since_last >= QUIET_MINUTES


def curve_points(
    values: Sequence[float], first_hour: int, last_hour: int
) -> list[tuple[int, float]]:
    """Кривая для графика: от первого часа записей до текущего, по одной точке на час."""
    lo = max(0, min(first_hour, HOURS - 1))
    hi = max(lo, min(last_hour, HOURS - 1))
    return [(h, round(values[h], 1)) for h in range(lo, hi + 1)]


def demo() -> None:
    """Runnable-чек: норма считается по дошедшим дням, отставание — по обоим порогам."""
    goal = 2000.0
    # Шесть дней, доведённых до конца: завтрак, обед, ужин.
    full = [[(9, 500.0), (13, 700.0), (20, 800.0)] for _ in range(6)]
    # И три брошенных после обеда — они не должны попасть в норму.
    dropped = [[(11, 300.0), (15, 400.0)] for _ in range(3)]
    pace = norm_curve(full + dropped, goal)
    assert pace.days_used == 6, pace.days_used
    assert pace.norm[9] == 500.0
    assert pace.norm[13] == 1200.0
    assert pace.norm[20] == 2000.0
    # Брошенные дни норму не опустили: к 20:00 она осталась полной.
    assert pace.norm[20] == 2000.0

    # Отставание: 990 против нормы 1200 к 13:00 — разрыв 210, меньше порога.
    assert not behind(pace, 13, 990.0)
    # 700 против 2000 к 20:00 — разрыв 1300 и доля 0.35: спрашиваем.
    assert behind(pace, 20, 700.0)
    # 1500 против 2000 — разрыв 500, но доля 0.75: день идёт нормально, молчим.
    assert not behind(pace, 20, 1500.0)

    # Мало дошедших дней — нормы нет и карточки нет (MA-INV-37).
    thin = norm_curve(full[:3] + dropped, goal)
    assert thin.days_used == 3
    assert not behind(thin, 20, 0.0)
    # Нет цели — считать не от чего.
    assert norm_curve(full, 0.0).days_used == 0

    # Пока человек записывает, не лезем; через полтора часа можно.
    assert not quiet_enough(20.0)
    assert quiet_enough(95.0)
    assert quiet_enough(None)      # записей сегодня нет вовсе

    pts = curve_points(pace.norm, 9, 13)
    assert pts[0] == (9, 500.0) and pts[-1] == (13, 1200.0) and len(pts) == 5
    print("темп дня: ок")


if __name__ == "__main__":
    demo()
