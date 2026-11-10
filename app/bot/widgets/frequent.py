"""«⚡ Частое» виджетом (FR-49.2, D-54): тап по плитке — запись, «⏪ Вернуть» — там же.

Запись — тем же путём, что кнопка FR-40 (`quick.log_frequent` → `log_quick` / `repeat_meal`),
без модели. Граница доверия (W-INV-4): `data` — строго по формату, набор ищется среди своих,
продукт — в общем каталоге (как у кнопки FR-40), граммы в (0; 5000]. Идемпотентность (W-INV-5):
у каждого тапа свой случайный ключ, повтор с тем же ключом получает прежний ответ и не пишет.

ponytail: ключи и «что записал этот виджет» — в памяти процесса, окно 10 минут (как у «Вернуть»
FR-41). Рестарт бота стирает окно: повтор после рестарта — новая запись, «Вернуть» — отказ.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from aiogram import Bot

from app.application.assistant import Assistant
from app.application.entities import User
from app.application.meal_service import meal_fingerprint
from app.bot import quick
from app.bot.keyboards import open_in_app
from app.bot.ui.widgets import frequent_data
from app.bot.widgets import live, template
from app.bot.widgets.transport import SendWidget

logger = logging.getLogger(__name__)

TTL_S = 600
_WRITE = re.compile(r"w:(p:[0-9]{1,10}:[0-9]{1,4}(?:\.[0-9])?|s:[0-9a-f]{10}):([0-9a-z]{4,10})")
_UNDO = re.compile(r"u:([0-9]{1,10}):([0-9a-z]{4,10})")

_done: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}  # (кто, ключ) → (когда, ответ)
# (кто, id записи) → (когда, id сообщения виджета, отпечаток записи): «Вернуть» снимает только
# запись этого виджета и не сносит правку, сделанную после.
_wrote: dict[tuple[int, int], tuple[float, int, object]] = {}
_now = time.monotonic  # отдельное имя — тест двигает часы виджета, не цикла asyncio


def _sweep(now: float) -> None:
    for key in [k for k, (at, _) in _done.items() if now - at > TTL_S]:
        del _done[key]
    for wkey in [k for k, v in _wrote.items() if now - v[0] > TTL_S]:
        del _wrote[wkey]


async def on_write(
    uid: int, mid: int, data: str, user: User, assistant: Assistant
) -> live.Outcome:
    """`w:<операция>:<ключ>` — записать плитку (AC-276, AC-277)."""
    m = _WRITE.fullmatch(data)
    op = m.group(1) if m else ""
    if m is None or quick.parse_op(op) is None:
        return live.Outcome({"err": "bad"})
    now = _now()
    _sweep(now)
    key = (uid, m.group(2))
    seen = _done.get(key)
    if seen is not None:
        return live.Outcome(seen[1])  # повтор того же тапа — прежний ответ, второй записи нет
    # Ключ занят ДО записи: второй такой же запрос, пришедший, пока первый пишет, не запишет.
    _done[key] = (now, {"err": "busy"})
    logged = None
    try:
        logged = await quick.log_frequent(user, assistant, op)
    finally:
        if logged is None or logged.meal.id is None:
            _done.pop(key, None)
    if logged is None or logged.meal.id is None:
        return live.Outcome({"err": "gone"})  # продукта/набора больше нет — плитка устарела
    meal, meal_id = logged.meal, logged.meal.id
    names = ", ".join(i.name_snapshot for i in meal.items[:2])
    reply: dict[str, Any] = {
        "ok": 1, "m": meal_id, "n": names[:40], "k": round(meal.totals.kcal),
        "l": round(logged.remaining.kcal_left),
    }

    def commit() -> None:
        _done[key] = (now, reply)
        _wrote[(uid, meal_id)] = (now, mid, meal_fingerprint(meal))

    def abort() -> None:
        _done.pop(key, None)

    return live.Outcome(reply, commit=commit, abort=abort)


async def on_undo(
    uid: int, mid: int, data: str, user: User, assistant: Assistant
) -> live.Outcome:
    """`u:<id записи>:<ключ>` — «⏪ Вернуть» записи этого виджета за 10 минут (AC-278).

    Запись успели поправить («✏️ Вес», дописали позицию) — не удаляем: «Вернуть» снёс бы правку
    (тот же отпечаток, что у «⏪ Вернуть» FR-41). Записи уже нет (удалили в другом месте) —
    `gone`: вернуть нечего, и «поправили» было бы неправдой."""
    m = _UNDO.fullmatch(data)
    if m is None:
        return live.Outcome({"err": "bad"})
    now = _now()
    _sweep(now)
    key = (uid, m.group(2))
    seen = _done.get(key)
    if seen is not None:
        return live.Outcome(seen[1])
    meal_id = int(m.group(1))
    wrote = _wrote.get((uid, meal_id))
    if wrote is None or wrote[1] != mid:
        return live.Outcome({"err": "bad"})  # чужая, давняя или записанная не этим виджетом
    meal = await assistant.get_meal(user, meal_id)
    if meal is None:
        def forget() -> None:
            _wrote.pop((uid, meal_id), None)

        return live.Outcome({"err": "gone"}, commit=forget)
    if meal_fingerprint(meal) != wrote[2]:
        return live.Outcome({"err": "edited"})
    deleted = await assistant.delete_meal(user, meal_id)
    if deleted.meal is None:
        return live.Outcome({"err": "gone"})
    today = await assistant.today(user)
    reply: dict[str, Any] = {"ok": 1, "u": meal_id, "l": round(today.remaining.kcal_left)}

    def commit() -> None:
        _done[key] = (now, reply)
        _wrote.pop((uid, meal_id), None)

    return live.Outcome(reply, commit=commit)


live.ROUTES["w"] = on_write
live.ROUTES["u"] = on_undo


async def show(bot: Bot, chat_id: int, user: User, assistant: Assistant) -> bool:
    """Отправить виджет «⚡ Частое». False — показывать нечего или не вышло: вызывающий шлёт
    прежнюю клавиатуру FR-40 (W-INV-2)."""
    try:
        freq = await quick.frequent(user, assistant)
        if not freq.tiles:
            return False
        await bot(SendWidget(
            chat_id=chat_id, html=template("frequent"), chat_data=frequent_data(freq),
            caption="⚡ Частое — тап по плитке запишет сразу", allow_queries=True,
            reply_markup=open_in_app("cat"),
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("widget: «Частое» не ушло (%s) — фолбэк на клавиатуру", exc)
        return False
    return True
