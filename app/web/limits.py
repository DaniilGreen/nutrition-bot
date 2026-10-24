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
from dataclasses import dataclass, field
from typing import Any

# Окно частоты опущено в application: им пользуется и бот (FR-45, D-MA-27). Имя здесь —
# чтобы `from app.web.limits import RateLimiter` и веб-поведение остались прежними.
from app.application.rate_limit import RateLimiter

__all__ = ["IdempotencyStore", "RateLimiter"]

IDEMPOTENCY_TTL = 5 * 60  # столько живёт запомненный ответ (§7.5)
# Сколько ключ считается «в работе». Больше самого долгого платного вызова
# (голос: Whisper плюс разбор), но конечно: если обработчик упал, не дописав
# ответ, ключ обязан отпуститься сам — иначе человек не сможет повторить
# запись до конца TTL. ponytail: срок вместо try/finally в каждом обработчике;
# понадобится точнее — снимать блокировку в зависимости FastAPI.
IN_FLIGHT_TTL = 90
MAX_KEYS = 500


@dataclass
class _Remembered:
    response: Any
    at: float = field(default_factory=time.monotonic)


class IdempotencyStore:
    """Ключ → ответ. Повтор с тем же ключом возвращает первый ответ, а не пишет второй раз.

    Хранить готовые ответы мало. Тап дублируется ИМЕННО пока первый запрос ещё
    идёт: он ушёл в модель на несколько секунд, кнопка молчит, палец жмёт ещё
    раз — а запоминать пока нечего. Поэтому ключ занимается на входе, и второй
    запрос с ним получает «уже работаю» (§7.5, 409 `in_flight`).
    """

    def __init__(self) -> None:
        self._seen: dict[tuple[int, str], _Remembered] = {}
        self._busy: dict[tuple[int, str], float] = {}

    def get(self, user_id: int, key: str) -> Any | None:
        self._sweep()
        found = self._seen.get((user_id, key))
        return found.response if found else None

    def claim(self, user_id: int, key: str) -> str:
        """Занять ключ под работу. `replay` — ответ уже готов, `in_flight` —
        такой же запрос ещё выполняется, `fresh` — можно работать."""
        self._sweep()
        if self.get(user_id, key) is not None:
            return "replay"
        if (user_id, key) in self._busy:
            return "in_flight"
        self._busy[(user_id, key)] = time.monotonic()
        return "fresh"

    def release(self, user_id: int, key: str) -> None:
        """Отпустить ключ, не запоминая ответа: обработчик не довёл дело."""
        self._busy.pop((user_id, key), None)

    def put(self, user_id: int, key: str, response: Any) -> None:
        self._sweep()
        if len(self._seen) >= MAX_KEYS:
            oldest = min(self._seen, key=lambda k: self._seen[k].at)
            del self._seen[oldest]
        self._seen[(user_id, key)] = _Remembered(response)
        self._busy.pop((user_id, key), None)

    def _sweep(self) -> None:
        now = time.monotonic()
        for k in [k for k, v in self._seen.items() if now - v.at > IDEMPOTENCY_TTL]:
            del self._seen[k]
        for k in [k for k, at in self._busy.items() if now - at > IN_FLIGHT_TTL]:
            del self._busy[k]


def demo() -> None:
    """Runnable-чек: повтор не пишет дважды, окно закрывается и открывается."""
    store = IdempotencyStore()
    assert store.get(1, "k") is None
    store.put(1, "k", {"ok": True})
    assert store.get(1, "k") == {"ok": True}
    # Ключ чужого пользователя не подходит: иначе один мог бы прочитать ответ другого.
    assert store.get(2, "k") is None

    # Главный сценарий §7.5: второй тап приходит, ПОКА первый ещё в работе.
    st = IdempotencyStore()
    assert st.claim(1, "x") == "fresh"
    assert st.claim(1, "x") == "in_flight"      # палец нажал второй раз
    assert st.claim(2, "x") == "fresh"          # чужой ключ не занят
    st.put(1, "x", {"kind": "logged"})
    assert st.claim(1, "x") == "replay"         # ответ готов — отдаём первый
    # Обработчик упал, не дописав ответ: ключ отпускается, повтор возможен.
    assert st.claim(1, "y") == "fresh"
    st.release(1, "y")
    assert st.claim(1, "y") == "fresh"

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
