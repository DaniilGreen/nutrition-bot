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
from aiogram.types import InlineKeyboardMarkup

from app.application.assistant import Assistant
from app.application.entities import Meal, User
from app.application.meal_service import SnapshotStale, meal_fingerprint
from app.application.results import MealLogged, MealSession
from app.bot.keyboards import meal_widget_keyboard, weighable
from app.bot.ui.widgets import meal_data, merged_data
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
_OPEN = re.compile(rf"o:{_ID}:{_KEY}")
_T_ADJ = re.compile(rf"s:a:{_ID}:{_ID}:([0-9]{{1,4}}):{_KEY}")
_T_DEL = re.compile(rf"s:x:{_ID}:{_KEY}")
_T_BACK = re.compile(rf"s:r:{_KEY}")

_done: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}  # (кто, ключ) → (когда, ответ)


class _Undo:
    """Что вернуть виджету `(кто, сообщение)`: запись до удаления и отпечаток после (FR-41)."""

    def __init__(self, at: float, snapshot: Meal, after: object, kind: int) -> None:
        self.at, self.snapshot, self.after, self.kind = at, snapshot, after, kind


_undo: dict[tuple[int, int], _Undo] = {}
# Карточки, открытые тапом по записи (D-69): шапка «Запись», не «Записал» (MA-INV-70).
_opened: set[tuple[int, int]] = set()
# (кто, сообщение) → ревизия, что уже лежит в сообщении: правка со старшей не ложится поверх.
_rv: dict[tuple[int, int], int] = {}
_now = time.monotonic


def _sweep(now: float) -> None:
    for k in [k for k, (at, _) in _done.items() if now - at > TTL_S]:
        del _done[k]
    for u in [k for k, v in _undo.items() if now - v.at > TTL_S]:
        del _undo[u]


async def _card(
    assistant: Assistant, user: User, meal: Meal, *, undo: int = 0, deleted: bool = False,
    left: Remaining | None = None, head: str | None = None, opened: bool = False,
) -> tuple[str | None, MealSession | None]:
    """Данные и приём пищи (FR-47, только из нескольких записей) карточки из базы;
    остаток — дня самой записи, а не сегодняшнего."""
    left = left or await assistant.day_left(user, meal.eaten_at)
    sess = None if deleted or meal.id is None else await assistant.session_of(user, meal.id)
    if sess is not None and len(sess.meals) < 2:
        sess = None
    ss = sum(m.totals.kcal for m in sess.meals) if sess is not None else None
    data = meal_data(meal, left, user.timezone, session_kcal=ss, undo=undo, deleted=deleted,
                     head="Запись" if opened else head)
    return data, sess


