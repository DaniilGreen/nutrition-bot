"""Серия записанных дней и покрытие (D-44, D-57): полнота учёта, а не «дни в цели».

Серия по «дням в цели» толкала бы недозаписать перебор (MA-INV-21), поэтому считаем другое:
день в серии — тот, где записан хотя бы один приём или который человек закрыл кнопкой
«✅ всё записал» вечернего итога (D-57, решение владельца 10.11.2026). Порога калорий нет.

Заморозка: один незачтённый день на ISO-неделю серию не рвёт и в счёт не идёт. Без неё один
пропуск ставит выбор «записать мусор ради серии или бросить» — оба портят данные.
Сегодня входит, когда уже записано, и никогда не рвёт серию: день ещё идёт.
Дни до самой первой записи человека — не пропуски: история там просто не начиналась
(новичок со среды не видит ❄ за понедельник и вторник).

Покрытие «N из 30» (D-44): засчитанных дней из последних 30, считая с первой записи; сегодня
в знаменателе, только когда уже засчитано.
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, timedelta

COVERAGE_DAYS = 30


@dataclass(frozen=True)
class Streak:
    days: int  # засчитанных дней подряд (заморозки внутри не считаются)
    frozen: bool  # на неделе сегодняшнего дня заморозка уже потрачена
    covered: int = 0  # засчитанных дней в окне покрытия
    of: int = 0  # дней в окне покрытия (≤ 30; меньше — история короче)


def _week(d: date) -> tuple[int, int]:
    iso = d.isocalendar()
    return iso[0], iso[1]


def logged_streak(counted: Collection[date], today: date) -> Streak:
    """`counted` — все засчитанные дни человека (приём или «всё записал»); может включать
    `today` и будущее не включает. Пусто — серии и покрытия нет."""
    days = {d for d in counted if d <= today}
    if not days:
        return Streak(0, False)
    first = min(days)
    count = 1 if today in days else 0
    used: set[tuple[int, int]] = set()  # (год, неделя) с потраченной заморозкой
    day = today - timedelta(days=1)
    while day >= first:
        if day in days:
            count += 1
        else:
            week = _week(day)
            if week in used:
                break
            used.add(week)
        day -= timedelta(days=1)
    start = max(first, today - timedelta(days=COVERAGE_DAYS - 1))
    span = (today - start).days + (1 if today in days else 0)
    covered = sum(1 for d in days if start <= d <= today)
    return Streak(count, _week(today) in used, covered, span)


def demo() -> None:
    """Runnable-чек AC-275."""
    today = date(2026, 11, 11)  # среда

    def back(*offsets: int) -> set[date]:
        return {today - timedelta(days=o) for o in offsets}

    # 10 дней подряд до вчера, сегодня ещё пусто — сегодня не рвёт и не считается.
    assert logged_streak(back(*range(1, 11)), today) == Streak(10, False, 10, 10)
    # Сегодня уже записано — +1; порога калорий нет.
    assert logged_streak(back(*range(0, 11)), today).days == 11
    # Пропуск в понедельник этой недели съела заморозка: 9 дней, заморозка недели потрачена.
    s = logged_streak(back(1, *range(3, 11)), today)
    assert (s.days, s.frozen) == (9, True)
    # Два пропуска в одну неделю — серия обрывается на втором: 0.
    assert logged_streak(back(*range(3, 11)), today).days == 0
    # Новичок: первая запись в понедельник — дни до неё не пропуски, ❄ нет.
    assert logged_streak(back(2, 1, 0), today) == Streak(3, False, 3, 3)
    # Длинная серия не упирается в окно: 400 дней подряд — 400.
    long = logged_streak(back(*range(0, 400)), today)
    assert long.days == 400 and (long.covered, long.of) == (30, 30)
    # Ничего не записано — ничего нет.
    assert logged_streak(set(), today) == Streak(0, False)
    print("серия: ок")


if __name__ == "__main__":
    demo()
