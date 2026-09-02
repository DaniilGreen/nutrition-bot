"""Идемпотентность записи и ограничение частоты (§7.5, §7.6).

Зачем идемпотентность: двойной тап, ретрай браузера на flaky-сети или повтор
после таймаута дают ДВА приёма вместо одного. Для проекта, где приоритет —
точность учёта, это худший класс бага: цифра выглядит правдоподобно и молча
врёт вдвое.

Зачем лимит частоты: `POST /api/meals` и `/api/meals/voice` идут в платную
модель. Залипшая кнопка или цикл в клиенте выжигают бюджет за минуты.

ponytail: обе структуры — словари в памяти процесса. Потолок тот же, что у
черновиков: один инстанс api, перезапуск сбрасывает окно. На двух пользователях
этого достаточно; появится второй инстанс — Redis.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

IDEMPOTENCY_TTL = 10 * 60  # столько живёт запомненный ответ
MAX_KEYS = 500


@dataclass
class _Remembered:
    response: Any
    at: float = field(default_factory=time.monotonic)


class IdempotencyStore:
    """Ключ → ответ. Повтор с тем же ключом возвращает первый ответ, а не пишет второй раз."""

    def __init__(self) -> None:
        self._seen: dict[tuple[int, str], _Remembered] = {}

    def get(self, user_id: int, key: str) -> Any | None:
        self._sweep()
        found = self._seen.get((user_id, key))
        return found.response if found else None

    def put(self, user_id: int, key: str, response: Any) -> None:
        self._sweep()
        if len(self._seen) >= MAX_KEYS:
            oldest = min(self._seen, key=lambda k: self._seen[k].at)
            del self._seen[oldest]
        self._seen[(user_id, key)] = _Remembered(response)

    def _sweep(self) -> None:
        now = time.monotonic()
        for k in [k for k, v in self._seen.items() if now - v.at > IDEMPOTENCY_TTL]:
            del self._seen[k]


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


def demo() -> None:
    """Runnable-чек: повтор не пишет дважды, окно закрывается и открывается."""
    store = IdempotencyStore()
    assert store.get(1, "k") is None
    store.put(1, "k", {"ok": True})
    assert store.get(1, "k") == {"ok": True}
    # Ключ чужого пользователя не подходит: иначе один мог бы прочитать ответ другого.
    assert store.get(2, "k") is None

    rl = RateLimiter()
    assert all(rl.allow(1, "meals", limit=3, window=60) for _ in range(3))
    assert not rl.allow(1, "meals", limit=3, window=60)
    # Лимит на пользователя, а не глобальный.
    assert rl.allow(2, "meals", limit=3, window=60)
    # Окно нулевой ширины сразу освобождает.
    assert rl.allow(1, "meals", limit=3, window=0)
    print("идемпотентность и лимиты: ок")


if __name__ == "__main__":
    demo()
