"""Подтверждение разбора одним виджетом (FR-49.7, каталог #11, D-66): размеры S/M/L и ±10 г у
всех позиций сразу вместо цепочки вопросов «Маленькая / Средняя / Большая».

Запись — тем же `continue_meal`, что ответ на вопрос размера: граммы из виджета ставятся явными
(INV-7, «явный вес»), плотность — та, что запиннулась при предпросмотре (INV-2). Граница доверия
(W-INV-4): `c:w:<граммы через запятую>:<ключ>` строго по формату, число граммов — ровно по позициям,
1…2000 г; черновик ищется по (человек, сообщение). Идемпотентность (W-INV-5): ключ на тап.

ponytail: черновик — в памяти процесса на 30 минут. Рестарт стирает: «Записать» после рестарта —
отказ `old`, человек присылает еду заново. Апгрейд — FSM-хранилище, если рестарты станут мешать.
"""
from __future__ import annotations

import copy
import logging
import re
import time
from typing import Any

from aiogram import Bot

from app.application.assistant import Assistant
from app.application.dto import ParsedMessage, Unit
from app.application.entities import User
from app.application.meal_service import ItemPreview
from app.application.results import MealLogged
from app.bot.ui.widgets import confirm_data, meal_caption
from app.bot.widgets import live, template
from app.bot.widgets import meal as wmeal
from app.bot.widgets.transport import EditMessageWidgetData, SendWidget
from app.domain.plausibility import MAX_PORTION_G, MIN_PORTION_G

logger = logging.getLogger(__name__)

TTL_S = 1800
MAX_ITEMS = 4  # больше — выше 560 px на 320 (W-INV-8); прежняя цепочка вопросов (D-66)
_WRITE = re.compile(r"c:w:([0-9]{1,4}(?:,[0-9]{1,4}){0,7}):([0-9a-z]{4,10})")
_CANCEL = re.compile(r"c:x:([0-9a-z]{4,10})")


class _Draft:
    def __init__(self, at: float, draft: ParsedMessage, raw: str, items: list[ItemPreview]):
        self.at, self.draft, self.raw, self.items = at, draft, raw, items


_drafts: dict[tuple[int, int], _Draft] = {}  # (кто, сообщение) → черновик
_done: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}  # (кто, ключ) → ответ
_now = time.monotonic


def _sweep(now: float) -> None:
    for k in [k for k, v in _drafts.items() if now - v.at > TTL_S]:
        del _drafts[k]
    for d in [k for k, (at, _) in _done.items() if now - at > TTL_S]:
        del _done[d]


async def show(
    bot: Bot, chat_id: int, user: User, assistant: Assistant, draft: ParsedMessage, raw: str
) -> bool:
    """Отправить подтверждение. False — позиций больше 4, не влезло или не вышло: вызывающий
    задаёт вопрос размера прежней цепочкой (W-INV-2)."""
    if len(draft.items) > MAX_ITEMS:
        return False
    try:
        items = await assistant.preview_meal(draft)
        data = confirm_data(items)
        if data is None:
            return False
        msg = await bot(SendWidget(
            chat_id=chat_id, html=template("confirm"), chat_data=data, allow_queries=True,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("widget: подтверждение разбора не ушло (%s) — прежние вопросы", exc)
        return False
    now = _now()
    _sweep(now)
    _drafts[(user.telegram_id, msg.message_id)] = _Draft(now, copy.deepcopy(draft), raw, items)
    return True


def _apply(slot: _Draft, grams: list[int]) -> ParsedMessage | str:
    """Граммы виджета → черновик с явным весом. Строка — отказ (`need`/`bad`)."""
    if len(grams) != len(slot.items):
        return "bad"
    draft = copy.deepcopy(slot.draft)
    for item, prev, g in zip(draft.items, slot.items, grams, strict=True):
        if prev.fixed or not prev.per_gram:
            continue  # вес из имени и свои КБЖУ (FR-22) — как разобрано
        if g == 0:
            return "need"  # размер не выбран: не угадываем (FR-21)
        if not MIN_PORTION_G <= g <= MAX_PORTION_G:
            return "bad"
        item.grams, item.unit, item.portion_hint, item.quantity = float(g), Unit.GRAM, None, None
    return draft


async def on_confirm(
    uid: int, mid: int, data: str, user: User, assistant: Assistant
) -> live.Outcome:
    """`c:w:…` — записать, `c:x:…` — отменить (AC-297)."""
    w, x = _WRITE.fullmatch(data), _CANCEL.fullmatch(data)
    m = w or x
    if m is None:
        return live.Outcome({"err": "bad"})
    now = _now()
    _sweep(now)
    key = (uid, m.groups()[-1])
    if (seen := _done.get(key)) is not None:
        return live.Outcome(seen[1])  # повтор тапа — прежний ответ, второй записи нет
    slot = _drafts.get((uid, mid))
    if slot is None:
        return live.Outcome({"err": "old"})  # рестарт, 30 минут или уже записано

    if x is not None:
        reply: dict[str, Any] = {"ok": 1, "x": 1}

        def drop() -> None:
            _done[key] = (now, reply)
            _drafts.pop((uid, mid), None)

        async def cancelled(bot: Bot) -> None:
            await bot(EditMessageWidgetData(chat_id=uid, message_id=mid,
                                            chat_data='{"v":1,"x":1}'))

        return live.Outcome(reply, commit=drop, after=cancelled)

    grams = [int(g) for g in w.group(1).split(",")]  # type: ignore[union-attr]
    draft = _apply(slot, grams)
    if isinstance(draft, str):
        return live.Outcome({"err": draft})
    _done[key] = (now, {"err": "busy"})  # занят ДО записи: второй такой же запрос не запишет
    try:
        result = await assistant.continue_meal(user, draft, raw_input=slot.raw)
        if not isinstance(result, MealLogged):  # все граммы явные — вопросов быть не должно
            logger.warning("widget: подтверждение дало %s вместо записи", type(result).__name__)
            _done.pop(key, None)
            return live.Outcome({"err": "bad"})
        card, kb = await wmeal.card(assistant, user, result)
    except BaseException:
        _done.pop(key, None)  # сессия откатится — ключ свободен для нового тапа
        raise
    reply = {"ok": 1}

    def commit() -> None:
        _done[key] = (now, reply)
        _drafts.pop((uid, mid), None)

    def abort() -> None:
        _done.pop(key, None)

    async def swap(bot: Bot) -> None:
        """Карточка приёма — следом, подтверждение — прочь (одна запись — одно сообщение)."""
        try:
            if card is None:
                raise ValueError("карточка не влезла")
            await bot(SendWidget(chat_id=uid, html=template("meal"), chat_data=card,
                                 allow_queries=True, reply_markup=kb))
        except Exception as exc:  # noqa: BLE001 — запись уже есть: сказать о ней текстом
            logger.warning("widget: карточка после подтверждения не ушла (%s)", exc)
            await bot.send_message(uid, f"{meal_caption(result.meal)} · "
                                        f"{round(result.meal.totals.kcal)} ккал", reply_markup=kb)
        await bot.delete_message(uid, mid)

    return live.Outcome(reply, commit=commit, abort=abort, after=swap)


live.ROUTES["c"] = on_confirm
