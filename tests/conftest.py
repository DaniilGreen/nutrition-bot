"""Общие фикстуры. Аппка тестируется тем же способом, что и бот: на фейках
в памяти, без БД и без вызовов LLM (§8.9).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest

from app.config import Settings
from tests.fakes import InMemoryUserRepo

from tests.unit.test_initdata import TOKEN


class _FakeDb:
    """Заменяет Database: сессия никому в этих тестах не нужна, но зависимость
    FastAPI её запрашивает — отдаём заглушку, чтобы граф собрался."""

    @staticmethod
    def session_factory():
        @asynccontextmanager
        async def _cm():
            yield None

        return _cm()

    async def dispose(self) -> None:
        return None


@dataclass
class FakeContainer:
    settings: Settings
    users: InMemoryUserRepo
    db: _FakeDb

    def assistant_for(self, session):
        # Сигнатура как у настоящего Container: (assistant, users). Ассистент в
        # тестах входа не участвует — там, где он понадобится, фикстура
        # расширится, а не подменится.
        return None, self.users


@pytest.fixture
def fake_container() -> FakeContainer:
    return FakeContainer(
        settings=Settings(bot_token=TOKEN, database_url="sqlite+aiosqlite:///:memory:"),
        users=InMemoryUserRepo(),
        db=_FakeDb(),
    )
