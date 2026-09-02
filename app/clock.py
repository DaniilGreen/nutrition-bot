"""Единая точка получения времени — инъектируется в сервисы для детерминируемых тестов."""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC)
