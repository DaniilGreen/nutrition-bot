"""Общие фикстуры. Аппка тестируется тем же способом, что и бот: на фейках
в памяти, без БД и без вызовов LLM (§8.9).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest

from app.application.assistant import Assistant
from app.application.dto import Intent, ParsedMessage
from app.application.meal_service import MealService
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.config import Settings
from tests.fakes import (
    InMemoryBugRepo,
    InMemoryMealRepo,
    InMemoryProductRepo,
    InMemoryUserRepo,
    InMemoryWeightRepo,
    StubNlu,
)
from tests.initdata_fixtures import TOKEN

# Ответ по умолчанию: «не понял». Пустой StubNlu падал бы IndexError,
# и тест про неудачный разбор выглядел бы как поломка фикстуры.
_NOTHING = ParsedMessage(intent=Intent.UNKNOWN, confidence=0.0)


class _FakeSession:
    """Считает commit/rollback: без этого «забыли зафиксировать транзакцию»
    проходит мимо любого числа зелёных тестов."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class _FakeDb:
    def __init__(self) -> None:
        self.session = _FakeSession()

    def session_factory(self):
        @asynccontextmanager
        async def _cm():
            yield self.session

        return _cm()

    async def dispose(self) -> None:
        return None


@dataclass
class FakeContainer:
    settings: Settings
    users: InMemoryUserRepo
    db: _FakeDb
    products: InMemoryProductRepo = field(default_factory=InMemoryProductRepo)
    meals: InMemoryMealRepo = field(default_factory=InMemoryMealRepo)
    weights: InMemoryWeightRepo = field(default_factory=InMemoryWeightRepo)
    bugs: InMemoryBugRepo = field(default_factory=InMemoryBugRepo)
    # Подменяется в тестах записи: разные сценарии требуют разных ответов NLU.
    nlu: object = None
    stt: object = None

    def assistant_for(self, session):
        """Сигнатура та же, что у настоящего Container: (Assistant, UserRepo).
        Настоящий ассистент, а не None — иначе первый же эндпоинт, который его
        дёрнет, получит AttributeError вместо понятного отказа."""
        meal = MealService(self.products, self.meals)
        weight = WeightService(self.weights)
        stats = StatsService(
            self.meals, self.users, self.settings.default_goal()
        )
        assistant = Assistant(
            self.nlu or StubNlu(_NOTHING),
            self.products, meal, weight, stats, self.stt, self.bugs, None,
        )
        return assistant, self.users


@pytest.fixture
def fake_container() -> FakeContainer:
    return FakeContainer(
        settings=Settings(bot_token=TOKEN, database_url="sqlite+aiosqlite:///:memory:"),
        users=InMemoryUserRepo(),
        db=_FakeDb(),
    )
