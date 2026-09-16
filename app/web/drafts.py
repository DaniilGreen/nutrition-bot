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
    # "variant" — LLM предложил замены имени (D-19), ответ переименовывает позицию.
    # "portion" — не хватает веса, ответ дописывается к вводу и разбирается заново.
    # Разные пути: перепутать их значит потерять запись (ответ «300 г» стал бы
    # именем продукта).
    kind: str = "variant"
    # День, на который человек смотрел, когда начал запись (0 = сегодня, −1 = вчера).
    # Хранится тут, а не спрашивается у клиента при ответе: пока человек отвечает
    # на уточнение, он мог пролистать на другой день — и запись уехала бы не туда.
    day_offset: int = 0
    created_at: float = field(default_factory=time.monotonic)


class DraftStore:
    def __init__(self) -> None:
        self._items: dict[str, Draft] = {}

    def put(
        self, user_id: int, payload: Any, item_index: int, raw_input: str,
        kind: str = "variant", day_offset: int = 0,
    ) -> str:
        self._sweep()
        if len(self._items) >= MAX_DRAFTS:
            # Выкидываем самый старый, а не отказываем: отказ на ровном месте
            # хуже, чем потеря чужого протухающего черновика.
            oldest = min(self._items, key=lambda k: self._items[k].created_at)
            del self._items[oldest]
        token = secrets.token_urlsafe(16)
        self._items[token] = Draft(user_id, payload, item_index, raw_input, kind, day_offset)
        return token

    def peek(self, token: str, user_id: int) -> Draft | None:
        """Посмотреть, не забирая: проверка ввода до изъятия, чтобы отказ
        валидации не сжигал черновик (MA-INV-44)."""
        self._sweep()
        draft = self._items.get(token)
        return draft if draft is not None and draft.user_id == user_id else None

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

    def restore(self, token: str, draft: Draft) -> None:
        """Вернуть взятый черновик под тем же токеном: запись упала (сеть, 429),
        и повтор с тем же ключом должен найти карточку, а не «устарела»."""
        self._items[token] = draft

    def _sweep(self) -> None:
        now = time.monotonic()
        dead = [k for k, v in self._items.items() if now - v.created_at > TTL_SECONDS]
        for k in dead:
            del self._items[k]

    def __len__(self) -> int:
        return len(self._items)
