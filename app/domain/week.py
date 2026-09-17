"""Недельный баланс (MINIAPP-SPEC §6.23, FR-35).

Одна функция на бот и аппку. Сервис собирает съеденное по дням недели, домен
решает, что из этого следует: баланс, «можно в день», вердикт.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Компенсация больше этого в день толкает в «сорвался — голодаю» (MA-INV-49).
MAX_DAILY_CUT = 500.0
# Пол без замера: ниже этого совет «ешь столько» вреден для взрослого.
FLOOR_NO_SCAN = 1200.0


@dataclass(frozen=True)
class WeekDay:
    day: date
    kcal: float | None      # None — записей не было (MA-INV-48)
    over: bool              # выше дневной цели


@dataclass(frozen=True)
class WeekBalance:
    start: date
    end: date               # воскресенье
    today: date             # «сегодня» человека (с учётом границы суток)
    goal_kcal: float
    week_goal_kcal: float
    consumed_kcal: float
    balance_kcal: float     # минус — запас, плюс — перебор
    logged_days: int         # дни с записями по сегодня включительно
    counted_days: int        # из них вошли в баланс
    past_logged_days: int    # записанные дни ДО сегодня
    days: list[WeekDay]
    remaining_days: int
    per_day_kcal: float | None   # None — не показываем (нет дней или ниже пола)
    floor_hit: bool
    verdict: str            # on_track | over | few_logs | empty

    @property
    def average_kcal(self) -> float:
        """Среднее по дням, вошедшим в баланс: сегодняшний неполный день его не тянет."""
        return self.consumed_kcal / self.counted_days if self.counted_days else 0.0


def balance(
    days: list[tuple[date, float | None, float]],
    *,
    today: date,
    bmr_kcal: float | None = None,
    first_day: date | None = None,
) -> WeekBalance:
    """`days` — семь троек (дата, ккал или None, цель того дня), с понедельника.

    Сегодня входит в баланс, только если цель дня уже превышена; иначе оно —
    оставшийся день. Будущие дни (предзапись) и дни до регистрации не входят.
    """
    assert len(days) == 7, days
    goal_today = next((g for d, _, g in days if d == today), days[-1][2])

    def counts(d: date, k: float | None, g: float) -> bool:
        if k is None or k <= 0 or d > today:
            return False
        if first_day is not None and d < first_day:
            return False
        return d < today or k > g

    counted = [(d, k, g) for d, k, g in days if counts(d, k, g)]
    consumed = sum(k for _, k, _ in counted)  # type: ignore[misc]
    bal = sum(k - g for _, k, g in counted)  # type: ignore[operator]
    in_week = days[0][0] <= today <= days[-1][0]
    today_counted = any(d == today for d, _, _ in counted)
    remaining = 0
    if in_week:
        remaining = sum(1 for d, _, _ in days if d > today) + (0 if today_counted else 1)

    floor = max(goal_today - MAX_DAILY_CUT, bmr_kcal or FLOOR_NO_SCAN)
    per_day: float | None = None
    floor_hit = False
    if remaining and goal_today > 0:
        raw = goal_today - bal / remaining
        if raw < floor:
            floor_hit = True           # MA-INV-49: не советуем есть ниже пола
        else:
            per_day = float(round(raw))

    # Сколько дней уже можно было записать: прошедшие (с регистрации) плюс сегодня.
    start = max(days[0][0], first_day) if first_day else days[0][0]
    elapsed = 0 if not in_week else max(0, (today - start).days)
    if not in_week and today > days[-1][0]:
        elapsed = sum(1 for d, _, _ in days if first_day is None or d >= first_day)
    past_logged = sum(1 for d, _, _ in counted if d < today)
    logged_set = {d for d, k, _ in days if k is not None and k > 0 and d <= today
                  and (first_day is None or d >= first_day)}
    if not logged_set:
        verdict = "empty"
    elif past_logged < elapsed - 1:
        verdict = "few_logs"           # незаписанный срыв не должен красить неделю зелёным
    else:
        verdict = "over" if bal > 0 else "on_track"

    return WeekBalance(
        start=days[0][0],
        end=days[-1][0],
        today=today,
        goal_kcal=goal_today,
        week_goal_kcal=sum(g for _, _, g in counted),
        consumed_kcal=round(consumed, 1),
        balance_kcal=float(round(bal)),
        logged_days=len(logged_set),
        counted_days=len(counted),
        past_logged_days=past_logged,
        days=[WeekDay(d, round(k, 1) if d in logged_set else None,  # type: ignore[arg-type]
                      bool(d in logged_set and k > g))  # type: ignore[operator]
              for d, k, g in days],
        remaining_days=remaining,
        per_day_kcal=per_day,
        floor_hit=floor_hit,
        verdict=verdict,
    )


def demo() -> None:
    from datetime import timedelta

    mon = date(2026, 9, 14)
    week = [mon + timedelta(days=i) for i in range(7)]

    def mk(kcal, goal=2176):
        goals = goal if isinstance(goal, list) else [goal] * 7
        return list(zip(week, kcal, goals, strict=True))

    # AC-MA-49: пн–чт по 1800, пт 2556 (перебор — входит), выходные пусты, сегодня пятница.
    b = balance(mk([1800, 1800, 1800, 1800, 2556, None, None]), today=week[4])
    assert b.balance_kcal == -1124, b.balance_kcal
    assert b.remaining_days == 2 and b.per_day_kcal == 2738, b
    assert b.logged_days == 5 and b.verdict == "on_track"
    assert [d.over for d in b.days] == [False] * 4 + [True, False, False]
    assert round(b.average_kcal) == 1951
    assert b.week_goal_kcal == 2176 * 5

    # AC-MA-60: пятница, съедено 400 — не в балансе, пятница оставшаяся.
    f = balance(mk([1800, 1800, 1800, 1800, 400, None, None]), today=week[4])
    assert f.balance_kcal == -1504 and f.remaining_days == 3
    assert f.per_day_kcal == round(2176 + 1504 / 3)
    assert f.logged_days == 5            # пятница видна в столбиках
    assert f.days[4].kcal == 400
    assert f.counted_days == 4 and f.past_logged_days == 4 and f.average_kcal == 1800

    # AC-MA-50: вторник, понедельник пуст — не в балансе, прочерк; мало записей не считаем
    # (прошёл один день, пропущен один — порог «минус один»).
    t = balance(mk([None, 2000, None, None, None, None, None]), today=week[1])
    assert t.balance_kcal == 0 and t.days[0].kcal is None
    assert t.remaining_days == 6 and t.verdict == "on_track"

    # Четверг, записан только понедельник — «мало записей».
    few = balance(mk([1500, None, None, 900, None, None, None]), today=week[3])
    assert few.verdict == "few_logs"

    # AC-MA-51: перебор такой, что «можно» ниже пола — числа нет.
    o = balance(mk([2176] * 4 + [6000, None, None]), today=week[4], bmr_kcal=1925)
    assert o.floor_hit and o.per_day_kcal is None and o.verdict == "over"
    # Пол без замера — max(цель − 500, 1200): у цели 1345 это 1200, а не 807.
    low = balance(mk([1345, 1345, 1345, 1345, 3000, None, None], goal=1345), today=week[4])
    assert low.floor_hit                  # 1345 − 1655/2 = 517 < 1200

    # AC-MA-59: цель сменили в среду — пн, вт от старой.
    g = balance(mk([2000, 2000, 2000, None, None, None, None],
                   goal=[2500, 2500, 2000, 2000, 2000, 2000, 2000]), today=week[2])
    assert g.balance_kcal == -1000, g.balance_kcal

    # Предзапись на субботу не входит.
    pre = balance(mk([2176, 2176, None, None, None, 900, None]), today=week[1])
    assert pre.balance_kcal == 0 and pre.days[5].kcal is None

    # Воскресенье с перебором — оставшихся дней нет.
    s = balance(mk([2000] * 6 + [2500]), today=week[6])
    assert s.remaining_days == 0 and s.per_day_kcal is None and not s.floor_hit

    # Прошлая неделя — оставшихся дней нет.
    p = balance(mk([2000] * 7), today=week[6] + timedelta(days=3))
    assert p.remaining_days == 0 and p.verdict == "on_track"

    # Регистрация в среду — пн, вт не в счёт.
    r = balance(mk([None, None, 2000, 2000, None, None, None]), today=week[3],
                first_day=week[2])
    assert r.verdict == "on_track" and r.logged_days == 2

    # Пустая неделя.
    z = balance(mk([None] * 7), today=week[0])
    assert z.verdict == "empty" and z.per_day_kcal == 2176
    print("неделя: ок")


if __name__ == "__main__":
    demo()
