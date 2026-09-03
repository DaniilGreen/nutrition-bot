"""Use-case: записать вес тела, дельта к прошлому, история для тренда."""
from __future__ import annotations

from app.application.entities import User, WeightLog
from app.application.ports import WeightRepo
from app.clock import Clock, utcnow


class WeightService:
    def __init__(self, weights: WeightRepo, clock: Clock = utcnow) -> None:
        self._weights = weights
        self._clock = clock

    # Человек весит в этих пределах. Проверка живёт в сервисе, а не в разборе
    # текста: иначе запись весов из аппки шла бы мимо неё.
    MIN_KG, MAX_KG = 20.0, 400.0

    def plausible(self, weight_kg: float) -> bool:
        return self.MIN_KG <= weight_kg <= self.MAX_KG

    async def log(self, user: User, weight_kg: float) -> tuple[WeightLog, float | None]:
        if not self.plausible(weight_kg):
            raise ValueError("вес вне разумных пределов (20–400 кг)")
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

    async def delete(self, user: User, log_id: int) -> WeightLog | None:
        return await self._weights.delete(_require_id(user), log_id)


def _require_id(user: User) -> int:
    assert user.id is not None, "user must be persisted before logging weight"
    return user.id
