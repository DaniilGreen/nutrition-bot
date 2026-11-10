"""«🗒 История» виджетом (FR-49.8, D-67, D-68): листание ‹ ›, 🗑 позиции, ⏪ Вернуть,
🔁 повторить с весом.

Те же use-case, что у кнопок rich-экрана дня: `remove_item` / `delete_meal`, `restore_meal` со
сверкой отпечатка (FR-41), `repeat_item` / `repeat_meal` (FR-42). Числа — `day_card` (MA-INV-1).
Граница доверия (W-INV-4): `data` строго по формату, запись — `get_meal` по человеку.
Идемпотентность (W-INV-5): ключ на тап.

ponytail: ключи и «Вернуть» — в памяти процесса на 10 минут (как у карточки приёма).
"""
from __future__ import annotations

import copy
import json
import logging
import re
import time
from typing import Any

from aiogram import Bot

from app.application.assistant import Assistant
from app.application.entities import Meal, User
from app.application.meal_service import SnapshotStale, meal_fingerprint
from app.bot.keyboards import open_in_app
from app.bot.ui.widgets import day_data
from app.bot.widgets import live, template
from app.bot.widgets.transport import EditMessageWidgetData, SendWidget
from app.domain.plausibility import MAX_PORTION_G, MIN_PORTION_G

logger = logging.getLogger(__name__)

TTL_S = 600
MAX_DAYS = 400
_KEY = r"([0-9a-z]{4,10})"
_READ = re.compile(r"h:d:([0-9]{1,3})")
_DEL = re.compile(rf"h:x:([0-9]{{1,3}}):([0-9]{{1,10}}):([0-9]{{1,10}}):{_KEY}")
_BACK = re.compile(rf"h:r:([0-9]{{1,3}}):{_KEY}")
_REP = re.compile(rf"h:p:([0-9]{{1,3}}):([0-9]{{1,10}}):([0-9]{{1,10}}):([0-9]{{1,4}}):{_KEY}")
CAPTION = "🗒 История"


class _Undo:
    def __init__(self, at: float, snapshot: Meal, after: object) -> None:
        self.at, self.snapshot, self.after = at, snapshot, after


_undo: dict[tuple[int, int], _Undo] = {}
_done: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}
_now = time.monotonic


def _sweep(now: float) -> None:
    for k in [k for k, v in _undo.items() if now - v.at > TTL_S]:
        del _undo[k]
    for d in [k for k, (at, _) in _done.items() if now - at > TTL_S]:
        del _done[d]


async def _day(assistant: Assistant, user: User, days_ago: int, undo: bool) -> str:
    rem, dv = await assistant.day_card(user, days_ago)
    return day_data(dv, rem, undo=int(undo))


