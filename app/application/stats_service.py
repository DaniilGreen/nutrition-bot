"""Use-case: дневная и недельная статистика, остаток КБЖУ, текущая цель."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.application.entities import Meal, User
from app.application.ports import MealRepo, UserRepo
from app.application.results import DailyStats, DayPace, WeeklyStats
from app.clock import Clock, utcnow
from app.domain.goals import Goal, remaining
from app.domain.pace import (
    behind,
    curve_points,
    norm_curve,
    quiet_enough,
)
from app.domain.prefs import day_start
from app.domain.timeframe import day_bounds_utc, local_date, week_bounds_utc
from app.domain.value_objects import Macros, sum_macros
from app.domain.week import WeekBalance, balance


class StatsService:
    def __init__(
        self,
        meals: MealRepo,
        users: UserRepo,
        default_goal: Goal,
        clock: Clock = utcnow,
    ) -> None:
        self._meals = meals
        self._users = users
        self._default_goal = default_goal
        self._clock = clock

    async def current_goal(self, user: User) -> Goal:
        return await self._users.get_goal(_require_id(user)) or self._default_goal

    def days_ago(self, user: User, moment: datetime) -> int:
        """Сколько дней назад (по дню человека) был момент: карточке прошлого приёма нужен его
        день, а не сегодняшний (FR-44)."""
        start = day_start(user.ui_prefs)
        today = local_date(self._clock(), user.timezone, start)
        return (today - local_date(moment, user.timezone, start)).days

    async def daily(self, user: User, days_ago: int = 0) -> DailyStats:
        """Статистика дня. days_ago=0 — сегодня; >0 — прошлые дни, <0 — будущие (предзапись).

        Параметр нужен карточке приёма при бэкдейте (FR-33): показать остаток ТОГО дня, куда
        записали, а не сегодняшнего.
        """
        user_id = _require_id(user)
        start, end = day_bounds_utc(
            self._clock() - timedelta(days=days_ago), user.timezone, day_start(user.ui_prefs)
        )
        meals = await self._meals.list_between(user_id, start, end)
        meals.sort(key=lambda m: m.eaten_at)  # хронологически — для списка съеденного в «Сегодня»
        consumed = sum_macros(meal.totals for meal in meals)
        goal = await self._users.get_goal(user_id) or self._default_goal
        return DailyStats(
            remaining=remaining(consumed, goal), meals_count=len(meals), meals=meals,
            timezone=user.timezone,
        )

    async def day_pace(self, user: User, *, window_days: int = 60) -> DayPace:
        """§6.20: сегодняшний темп против личной нормы к этому часу.

        Сервис только собирает данные и отдаёт решение домену: вся арифметика
        нормы и порогов — в `app/domain/pace.py`, чтобы проверяться тестом,
        а не глазами на проде.

        Окно 60 дней, а не 30: замерено на живых данных 05.09.2026 — за 30 дней
        у владельца набирается ровно 5 дошедших дней, то есть впритык к минимуму,
        и норма то есть, то нет. За 60 их 16. Опустить планку «дошедшего дня»
        было бы дешевле и неправильно: в норму попали бы половинчатые дни, и она
        сползла бы до уровня той самой недозаписи, против которой всё затевалось.
        """
        user_id = _require_id(user)
        zone = ZoneInfo(user.timezone)
        start_h = day_start(user.ui_prefs)
        now_local = self._clock().astimezone(zone)
        today = local_date(self._clock(), user.timezone, start_h).isoformat()

        # Часы здесь — «часы от начала суток человека» (§6.5): при начале в 4:00
        # перекус в 00:30 стоит в конце кривой, а не в начале. Наружу — настоящий час.
        rows = await self._meals.hourly_kcal(
            user_id, days=window_days, tz=user.timezone, day_start=start_h
        )
        by_day: dict[str, list[tuple[int, float]]] = {}
        for day_str, hour, kcal in rows:
            by_day.setdefault(day_str, []).append((hour, kcal))

        goal = await self._users.get_goal(user_id) or self._default_goal
        # Сегодняшний день из нормы исключаем: он ещё идёт и заведомо неполон,
        # а попав в выборку, тянул бы планку вниз ровно тогда, когда она нужна.
        past = [meals for day_str, meals in by_day.items() if day_str != today]
        pace = norm_curve(past, goal.daily_kcal)

        today_meals = by_day.get(today, [])
        today_kcal = sum(kcal for _, kcal in today_meals)
        hour = (now_local.hour - start_h) % 24
        real_hour = now_local.hour
        norm_kcal = pace.norm[hour] if pace.norm else 0.0

        # Точное время последней записи, а не час из сводки: между 15:50 и 16:10
        # двадцать минут, а по часам получалось бы семьдесят — и карточка
        # выскакивала бы человеку под руку, пока он ещё пишет.
        recent = await self._meals.list_recent(user_id, 1)
        last = recent[0].eaten_at.astimezone(zone) if recent else None
        # Вчерашняя запись про сегодняшний день ничего не говорит: и тишину от неё
        # считать нельзя, и показывать её как «последнюю» было бы враньём.
        if last is not None and local_date(last, user.timezone, start_h) != local_date(
            now_local, user.timezone, start_h
        ):
            last = None
        minutes_since = (now_local - last).total_seconds() / 60 if last else None

        show = behind(pace, hour, today_kcal, goal.daily_kcal) and quiet_enough(minutes_since)
        if not show:
            return DayPace(
                show=False, hour=real_hour, norm_kcal=round(norm_kcal, 1),
                today_kcal=round(today_kcal, 1), days_used=pace.days_used,
            )

        first = min((h for h, _ in today_meals), default=hour)
        # Кривая начинается с часа, в который человек ОБЫЧНО начинает есть, даже
        # если сегодня он ещё не ел: иначе в пустой день график вырождается в точку.
        norm_first = next((h for h, v in enumerate(pace.norm) if v > 0), first)
        start = min(first, norm_first)
        today_cum, run = [], 0.0
        per_hour = dict(today_meals)
        for h in range(start, hour + 1):
            run += per_hour.get(h, 0.0)
            today_cum.append((h, round(run, 1)))
        return DayPace(
            show=True, hour=real_hour, norm_kcal=round(norm_kcal, 1),
            today_kcal=round(today_kcal, 1), days_used=pace.days_used,
            last_at=last.strftime("%H:%M") if last is not None else None,
            norm_points=curve_points(pace.norm, start, hour),
            today_points=today_cum,
        )

    async def day_meals(self, user: User, days_ago: int = 0) -> list[Meal]:
        """Приёмы за день (0=сегодня), свежие первыми — пикер #12 и просмотр дня (вар. A)."""
        ref = self._clock() - timedelta(days=days_ago)
        start, end = day_bounds_utc(ref, user.timezone, day_start(user.ui_prefs))
        meals = await self._meals.list_between(_require_id(user), start, end)
        meals.sort(key=lambda m: m.eaten_at, reverse=True)
        return meals

    async def last_days(self, user: User, n: int) -> list[tuple[date, float]]:
        """(дата, ккал) за n последних дней, старые→новые — для графика недели и хитмапа."""
        out: list[tuple[date, float]] = []
        for days_ago in range(n - 1, -1, -1):
            meals = await self.day_meals(user, days_ago)
            day = local_date(
                self._clock() - timedelta(days=days_ago), user.timezone, day_start(user.ui_prefs)
            )
            out.append((day, sum(m.totals.kcal for m in meals)))
        return out

    async def last_days_macros(
        self, user: User, n: int
    ) -> list[tuple[date, Macros, int]]:
        """(дата, КБЖУ, число приёмов) за n дней, старые→новые — для графиков.

        Одним запросом на всё окно, в отличие от `last_days`, который ходит в БД
        по разу на день: на месяце это тридцать round-trip'ов ради одного графика.
        Группировка по ЛОКАЛЬНОЙ дате — иначе ужин в 23:30 уехал бы во вчера.
        """
        user_id = _require_id(user)
        start_h = day_start(user.ui_prefs)
        now = self._clock()
        start, _ = day_bounds_utc(now - timedelta(days=n - 1), user.timezone, start_h)
        _, end = day_bounds_utc(now, user.timezone, start_h)
        meals = await self._meals.list_between(user_id, start, end)

        by_day: dict[date, list[Meal]] = {}
        for meal in meals:
            by_day.setdefault(local_date(meal.eaten_at, user.timezone, start_h), []).append(meal)

        out: list[tuple[date, Macros, int]] = []
        for days_ago in range(n - 1, -1, -1):
            day = local_date(now - timedelta(days=days_ago), user.timezone, start_h)
            todays = by_day.get(day, [])
            out.append((day, sum_macros(m.totals for m in todays), len(todays)))
        return out

    async def day_view(self, user: User, days_ago: int = 0) -> tuple[list[Meal], date]:
        """Приёмы за день ХРОНОЛОГИЧЕСКИ + дата дня (в TZ юзера) — для day-view с навигацией."""
        meals = list(reversed(await self.day_meals(user, days_ago)))
        day = local_date(
            self._clock() - timedelta(days=days_ago), user.timezone, day_start(user.ui_prefs)
        )
        return meals, day

    async def period(self, user: User, days: int) -> Macros:
        """Суммарные КБЖУ за последние `days` дней — для отчёта /month."""
        user_id = _require_id(user)
        now = self._clock()
        start_h = day_start(user.ui_prefs)
        start, _ = day_bounds_utc(now - timedelta(days=days - 1), user.timezone, start_h)
        _, end = day_bounds_utc(now, user.timezone, start_h)
        meals = await self._meals.list_between(user_id, start, end)
        return sum_macros(m.totals for m in meals)

    async def kcal_by_day(
        self, user: User, first: date, last: date
    ) -> dict[date, float]:
        """Съедено по местным дням [first, last] одним запросом. Дни без записей
        в словарь не попадают — пустой день не ноль (MA-INV-48)."""
        start_h = day_start(user.ui_prefs)
        zone = ZoneInfo(user.timezone)
        lo = datetime(first.year, first.month, first.day, start_h, tzinfo=zone)
        hi = datetime(last.year, last.month, last.day, start_h, tzinfo=zone) + timedelta(days=1)
        meals = await self._meals.list_between(_require_id(user), lo, hi)
        out: dict[date, float] = {}
        for m in meals:
            d = local_date(m.eaten_at, user.timezone, start_h)
            out[d] = out.get(d, 0.0) + m.totals.kcal
        return out

    async def goals_by_day(self, user: User, days: list[date]) -> dict[date, float]:
        """Цель, действовавшая в каждый из дней (§6.23): последняя смена до конца дня.
        До первой записи истории — текущая цель."""
        current = (await self.current_goal(user)).daily_kcal
        history = await self._users.goal_history(_require_id(user))
        start_h = day_start(user.ui_prefs)
        zone = ZoneInfo(user.timezone)
        out: dict[date, float] = {}
        for d in days:
            end = datetime(d.year, d.month, d.day, start_h, tzinfo=zone) + timedelta(days=1)
            known = [k for t, k in history if t < end]
            out[d] = known[-1] if known else current
        return out

    async def week_balance(
        self, user: User, weeks_ago: int = 0, *, bmr_kcal: float | None = None
    ) -> WeekBalance:
        """Недельный баланс (§6.23)."""
        start_h = day_start(user.ui_prefs)
        today = local_date(self._clock(), user.timezone, start_h)
        monday = today - timedelta(days=today.weekday() + 7 * weeks_ago)
        days = [monday + timedelta(days=i) for i in range(7)]
        eaten = await self.kcal_by_day(user, days[0], days[-1])
        goals = await self.goals_by_day(user, days)
        first_day = (
            local_date(user.created_at, user.timezone, start_h) if user.created_at else None
        )
        return balance(
            [(d, eaten.get(d), goals[d]) for d in days],
            today=today, bmr_kcal=bmr_kcal, first_day=first_day,
        )

    async def weekly(self, user: User) -> WeeklyStats:
        user_id = _require_id(user)
        start, end = week_bounds_utc(self._clock(), user.timezone, day_start(user.ui_prefs))
        meals = await self._meals.list_between(user_id, start, end)
        consumed = sum_macros(meal.totals for meal in meals)
        start_h = day_start(user.ui_prefs)
        days_logged = len({local_date(m.eaten_at, user.timezone, start_h) for m in meals})
        goal = await self._users.get_goal(user_id) or self._default_goal
        return WeeklyStats(
            consumed=consumed,
            days_logged=days_logged,
            meals_count=len(meals),
            goal=goal,
        )


def _require_id(user: User) -> int:
    assert user.id is not None, "user must be persisted before reading stats"
    return user.id
