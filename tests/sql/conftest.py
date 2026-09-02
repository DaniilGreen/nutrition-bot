"""Тесты на НАСТОЯЩЕМ Postgres.

Фейки в `tests/fakes.py` реализуют другую семантику, чем SQL: экранирование `%`
в поиске, `percentile_cont` для медианы и перепривязка истории при слиянии
проверялись фейком, а не базой — то есть не проверялись вовсе.

sqlite тут не подошёл бы: половина запросов постгресовая по определению.
Без доступной БД набор пропускается, чтобы `make test` не требовал её локально.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.infrastructure.db.base import Base

URL = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL", "")


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    if not URL:
        pytest.skip("нет DATABASE_URL — набор на живом SQL пропущен")
    engine = create_async_engine(URL)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # noqa: BLE001 — причина не важна, важно не свалить прогон
        await engine.dispose()
        pytest.skip(f"БД недоступна: {type(exc).__name__}")

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        yield s
        # Откатываем всё, что тест наделал: база общая с разработкой.
        await s.rollback()
    await engine.dispose()
