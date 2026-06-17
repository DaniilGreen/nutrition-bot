"""Use-case: дневная статистика и остаток КБЖУ."""
from __future__ import annotations

from app.application.entities import User
from app.application.ports import MealRepo, UserRepo
from app.application.results import DailyStats
from app.clock import Clock, utcnow
from app.domain.goals import Goal, remaining
from app.domain.timeframe import day_bounds_utc
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

    async def daily(self, user: User) -> DailyStats:
        user_id = user.id
        assert user_id is not None, "user must be persisted before reading stats"

        start, end = day_bounds_utc(self._clock(), user.timezone)
        meals = await self._meals.list_for_day(user_id, start, end)
        consumed = sum_macros(meal.totals for meal in meals)
        goal = await self._users.get_goal(user_id) or self._default_goal
        return DailyStats(remaining=remaining(consumed, goal), meals_count=len(meals))