async def show(
    bot: Bot, chat_id: int, user: User, assistant: Assistant, result: MealLogged, *,
    head: str | None = None,
) -> bool:
    """Отправить карточку записи виджетом. False — не вышло или не влезло: вызывающий шлёт
    rich-карточку с прежними кнопками (W-INV-2). head — у слитого набора (FR-47)."""
    try:
        data, sess = await _card(
            assistant, user, result.meal, left=result.remaining, head=head
        )
        if data is None:
            return False
        await bot(SendWidget(
            chat_id=chat_id, html=template("meal"), chat_data=data,
            allow_queries=True, reply_markup=meal_widget_keyboard(result.meal, sess),
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("widget: карточка приёма не ушла (%s) — фолбэк на rich", exc)
        return False
    return True


async def _edit(
    bot: Bot, uid: int, mid: int, data: str, meal: Meal | None,
    sess: MealSession | None,
) -> bool:
    """Правка самого сообщения: данные и кнопки под ним. False — данные не легли (это не
    виджет или сообщения нет); кнопки тогда не трогаем.

    Под замком человека и со сверкой ревизии: тап в виджете и ответ текстом правят одно сообщение
    задачами, и собранное раньше не должно лечь последним (открытая позже карточка врала бы)."""
    async with live.lock(uid):
        rv, key = json.loads(data).get("rv", 0), (uid, mid)
        if rv < _rv.get(key, 0):
            return True  # в сообщении уже свежее — эта правка устарела
        if len(_rv) > 200:  # ponytail: двое людей; чистим целиком, худшее — одна лишняя правка
            _rv.clear()
        _rv[key] = rv
        return await _put(bot, uid, mid, data, meal, sess)


async def _put(
    bot: Bot, uid: int, mid: int, data: str, meal: Meal | None,
    sess: MealSession | None,
) -> bool:
    try:
        await bot(EditMessageWidgetData(chat_id=uid, message_id=mid, chat_data=data))
    except Exception as exc:  # noqa: BLE001
        if "not modified" not in str(exc).lower():
            logger.warning("widget: данные карточки приёма не правятся: %s", exc)
            return False
    try:
        await bot.edit_message_reply_markup(  # подписи нет (D-75) — только кнопки
            chat_id=uid, message_id=mid, reply_markup=meal_widget_keyboard(meal, sess),
        )
    except Exception as exc:  # noqa: BLE001 — данные уже легли: виджет показывает правду
        if "not modified" not in str(exc).lower():
            logger.warning("widget: кнопки карточки приёма не правятся: %s", exc)
    return True


async def card(
    assistant: Assistant, user: User, result: MealLogged
) -> tuple[str | None, InlineKeyboardMarkup | None]:
    """Данные и кнопки свежей карточки — для того, кто шлёт её позже, без сессии БД
    (подтверждение разбора, FR-49.7)."""
    data, sess = await _card(assistant, user, result.meal, left=result.remaining)
    return data, meal_widget_keyboard(result.meal, sess)


def _refresh(
    uid: int, mid: int, data: str, meal: Meal | None, sess: MealSession | None
) -> Callable[[Bot], Awaitable[None]]:
    """Правка самого сообщения после ответа виджету — открытая позже карточка покажет правду."""

    async def run(bot: Bot) -> None:
        await _edit(bot, uid, mid, data, meal, sess)

    return run


async def merged(bot: Bot, uid: int, mid: int, name: str) -> bool:
    """Виджет, где нажали «📌 Приём пищи — в набор», — след «вошло в набор» без действий (FR-47,
    FR-49.4): его записей больше нет. False — это не виджет: вызывающий правит rich-следом."""
    _undo.pop((uid, mid), None)
    return await _edit(bot, uid, mid, merged_data(name), None, None)


async def refresh_fix(
    bot: Bot, user: User, assistant: Assistant, mid: int, meal: Meal | None,
    snapshot: Meal | None,
) -> bool:
    """Ответ текстом на карточку-виджет (FR-44, D-61): перерисовать сам виджет из базы. `meal` —
    запись после правки (None — удалена целиком), `snapshot` — до замены/удаления: тогда
    «⏪ Вернуть» — кнопкой в виджете (слот `m:r`, отпечаток FR-41); у правки веса снимка нет.
    False — это не виджет (rich-карточка) или его не править: вызывающий идёт прежним rich-путём.
    Звать ПОСЛЕ фиксации: слот «Вернуть» не должен пережить откат."""
    uid = user.telegram_id
    shown = meal or snapshot
    if shown is None:
        return False
    undo = 0 if snapshot is None else 1 if meal is not None else 2
    data, sess = await _card(assistant, user, shown, undo=undo, deleted=meal is None)
    if data is None or not await _edit(bot, uid, mid, data, meal, sess):
        return False
    if snapshot is not None:
        _undo[(uid, mid)] = _Undo(_now(), snapshot, meal_fingerprint(meal), undo)
    else:
        _undo.pop((uid, mid), None)  # вес поправили — прежний «Вернуть» устарел
    return True


async def _answer(
    uid: int, mid: int, key: tuple[int, str], now: float, user: User, assistant: Assistant,
    meal: Meal, *, undo: int, deleted: bool = False, slot: _Undo | None = None,
    drop_slot: bool = False,
) -> live.Outcome:
    data, sess = await _card(assistant, user, meal, undo=undo, deleted=deleted,
                             opened=(uid, mid) in _opened)
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
        after=_refresh(uid, mid, data, None if deleted else meal, sess),
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


async def on_open(
    uid: int, mid: int, data: str, user: User, assistant: Assistant
) -> live.Outcome:
    """`o:<запись>:<ключ>` — тап по записи в «Сегодня» / «✏️ Править» в «Истории» (D-69): её
    карточка приёма следом — там ±10 г, 🗑, «Отменить всё», «Вернуть». Ничего не пишет."""
    m = _OPEN.fullmatch(data)
    if m is None:
        return live.Outcome({"err": "bad"})
    now = _now()
    _sweep(now)
    key = (uid, m.group(2))
    if (seen := _done.get(key)) is not None:
        return live.Outcome(seen[1])  # двойной тап — одна карточка
    meal = await assistant.get_meal(user, int(m.group(1)))
    if meal is None:
        return live.Outcome({"err": "gone"})  # чужая или уже удалённая
    left = await assistant.day_left(user, meal.eaten_at)
    data_, sess = await _card(assistant, user, meal, left=left, opened=True)
    if data_ is None:
        return live.Outcome({"err": "bad"})
    reply: dict[str, Any] = {"ok": 1}

    def commit() -> None:
        _done[key] = (now, reply)

    async def send(bot: Bot) -> None:
        msg = await bot(SendWidget(
            chat_id=uid, html=template("meal"), chat_data=data_,
            allow_queries=True, reply_markup=meal_widget_keyboard(meal, sess),
        ))
        if len(_opened) > 200:  # ponytail: двое людей; чистим целиком — худшее: шапка «Записал»
            _opened.clear()
        _opened.add((uid, msg.message_id))

    return live.Outcome(reply, commit=commit, after=send)


async def on_today(
    uid: int, mid: int, data: str, user: User, assistant: Assistant
) -> live.Outcome:
    """Правка прямо в «Сегодня» (D-74): `s:a:<запись>:<позиция>:<г>` — вес той же
    `adjust_meal_item`, что ±10 г карточки; `s:x:<запись>` — удалить (`delete_meal`); `s:r` —
    «⏪ Вернуть» удалённое этим виджетом (отпечаток FR-41). Ответ — данные дня из `live.build`."""
    m = _T_ADJ.fullmatch(data) or _T_DEL.fullmatch(data) or _T_BACK.fullmatch(data)
    if m is None:
        return live.Outcome({"err": "bad"})
    now = _now()
    _sweep(now)
    key, slot_key = (uid, m.groups()[-1]), (uid, mid)
    if (seen := _done.get(key)) is not None:
        return live.Outcome(seen[1])
    op, slot = data[2], _undo.get(slot_key)
    commit_slot: _Undo | None = None
    drop = False
    if op == "r":
        if slot is None:
            return live.Outcome({"err": "old"})
        try:
            if await assistant.restore_meal(user, slot.snapshot, expect=slot.after) is None:
                return live.Outcome({"err": "bad"})
        except SnapshotStale:
            return live.Outcome({"err": "edited"})
        drop = True
    else:
        meal = await assistant.get_meal(user, int(m.group(1)))
        if meal is None:
            return live.Outcome({"err": "gone"})
        if op == "a":
            item_id, grams = int(m.group(2)), int(m.group(3))
            if item_id not in {i.id for i in weighable(meal)} or not (
                    MIN_PORTION_G <= grams <= MAX_PORTION_G):
                return live.Outcome({"err": "bad"})
            if await assistant.adjust_meal_item(user, meal.id or 0, item_id, float(grams)) is None:
                return live.Outcome({"err": "gone"})
        else:
            snapshot = copy.deepcopy(meal)
            if (await assistant.delete_meal(user, meal.id or 0)).meal is None:
                return live.Outcome({"err": "gone"})
            commit_slot = _Undo(now, snapshot, meal_fingerprint(None), 2)
    built = await live.build(assistant, user)
    undo = commit_slot is not None or (slot is not None and not drop)
    reply = {**json.loads(built.data), "ok": 1, "z": int(undo)}

    def commit() -> None:
        _done[key] = (now, reply)
        if commit_slot is not None:
            _undo[slot_key] = commit_slot
        elif drop:
            _undo.pop(slot_key, None)

    return live.Outcome(reply, commit=commit)


live.ROUTES["m"] = on_meal
live.ROUTES["s"] = on_today
live.ROUTES["o"] = on_open
