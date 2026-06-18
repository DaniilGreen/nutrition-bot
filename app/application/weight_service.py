"""Use-case: записать вес тела, дельта к прошлому, история для тренда."""
from __future__ import annotations

from app.application.entities import User, WeightLog
from app.application.ports import WeightRepo
from app.clock import Clock, utcnow


class WeightService:
    def __init__(self, weights: WeightRepo, clock: Clock = utcnow) -> None:
        self._weights = weights
        self._clock = clock

    async def log(self, user: User, weight_kg: float) -> tuple[WeightLog, float | None]:
        user_id = _require_id(user)
        now = self._clock()
        previous = await self._weights.latest(user_id, before=now)
        delta = round(weight_kg - previous.weight_kg, 1) if previous is not None else None
        saved = await self._weights.add(
            WeightLog(user_id=user_id, measured_at=now, weight_kg=weight_kg)
        )
        return saved, delta

    async def history(self, user: User, limit: int = 14) -> list[WeightLog]:
        return await self._weights.history(_require_id(user), limit)


def _require_id(user: User) -> int:
    assert user.id is not None, "user must be persisted before logging weight"
    return user.id
