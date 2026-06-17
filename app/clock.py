"""Единая точка получения времени — инъектируется в сервисы для детерминируемых тестов."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
