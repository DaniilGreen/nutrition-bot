"""Карточка приёма виджетом (FR-49.4, каталог #12): ±10 г, убрать позицию, отменить, «Вернуть».

Все правки — теми же use-case, что кнопки rich-карточки и шторка аппки: `adjust_meal_item`
(«✏️ Вес»), `remove_item` («🗑»), `delete_meal` («↩️ Отменить всё»), `restore_meal` со сверкой
отпечатка («⏪ Вернуть», FR-41). Числа считает приложение (INV-1), снапшоты — INV-3.

Граница доверия (W-INV-4): `data` строго по формату, запись — `get_meal` по человеку (чужая —
`gone`), граммы 1…2000 как у «✏️ Вес». Идемпотентность (W-INV-5): случайный ключ на тап.

ponytail: ключи и снимок «Вернуть» — в памяти процесса на 10 минут (как FR-41 у бота). Рестарт
стирает окно: «Вернуть» после рестарта — отказ `old`; правка граммов абсолютная — её повтор
безвреден.
"""
from __future__ import annotations

import copy
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot

from app.application.assistant import Assistant
from app.application.entities import Meal, User
from app.application.meal_service import SnapshotStale, meal_fingerprint
from app.application.results import MealLogged, MealSession
from app.bot.keyboards import meal_widget_keyboard, weighable
from app.bot.ui.widgets import meal_caption, meal_data
from app.bot.widgets import live, template
from app.bot.widgets.transport import EditMessageWidgetData, SendWidget
from app.domain.goals import Remaining
from app.domain.plausibility import MAX_PORTION_G, MIN_PORTION_G

logger = logging.getLogger(__name__)

TTL_S = 600
_ID = r"([0-9]{1,10})"
_KEY = r"([0-9a-z]{4,10})"
_ADJ = re.compile(rf"m:a:{_ID}:{_ID}:([0-9]{{1,4}}):{_KEY}")
_DEL = re.compile(rf"m:d:{_ID}:{_ID}:{_KEY}")
_UNDO = re.compile(rf"m:u:{_ID}:{_KEY}")
_BACK = re.compile(rf"m:r:{_ID}:{_KEY}")

_done: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}  # (кто, ключ) → (когда, ответ)


class _Undo:
    """Что вернуть виджету `(кто, сообщение)`: запись до удаления и отпечаток после (FR-41)."""

    def __init__(self, at: float, snapshot: Meal, after: object, kind: int) -> None:
        self.at, self.snapshot, self.after, self.kind = at, snapshot, after, kind


_undo: dict[tuple[int, int], _Undo] = {}
_now = time.monotonic


def _sweep(now: float) -> None:
    for k in [k for k, (at, _) in _done.items() if now - at > TTL_S]:
        del _done[k]
    for u in [k for k, v in _undo.items() if now - v.at > TTL_S]:
        del _undo[u]


async def _card(
    assistant: Assistant, user: User, meal: Meal, *, undo: int = 0, deleted: bool = False,
    left: Remaining | None = None,
) -> tuple[str | None, str, MealSession | None]:
    """Данные, подпись и приём пищи (FR-47, только из нескольких записей) карточки из базы;
    остаток — дня самой записи, а не сегодняшнего."""
    left = left or await assistant.day_left(user, meal.eaten_at)
    goal = await assistant.current_goal(user)
    sess = None if deleted or meal.id is None else await assistant.session_of(user, meal.id)
    if sess is not None and len(sess.meals) < 2:
        sess = None
    ss = sum(m.totals.kcal for m in sess.meals) if sess is not None else None
    data = meal_data(meal, left, goal, user.timezone, session_kcal=ss, undo=undo, deleted=deleted)
    return data, meal_caption(meal, left, deleted=deleted), sess


