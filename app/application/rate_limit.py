"""Ограничение частоты платных вызовов на человека (§7.6, FR-45).

Жил в `app/web/limits.py`; опущен сюда, когда ассистент появился и в боте (D-MA-27): бот не
импортирует веб-слой, а правило «не чаще N за окно» у обоих каналов одно.

ponytail: словарь в памяти процесса. У бота и api процессы разные — окна считаются раздельно;
потолок трат месяца при этом общий, из базы. Понадобится общий счёт — окно в БД или Redis.
"""
from __future__ import annotations

import time
from collections import deque


class RateLimiter:
    """Скользящее окно на пользователя. Не токен-бакет: окно понятнее объясняется
    человеку («не больше N за минуту») и не копит кредит на всплеск."""

    def __init__(self) -> None:
        self._hits: dict[tuple[int, str], deque[float]] = {}

    def allow(self, user_id: int, bucket: str, *, limit: int, window: float) -> bool:
        now = time.monotonic()
        key = (user_id, bucket)
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True
