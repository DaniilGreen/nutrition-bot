"""Use-case: дневная и недельная статистика, остаток КБЖУ, текущая цель."""
from __future__ import annotations

from zoneinfo import ZoneInfo

from app.application.entities import Meal, User
from app.application.ports import MealRepo, UserRepo
from app.application.results import DailyStats, WeeklyStats
from app.clock import Clock, utcnow
from app.domain.goals import Goal, remaining
from app.domain.timeframe import day_bounds_utc, week_bounds_utc
from app.domain.value_objects import sum_macros


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

    async def daily(self, user: User) -> DailyStats:
        user_id = _require_id(user)
        start, end = day_bounds_utc(self._clock(), user.timezone)
        meals = await self._meals.list_between(user_id, start, end)
        consumed = sum_macros(meal.totals for meal in meals)
        goal = await self._users.get_goal(user_id) or self._default_goal
        return DailyStats(remaining=remaining(consumed, goal), meals_count=len(meals))

    async def day_meals(self, user: User) -> list[Meal]:
        """Все приёмы за СЕГОДНЯ, свежие первыми — для пикера отмены (не последние N, баг #12)."""
        start, end = day_bounds_utc(self._clock(), user.timezone)
        meals = await self._meals.list_between(_require_id(user), start, end)
        meals.sort(key=lambda m: m.eaten_at, reverse=True)
        return meals

    async def weekly(self, user: User) -> WeeklyStats:
        user_id = _require_id(user)
        start, end = week_bounds_utc(self._clock(), user.timezone)
        meals = await self._meals.list_between(user_id, start, end)
        consumed = sum_macros(meal.totals for meal in meals)
        tz = ZoneInfo(user.timezone)
        days_logged = len({meal.eaten_at.astimezone(tz).date() for meal in meals})
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