async def show(
    bot: Bot, chat_id: int, user: User, assistant: Assistant, result: MealLogged
) -> bool:
    """Отправить карточку записи виджетом. False — не вышло или не влезло: вызывающий шлёт
    rich-карточку с прежними кнопками (W-INV-2)."""
    try:
        data, caption, sess = await _card(assistant, user, result.meal, left=result.remaining)
        if data is None:
            return False
        await bot(SendWidget(
            chat_id=chat_id, html=template("meal"), chat_data=data, caption=caption,
            allow_queries=True, reply_markup=meal_widget_keyboard(result.meal, sess),
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("widget: карточка приёма не ушла (%s) — фолбэк на rich", exc)
        return False
    return True


def _refresh(
    uid: int, mid: int, data: str, caption: str, meal: Meal | None, sess: MealSession | None
) -> Callable[[Bot], Awaitable[None]]:
    """Правка самого сообщения после ответа: данные, подпись, кнопки под ним (D-50)."""

    async def run(bot: Bot) -> None:
        try:
            await bot(EditMessageWidgetData(chat_id=uid, message_id=mid, chat_data=data))
        except Exception as exc:  # noqa: BLE001 — подпись и кнопки правим всё равно
            if "not modified" not in str(exc).lower():
                logger.warning("widget: данные карточки приёма не правятся: %s", exc)
        try:
            await bot.edit_message_caption(
                chat_id=uid, message_id=mid, caption=caption, parse_mode=None,
                reply_markup=meal_widget_keyboard(meal, sess),
            )
        except Exception as exc:  # noqa: BLE001
            if "not modified" not in str(exc).lower():
                logger.warning("widget: подпись карточки приёма не правится: %s", exc)

    return run


async def _answer(
    uid: int, mid: int, key: tuple[int, str], now: float, user: User, assistant: Assistant,
    meal: Meal, *, undo: int, deleted: bool = False, slot: _Undo | None = None,
    drop_slot: bool = False,
) -> live.Outcome:
    data, caption, sess = await _card(assistant, user, meal, undo=undo, deleted=deleted)
    if data is None:
        # Карточка после правки не влезла в 1024 байта: исключение откатит сессию — правка не
        # сохранится молча за ответом «не вышло» (и «Вернуть» не потеряется).
        raise RuntimeError("карточка приёма после правки не влезла в бюджет")
    reply = {**json.loads(data), "ok": 1}

    def commit() -> None:
        _done[key] = (now, reply)
        if slot is not None:
            _undo[(uid, mid)] = slot
        elif drop_slot:
            _undo.pop((uid, mid), None)

    def abort() -> None:
        _done.pop(key, None)

    return live.Outcome(
        reply, commit=commit, abort=abort,
        after=_refresh(uid, mid, data, caption, None if deleted else meal, sess),
    )


async def on_meal(
    uid: int, mid: int, data: str, user: User, assistant: Assistant
) -> live.Outcome:
    """`m:a|d|u|r:…` — правка записи из её карточки (AC-285, AC-286)."""
    m = _ADJ.fullmatch(data) or _DEL.fullmatch(data) or _UNDO.fullmatch(data) or _BACK.fullmatch(
        data)
    if m is None:
        return live.Outcome({"err": "bad"})
    now = _now()
    _sweep(now)
    key = (uid, m.groups()[-1])
    seen = _done.get(key)
    if seen is not None:
        return live.Outcome(seen[1])  # повтор того же тапа — прежний ответ, второй правки нет
    # Ключ занят ДО правки: такой же запрос, пришедший, пока первый пишет, получит `busy`.
    _done[key] = (now, {"err": "busy"})
    try:
        out = await _apply(uid, mid, m, data[2], key, now, user, assistant)
    except BaseException:
        _done.pop(key, None)
        raise
    if not out.reply.get("ok"):
        _done.pop(key, None)
    return out


async def _apply(
    uid: int, mid: int, m: re.Match[str], op: str, key: tuple[int, str], now: float,
    user: User, assistant: Assistant,
) -> live.Outcome:
    meal_id = int(m.group(1))
    slot = _undo.get((uid, mid))

    if op == "r":  # «⏪ Вернуть»
        if slot is None or slot.snapshot.id != meal_id:
            return live.Outcome({"err": "old"})
        try:
            back = await assistant.restore_meal(user, slot.snapshot, expect=slot.after)
        except SnapshotStale:
            return live.Outcome({"err": "edited"})
        if back is None:
            return live.Outcome({"err": "bad"})
        return await _answer(uid, mid, key, now, user, assistant, back.meal, undo=0,
                             drop_slot=True)

    meal = await assistant.get_meal(user, meal_id)
    if meal is None:
        return live.Outcome({"err": "gone"})  # чужая или уже удалённая — правка невозможна

    if op == "a":
        item_id, grams = int(m.group(2)), int(m.group(3))
        if not MIN_PORTION_G <= grams <= MAX_PORTION_G:
            return live.Outcome({"err": "bad"})
        if item_id not in {i.id for i in weighable(meal)}:
            return live.Outcome({"err": "bad"})  # позиция без веса или не этой записи
        res = await assistant.adjust_meal_item(user, meal_id, item_id, float(grams))
        if res is None or res.meal is None:
            return live.Outcome({"err": "gone"})
        # Вес поправили — снимок «Вернуть» устарел (отпечаток не сойдётся): кнопку прячем, а не
        # обещаем то, что кончится «уже поправили». Обновить отпечаток нельзя — тогда «Вернуть»
        # молча откатил бы и эту правку граммов.
        return await _answer(uid, mid, key, now, user, assistant, res.meal, undo=0,
                             drop_slot=True)

    snapshot = copy.deepcopy(meal)
    if op == "d":
        item_id = int(m.group(2))
        if len(meal.items) < 2 or item_id not in {i.id for i in meal.items}:
            return live.Outcome({"err": "bad"})  # последнюю позицию — «Отменить всё»
        rest = await assistant.remove_item(user, meal_id, item_id)
        if rest is None:
            return live.Outcome({"err": "gone"})
        new = _Undo(now, snapshot, meal_fingerprint(rest.meal), 1)
        return await _answer(uid, mid, key, now, user, assistant, rest.meal, undo=1, slot=new)

    # op == "u": удалить запись целиком
    deleted = await assistant.delete_meal(user, meal_id)
    if deleted.meal is None:
        return live.Outcome({"err": "gone"})
    new = _Undo(now, snapshot, meal_fingerprint(None), 2)
    return await _answer(uid, mid, key, now, user, assistant, snapshot, undo=2, deleted=True,
                         slot=new)


live.ROUTES["m"] = on_meal
