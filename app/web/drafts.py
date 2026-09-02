"""Черновики незавершённых записей: то, что ждёт ответа на уточнение.

Почему на сервере, а не в браузере: черновик содержит плотности КБЖУ. Приняв
его обратно из клиента, мы дали бы любому возможность прислать «шоколад,
0 ккал/100 г» и записать это как факт — прямое нарушение INV-1. Клиент видит
только непрозрачный `draft_id`.

ponytail: словарь в памяти процесса. Потолок — один инстанс api и перезапуск
теряет незавершённые уточнения (их время жизни 15 минут, потеря безболезненна).
Появится второй инстанс → Redis или таблица.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

TTL_SECONDS = 15 * 60
MAX_DRAFTS = 200  # заведомо больше, чем нужно двум людям; страховка от роста памяти


@dataclass
class Draft:
    user_id: int
    payload: Any  # ParsedMessage — web про его устройство не знает и не должен
    item_index: int
    raw_input: str
    created_at: float = field(default_factory=time.monotonic)


class DraftStore:
    def __init__(self) -> None:
        self._items: dict[str, Draft] = {}

    def put(self, user_id: int, payload: Any, item_index: int, raw_input: str) -> str:
        self._sweep()
        if len(self._items) >= MAX_DRAFTS:
            # Выкидываем самый старый, а не отказываем: отказ на ровном месте
            # хуже, чем потеря чужого протухающего черновика.
            oldest = min(self._items, key=lambda k: self._items[k].created_at)
            del self._items[oldest]
        token = secrets.token_urlsafe(16)
        self._items[token] = Draft(user_id, payload, item_index, raw_input)
        return token

    def take(self, token: str, user_id: int) -> Draft | None:
        """Достать и удалить. Одноразовость намеренная: повторный ответ на то же
        уточнение записал бы приём дважды."""
        self._sweep()
        draft = self._items.get(token)
        if draft is None:
            return None
        # Чужой черновик не отдаём даже по верному токену: токен мог утечь.
        if draft.user_id != user_id:
            return None
        del self._items[token]
        return draft

    def _sweep(self) -> None:
        now = time.monotonic()
        dead = [k for k, v in self._items.items() if now - v.created_at > TTL_SECONDS]
        for k in dead:
            del self._items[k]

    def __len__(self) -> int:
        return len(self._items)
