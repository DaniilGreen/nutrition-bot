"""Живой виджет «📊 Сегодня» (FR-49.1, D-43, D-46, D-47) и приём `widget_query` (W-INV-4).

Свежесть — пуш и тяга (D-46):
- пуш: после каждого апдейта человека `push()` пересобирает `chat_data` и правит виджет дня,
  если данные изменились (запись, правка, удаление, «Вернуть» — какой бы хендлер их ни сделал);
- тяга: виджет при появлении на экране шлёт фоновый `t:<дата>` — так видны записи из аппки
  (другой процесс) и правки, сделанные, пока бот перезапускался.

ponytail: какой виджет «живой» — помним в памяти процесса. Потолок: после рестарта пуш молчит,
пока виджет не покажется на экране и не спросит сам (запрос заново учит его id). Апгрейд —
таблица `widget_messages` (миграция), если пуш после рестарта станет важен.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.application.assistant import Assistant
from app.application.entities import User
from app.bot.keyboards import open_in_app
from app.bot.ui.widgets import today_data, weight_data
from app.bot.widgets import template
from app.bot.widgets.transport import AnswerWidgetQuery, EditMessageWidgetData, SendWidget
from app.config import get_settings
from app.domain.prefs import day_start
from app.domain.timeframe import local_date

logger = logging.getLogger(__name__)

_QUERY = re.compile(r"t:([0-9]{4}-[0-9]{2}-[0-9]{2})")
_VOLATILE = ("at", "hn")  # меняются сами со временем — правку не оправдывают
# Виджета больше нет (удалили руками, > 48 ч): забыть, а не биться в API на каждом апдейте.
_GONE = ("not found", "can't be edited", "message_id_invalid", "message to edit")
CAPTION = "📊 Сегодня"  # W-INV-2, D-60: цифры — в самом виджете, подпись их не повторяет


@dataclass
class _Live:
    day: str  # ISO-дата дня виджета
    message_id: int
    key: str  # chat_data без «живых» полей — что сейчас нарисовано


_LIVE: dict[int, _Live] = {}  # telegram id (= id личного чата) → виджет дня
_LOCKS: dict[int, asyncio.Lock] = {}
# Показал ли «Сегодня» текущий апдейт: тогда пуш после него — лишняя сборка того же самого.
# В контексте лежит изменяемая метка: её видно и через копию контекста.
_SHOWN: ContextVar[list[bool] | None] = ContextVar("widget_shown", default=None)


def lock(uid: int) -> asyncio.Lock:
    """Замок человека: сборка данных дня и правка виджета — одна операция (гонка пушей).

    Порядок захвата: хендлер держит свою сессию БД и ждёт замка (`show_today`), пуш держит замок
    и открывает сессию. Взаимной блокировки нет, пока пул соединений больше числа людей, которые
    пишут одновременно (пул 5 + 10, людей двое); ponytail: при росте — собирать до замка."""
    return _LOCKS.setdefault(uid, asyncio.Lock())


def begin_update() -> list[bool]:
    """Метка апдейта: middleware ставит её до хендлера, `show_today` отмечает показ.

    Контракт: `show_today` — последнее, что хендлер делает с днём. Хендлер, который пишет ПОСЛЕ
    показа, оставит виджет со старыми цифрами до следующего апдейта (пуш этого апдейта пропущен)."""
    mark = [False]
    _SHOWN.set(mark)
    return mark


def enabled(user: User, spec: str | None = None) -> bool:
    """Флаг раскатки `WIDGETS_USERS` (D-51): `owner`, telegram id через запятую, `all`."""
    raw = get_settings().widgets_users if spec is None else spec
    parts = {p.strip().lower() for p in raw.split(",") if p.strip()}
    return (
        "all" in parts
        or str(user.telegram_id) in parts
        or ("owner" in parts and user.role == "owner")
    )


def _key(data: str) -> str:
    obj = json.loads(data)
    for k in _VOLATILE:
        obj.pop(k, None)
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


@dataclass
class Built:
    day: str
    data: str


async def build(assistant: Assistant, user: User, now: datetime | None = None) -> Built:
    """Данные и подпись виджета дня из тех же сервисов, что у rich-экрана (MA-INV-1)."""
    now = now or datetime.now(UTC)
    start_h = day_start(user.ui_prefs)
    stats = await assistant.today(user)
    goal = await assistant.current_goal(user)
    trend = await assistant.weight_trend(user, limit=1)
    pace = await assistant.day_pace(user)
    streak = await assistant.streak(user)
    day = local_date(now, stats.timezone, start_h)
    weight = trend.logs[0].weight_kg if trend.logs else None
    data = today_data(stats, goal, weight, pace, streak, now, start_h)
    return Built(day.isoformat(), data)


async def show_today(bot: Bot, chat_id: int, user: User, assistant: Assistant) -> bool:
    """Отправить виджет дня. False — не вышло, вызывающий шлёт rich-экран (W-INV-2, AC-272).

    Тот же день уже показан — новый виджет внизу, прежний удаляется (D-47)."""
    async with lock(user.telegram_id):
        # Прежний — ДО отправки: новый виджет сразу шлёт `t:`, а ответ на него (без замка) учит
        # `_LIVE` уже его id — взятый после, `old` оказался бы им самим, и прежний не удалился бы.
        old = _LIVE.get(user.telegram_id)
        try:
            built = await build(assistant, user)
            msg = await bot(
                SendWidget(
                    chat_id=chat_id, html=template("today"), chat_data=built.data,
                    caption=CAPTION, allow_queries=True, reply_markup=open_in_app("day"),
                )
            )
        except Exception as exc:  # noqa: BLE001 — любой сбой виджета не должен оставить без «Сегодня»
            logger.warning("widget: sendWidget не прошёл (%s) — фолбэк на rich", exc)
            return False
        _LIVE[user.telegram_id] = _Live(built.day, msg.message_id, _key(built.data))
        mark = _SHOWN.get()
        if mark is not None:
            mark[0] = True
        if old is not None and old.day == built.day and old.message_id != msg.message_id:
            with suppress(Exception):
                await bot.delete_message(chat_id, old.message_id)
    return True


async def show_weight(
    bot: Bot, chat_id: int, points: list[tuple[datetime, float]], tz: str, target: str,
    caption: str = "⚖️ Вес",
) -> bool:
    """«⚖️ Вес» графиком (FR-49.3) — единственное сообщение пути (D-60). False — замеров меньше
    двух или не вышло: вызывающий шлёт прежние rich/текст и PNG (D-55)."""
    try:
        data = weight_data(points, tz, target)
        if data is None:
            return False
        burn = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="🔥 Реальный расход", callback_data="burn")]])  # FR-50: `burn.CALLBACK`
        await bot(SendWidget(
            chat_id=chat_id, html=template("weight"), chat_data=data, caption=caption,
            reply_markup=open_in_app("body", burn),
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("widget: вес не ушёл (%s) — фолбэк на PNG", exc)
        return False
    return True


async def _apply(bot: Bot, chat_id: int, live: _Live, built: Built) -> None:
    """Правка данных живого виджета; «not modified» — не ошибка. Подпись цифр не несёт и не
    правится (W-INV-2, D-60)."""
    key = _key(built.data)
    if key == live.key:
        return
    try:
        await bot(EditMessageWidgetData(
            chat_id=chat_id, message_id=live.message_id, chat_data=built.data
        ))
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise
    live.key = key


def watching(user: User) -> bool:
    """Есть ли живой виджет дня — дешёвая проверка до похода в БД (пуш после каждого апдейта)."""
    return user.telegram_id in _LIVE


async def _safe_apply(bot: Bot, uid: int, live: _Live, built: Built) -> None:
    try:
        await _apply(bot, uid, live, built)
    except TelegramBadRequest as exc:
        if any(w in str(exc).lower() for w in _GONE) and _LIVE.get(uid) is live:
            _LIVE.pop(uid, None)
        logger.warning("widget: правка виджета дня не прошла: %s", exc)
    except Exception as exc:  # noqa: BLE001 — виджет подтянет сам при показе (запрос t:)
        logger.warning("widget: правка виджета дня не прошла: %s", exc)


Opener = Callable[[], AbstractAsyncContextManager[Assistant]]


async def push(bot: Bot, user: User, open_assistant: Opener) -> None:
    """После апдейта человека: виджет дня показывает то, что сейчас в базе (D-46, AC-273).

    Под замком человека: сборка и правка — одна операция, иначе два апдейта подряд (aiogram
    гоняет их задачами) могут положить старые цифры поверх свежих. Сессия БД закрыта до похода
    в Telegram — соединение пула не ждёт HTTP."""
    uid = user.telegram_id
    async with lock(uid):
        live = _LIVE.get(uid)
        if live is None or not enabled(user):
            return
        async with open_assistant() as assistant:
            built = await build(assistant, user)
        if built.day != live.day:  # новый день: вчерашний виджет — снимок, забываем его
            if _LIVE.get(uid) is live:
                _LIVE.pop(uid, None)
            return
        await _safe_apply(bot, uid, live, built)


def _reply(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def parse_origin(q: dict[str, Any]) -> tuple[int, int, str] | None:
    """Откуда запрос (W-INV-4): (telegram id, message_id, data) или None.

    Только личный чат бота (`chat.id == from.id`), живой человек, `data` — строка ≤ 64 байт.
    Формат самой `data` разбирает тот, кому она адресована (`t:` — здесь, `w:`/`u:` — «Частое»)."""
    data, frm, chat, mid = q.get("data"), q.get("from"), q.get("chat"), q.get("message_id")
    if not isinstance(data, str) or not data or len(data.encode()) > 64:
        return None
    if not isinstance(frm, dict) or not isinstance(chat, dict):
        return None
    uid = frm.get("id")
    if not isinstance(uid, int) or isinstance(uid, bool) or frm.get("is_bot"):
        return None
    if chat.get("type") != "private" or chat.get("id") != uid:
        return None
    if not isinstance(mid, int) or isinstance(mid, bool) or mid <= 0:
        return None
    return uid, mid, data


def parse_query(q: dict[str, Any]) -> tuple[int, int, str] | None:
    """Запрос виджета дня: (telegram id, message_id, день) или None; `data` ровно `t:YYYY-MM-DD`."""
    origin = parse_origin(q)
    m = _QUERY.fullmatch(origin[2]) if origin else None
    if origin is None or m is None:
        return None
    try:
        datetime.strptime(m.group(1), "%Y-%m-%d")
    except ValueError:
        return None
    return origin[0], origin[1], m.group(1)


@dataclass
class Outcome:
    """Итог пишущего запроса. `commit` — что запомнить ПОСЛЕ фиксации (ключ повтора, «что
    записал»), `abort` — что снять, если фиксация упала: иначе ответ «записал» и кэш повтора
    врали бы о записи, которой нет."""

    reply: dict[str, Any]
    commit: Callable[[], None] | None = None
    abort: Callable[[], None] | None = None
    # После ответа виджету (сессия уже закрыта): поправить само сообщение — данные, подпись,
    # кнопки, — чтобы открытый позже виджет показал правду, а не то, что был при отправке.
    after: Callable[[Bot], Awaitable[None]] | None = None


# Запросы других виджетов: префикс `data` до «:» → обработчик. Обработчик пишет через assistant;
# фиксацию, ответ и пуш виджета дня делает middleware (`app/bot/middlewares.py`).
Route = Callable[[int, int, str, User, Assistant], Awaitable[Outcome]]
ROUTES: dict[str, Route] = {}
# Что ещё нужно маршрутам сверх Assistant (`users` — сменить цель, `scans` — пол недели по обмену);
# кладёт middleware перед вызовом маршрута, в той же сессии БД.
EXTRA: ContextVar[dict[str, Any] | None] = ContextVar("widget_extra", default=None)


async def respond(
    parsed: tuple[int, int, str], user: User, assistant: Assistant
) -> tuple[dict[str, Any], Built | None]:
    """Ответ на `t:<день>` и собранное для него. Без замка: чтение не гоняется с пушем, а ответ
    не должен ждать медленную правку чужого апдейта (15 с до TIMEOUT). Правка самого сообщения —
    после ответа, через `push`: под замком и свежей сборкой.

    Сбой сборки — исключение: вызывающий отвечает `{"err":"fail"}` (без ответа виджет ждал бы
    15 с и получил TIMEOUT)."""
    if not enabled(user):
        return {"err": "bad"}, None  # AC-270: данные не читаем
    uid, mid, day = parsed
    built = await build(assistant, user)
    live = _LIVE.get(uid)
    if built.day != day or (live is not None and live.day == day and mid < live.message_id):
        # Вчерашний — «итог дня» (1), старший того же дня — «снимок» (2); не правим (AC-274).
        return {"st": 1 if built.day != day else 2}, None
    if live is None or live.message_id != mid:  # после рестарта: учимся заново
        _LIVE[uid] = _Live(day, mid, "")
    return json.loads(built.data), built


async def reply(bot: Bot, qid: str, obj: dict[str, Any]) -> None:
    """`answerWidgetQuery`; сбой — в лог: виджет сам покажет TIMEOUT, поллинг не падает."""
    try:
        await bot(AnswerWidgetQuery(widget_query_id=qid, data=_reply(obj)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("widget: ответ на запрос не ушёл: %s", exc)
