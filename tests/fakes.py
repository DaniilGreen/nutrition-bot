"""In-memory реализации портов + StubNlu/StubStt для тестов без БД и без LLM."""
from __future__ import annotations

from datetime import datetime

from app.application.dto import ParsedMessage
from app.application.entities import Meal, Product, User, WeightLog
from app.domain.goals import Goal


class InMemoryUserRepo:
    def __init__(self) -> None:
        self._users: dict[int, User] = {}
        self._goals: dict[int, Goal] = {}
        self._seq = 0

    async def get_or_create(
        self, *, telegram_id: int, name: str | None, locale: str, timezone: str
    ) -> User:
        for u in self._users.values():
            if u.telegram_id == telegram_id:
                return u
        self._seq += 1
        user = User(
            id=self._seq, telegram_id=telegram_id, name=name, locale=locale, timezone=timezone
        )
        self._users[self._seq] = user
        return user

    async def get_goal(self, user_id: int) -> Goal | None:
        return self._goals.get(user_id)

    async def set_goal(self, user_id: int, goal: Goal) -> None:
        self._goals[user_id] = goal


class InMemoryProductRepo:
    def __init__(self) -> None:
        self._by_key: dict[str, Product] = {}
        self._seq = 0

    async def get_by_key(self, canonical_key: str) -> Product | None:
        return self._by_key.get(canonical_key)

    async def create(self, product: Product) -> Product:
        self._seq += 1
        product.id = self._seq
        self._by_key[product.canonical_key] = product
        return product

    async def all_keys(self) -> list[str]:
        return list(self._by_key.keys())


class InMemoryMealRepo:
    def __init__(self) -> None:
        self._meals: list[Meal] = []
        self._seq = 0

    async def add(self, meal: Meal) -> Meal:
        self._seq += 1
        meal.id = self._seq
        self._meals.append(meal)
        return meal

    async def list_between(self, user_id: int, start: datetime, end: datetime) -> list[Meal]:
        return [m for m in self._meals if m.user_id == user_id and start <= m.eaten_at < end]

    async def list_recent(self, user_id: int, limit: int) -> list[Meal]:
        xs = [m for m in self._meals if m.user_id == user_id]
        xs.sort(key=lambda m: m.eaten_at, reverse=True)
        return xs[:limit]

    async def delete_last(self, user_id: int) -> Meal | None:
        xs = [m for m in self._meals if m.user_id == user_id]
        if not xs:
            return None
        last = max(xs, key=lambda m: m.eaten_at)
        self._meals.remove(last)
        return last


class InMemoryWeightRepo:
    def __init__(self) -> None:
        self._logs: list[WeightLog] = []
        self._seq = 0

    async def add(self, log: WeightLog) -> WeightLog:
        self._seq += 1
        log.id = self._seq
        self._logs.append(log)
        return log

    async def latest(self, user_id: int, before: datetime | None = None) -> WeightLog | None:
        xs = [
            log
            for log in self._logs
            if log.user_id == user_id and (before is None or log.measured_at < before)
        ]
        return max(xs, key=lambda log: log.measured_at) if xs else None


class StubNlu:
    """Возвращает заранее заданные ответы по порядку (последний повторяется)."""

    def __init__(self, *responses: ParsedMessage) -> None:
        self._responses = list(responses)
        self._i = 0

    async def parse(self, text: str, known_keys: object) -> ParsedMessage:
        idx = min(self._i, len(self._responses) - 1)
        self._i += 1
        return self._responses[idx]


class StubStt:
    """Возвращает фиксированный транскрипт для голоса."""

    def __init__(self, transcript: str) -> None:
        self._transcript = transcript

    async def transcribe(self, audio: bytes, *, filename: str = "voice.ogg") -> str:
        return self._transcript