async def show(bot: Bot, chat_id: int, user: User, assistant: Assistant) -> bool:
    """Отправить сегодняшний день. False — не вышло: вызывающий шлёт rich-экран (W-INV-2)."""
    try:
        await bot(SendWidget(
            chat_id=chat_id, html=template("day"), chat_data=await _day(assistant, user, 0, False),
            caption=CAPTION, allow_queries=True, reply_markup=open_in_app("day"),
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("widget: история не ушла (%s) — фолбэк на rich", exc)
        return False
    return True


def _edit(uid: int, mid: int, data: str):  # noqa: ANN202
    """После ответа — данные самого сообщения: открытый позже виджет покажет правду."""
    async def run(bot: Bot) -> None:
        try:
            await bot(EditMessageWidgetData(chat_id=uid, message_id=mid, chat_data=data))
        except Exception as exc:  # noqa: BLE001
            if "not modified" not in str(exc).lower():
                logger.warning("widget: данные истории не правятся: %s", exc)

    return run


async def on_day(uid: int, mid: int, data: str, user: User, assistant: Assistant) -> live.Outcome:
    """`h:d|x|r|p:…` (AC-298, AC-299)."""
    now = _now()
    _sweep(now)
    slot_key = (uid, mid)
    if (r := _READ.fullmatch(data)) is not None:
        da = int(r.group(1))
        if da > MAX_DAYS:
            return live.Outcome({"err": "bad"})
        return live.Outcome(json.loads(await _day(assistant, user, da, slot_key in _undo)))
    m = _DEL.fullmatch(data) or _BACK.fullmatch(data) or _REP.fullmatch(data)
    if m is None or int(m.group(1)) > MAX_DAYS:
        return live.Outcome({"err": "bad"})
    da, key = int(m.group(1)), (uid, m.groups()[-1])
    if (seen := _done.get(key)) is not None:
        return live.Outcome(seen[1])
    _done[key] = (now, {"err": "busy"})  # занят ДО записи: такой же запрос следом не запишет
    try:
        out = await _write(data[2], m, da, slot_key, now, user, assistant)
    except BaseException:
        _done.pop(key, None)
        raise
    if not out.reply.get("ok"):
        _done.pop(key, None)
        return out
    commit = out.commit

    def remember() -> None:
        _done[key] = (now, out.reply)
        if commit is not None:
            commit()

    return live.Outcome(out.reply, commit=remember, abort=lambda: _done.pop(key, None),
                        after=out.after)


async def _write(
    op: str, m: re.Match[str], da: int, slot_key: tuple[int, int], now: float, user: User,
    assistant: Assistant,
) -> live.Outcome:
    uid, mid = slot_key
    if op == "r":  # «⏪ Вернуть»
        slot = _undo.get(slot_key)
        if slot is None:
            return live.Outcome({"err": "old"})
        try:
            back = await assistant.restore_meal(user, slot.snapshot, expect=slot.after)
        except SnapshotStale:
            return live.Outcome({"err": "edited"})
        if back is None:
            return live.Outcome({"err": "bad"})
        text = await _day(assistant, user, da, False)
        return live.Outcome({**json.loads(text), "ok": 1},
                            commit=lambda: _undo.pop(slot_key, None), after=_edit(uid, mid, text))

    meal_id, item_id = int(m.group(2)), int(m.group(3))
    meal = await assistant.get_meal(user, meal_id)
    if meal is None:
        return live.Outcome({"err": "gone"})  # чужая или уже удалённая

    if op == "p":  # «🔁 Повторить» в сегодня с выбранным весом (FR-42, D-68), с любого дня
        grams = int(m.group(4))
        if item_id and item_id not in {i.id for i in meal.items}:
            return live.Outcome({"err": "bad"})
        if grams and not MIN_PORTION_G <= grams <= MAX_PORTION_G:
            return live.Outcome({"err": "bad"})
        res = (await assistant.repeat_item(user, meal_id, item_id, grams=float(grams) or None)
               if item_id else await assistant.repeat_meal(user, meal_id))
        if res is None:
            return live.Outcome({"err": "gone"})
        what = ", ".join(i.name_snapshot + (f" {round(i.grams)} г" if i.grams else "")
                         for i in res.meal.items)
        what = what if len(what) <= 40 else what[:39] + "…"
        text = await _day(assistant, user, da, slot_key in _undo)
        kcal = round(res.meal.totals.kcal)
        return live.Outcome({**json.loads(text), "ok": 1,
                             "tm": f"🔁 Записал в сегодня: {what} · {kcal} ккал"})

    # op == "x": убрать позицию (последняя — запись целиком); позиция 0 — вся запись
    if item_id and item_id not in {i.id for i in meal.items}:
        return live.Outcome({"err": "bad"})
    snapshot = copy.deepcopy(meal)
    if item_id and len(meal.items) > 1:
        rest = await assistant.remove_item(user, meal_id, item_id)
        after = meal_fingerprint(rest.meal if rest is not None else None)
    else:
        if (await assistant.delete_meal(user, meal_id)).meal is None:
            return live.Outcome({"err": "gone"})
        after = meal_fingerprint(None)
    text = await _day(assistant, user, da, True)
    slot = _Undo(now, snapshot, after)

    def keep() -> None:
        _undo[slot_key] = slot

    return live.Outcome({**json.loads(text), "ok": 1}, commit=keep, after=_edit(uid, mid, text))


live.ROUTES["h"] = on_day
