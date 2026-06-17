"""Порты (Protocol-интерфейсы) — точка инверсии зависимостей.

Сервисы зависят от этих абстракций, а не от SQLAlchemy/OpenAI. В проде их
реализует infrastructure/, в тестах — in-memory фейки. Так тесты гоняются
без БД и без вызовов LLM.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from app.application.dto import ParsedMessage
from app.application.entities import Meal, Product, User, WeightLog
from app.domain.goals import Goal


class NluPort(Protocol):
    async def parse(self, text: str, known_keys: Sequence[str]) -> ParsedMessage: ...


class UserRepo(Protocol):
    async def get_or_create(
        self, *, telegram_id: int, name: str | None, locale: str, timezone: str
    ) -> User: ...

    async def get_goal(self, user_id: int) -> Goal | None: ...

    async def set_goal(self, user_id: int, goal: Goal) -> None: ...


class ProductRepo(Protocol):
    async def get_by_key(self, canonical_key: str) -> Product | None: ...

    async def create(self, product: Product) -> Product: ...

    async def all_keys(self) -> list[str]: ...


class MealRepo(Protocol):
    async def add(self, meal: Meal) -> Meal: ...

    async def list_for_day(
        self, user_id: int, start: datetime, end: datetime
    ) -> list[Meal]: ...


class WeightRepo(Protocol):
    async def add(self, log: WeightLog) -> WeightLog: ...

    async def latest(self, user_id: int, before: datetime | None = None) -> WeightLog | None: ...
