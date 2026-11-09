"""Серия записанных дней (D-44, D-49): полнота учёта, а не «дни в цели».

Серия по «дням в цели» толкала бы недозаписать перебор (MA-INV-21), поэтому считаем другое:
день в серии — тот, что записан полностью. Пока кнопки «✅ всё записал» нет, «полностью» —
итог не ниже 70 % цели (`pace.COMPLETE_SHARE`, тот же порог «дошедшего дня», что у нормы §6.20).

Заморозка: один незачтённый день на ISO-неделю серию не рвёт и в счёт не идёт. Без неё один
пропуск ставит выбор «записать мусор ради серии или бросить» — оба портят данные.
Сегодня входит, только когда уже прошло порог, и никогда не рвёт серию: день ещё идёт.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from app.domain.pace import COMPLETE_SHARE


@dataclass(frozen=True)
class Streak:
    days: int  # засчитанных дней подряд (заморозки внутри не считаются)
    frozen: bool  # на неделе сегодняшнего дня заморозка уже потрачена


def logged_streak(days: Sequence[tuple[date, float]], goal_kcal: float, today: date) -> Streak:
    """`days` — (дата, ккал) за окно без дыр; может включать `today`. Цель — текущая."""
    if goal_kcal <= 0:
        return Streak(0, False)
    bar = goal_kcal * COMPLETE_SHARE
    kcal = dict(days)
    count = 1 if kcal.get(today, 0.0) >= bar else 0
    used: set[tuple[int, ...]] = set()  # (год, неделя) с потраченной заморозкой
    for day in sorted((d for d in kcal if d < today), reverse=True):
        if kcal[day] >= bar:
            count += 1
            continue
        week = tuple(day.isocalendar())[:2]
        if week in used:
            break
        used.add(week)
    return Streak(count, tuple(today.isocalendar())[:2] in used)


def demo() -> None:
    """Runnable-чек AC-275."""
    from datetime import timedelta

    today = date(2026, 11, 11)  # среда
    goal = 2000.0

    def series(values: list[float]) -> list[tuple[date, float]]:
        start = today - timedelta(days=len(values) - 1)
        return [(start + timedelta(days=i), v) for i, v in enumerate(values)]

    # 10 полных дней, сегодня 300 ккал — сегодня не рвёт и не считается.
    assert logged_streak(series([1800.0] * 10 + [300.0]), goal, today) == Streak(10, False)
    # Сегодня уже прошло порог — +1.
    assert logged_streak(series([1800.0] * 10 + [1500.0]), goal, today).days == 11
    # Пропуск в понедельник этой недели съела заморозка: 9 полных, заморозка недели потрачена.
    assert logged_streak(series([1800.0] * 8 + [800.0, 1800.0, 300.0]), goal, today) == Streak(
        9, True
    )
    # Два пропуска в одну неделю — серия обрывается на втором: 0.
    assert logged_streak(series([1800.0] * 8 + [800.0, 800.0, 300.0]), goal, today) == Streak(
        0, True
    )
    # Пропуски в разные недели — у каждого своя заморозка.
    vals = [1800.0] * 3 + [0.0] + [1800.0] * 5 + [0.0, 1800.0, 300.0]
    assert logged_streak(series(vals), goal, today) == Streak(9, True)
    # Нет цели — нет серии.
    assert logged_streak(series([1800.0] * 5), 0.0, today) == Streak(0, False)
    print("серия: ок")


if __name__ == "__main__":
    demo()
