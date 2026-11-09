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

import json
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import TelegramObject, Update

from app.application.assistant import Assistant
from app.application.entities import User
from app.bot.keyboards import open_in_app
from app.bot.ui.widgets import today_caption, today_data
from app.bot.widgets import template
from app.bot.widgets.transport import AnswerWidgetQuery, EditMessageWidgetData, SendWidget
from app.config import get_settings
from app.domain.prefs import day_start
from app.domain.streak import logged_streak
from app.domain.timeframe import local_date

if TYPE_CHECKING:
    from app.container import Container

logger = logging.getLogger(__name__)

STREAK_WINDOW = 60  # дней истории для серии (D-49)
_QUERY = re.compile(r"t:([0-9]{4}-[0-9]{2}-[0-9]{2})")
_VOLATILE = ("at", "hn")  # меняются сами со временем — правку не оправдывают
# Виджета больше нет (удалили руками, > 48 ч): забыть, а не биться в API на каждом апдейте.
_GONE = ("not found", "can't be edited", "message_id_invalid", "message to edit")


@dataclass
class _Live:
    day: str  # ISO-дата дня виджета
    message_id: int
    key: str  # chat_data без «живых» полей — что сейчас нарисовано
    caption: str


_LIVE: dict[int, _Live] = {}  # telegram id (= id личного чата) → виджет дня


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
    caption: str


async def build(assistant: Assistant, user: User, now: datetime | None = None) -> Built:
    """Данные и подпись виджета дня из тех же сервисов, что у rich-экрана (MA-INV-1)."""
    now = now or datetime.now(UTC)
    start_h = day_start(user.ui_prefs)
    stats = await assistant.today(user)
    goal = await assistant.current_goal(user)
    trend = await assistant.weight_trend(user, limit=1)
    pace = await assistant.day_pace(user)
    rows, _ = await assistant.daily_series(user, STREAK_WINDOW)
    day = local_date(now, stats.timezone, start_h)
    streak = logged_streak([(d, m.kcal) for d, m, _ in rows], goal.daily_kcal, day)
    weight = trend.logs[0].weight_kg if trend.logs else None
    data = today_data(stats, goal, weight, pace, streak, now, start_h)
    return Built(day.isoformat(), data, today_caption(stats, goal))


async def show_today(bot: Bot, chat_id: int, user: User, assistant: Assistant) -> bool:
    """Отправить виджет дня. False — не вышло, вызывающий шлёт rich-экран (W-INV-2, AC-272).

    Тот же день уже показан — новый виджет внизу, прежний удаляется (D-47)."""
    try:
        built = await build(assistant, user)
        msg = await bot(
            SendWidget(
                chat_id=chat_id, html=template("today"), chat_data=built.data,
                caption=built.caption, allow_queries=True, reply_markup=open_in_app("day"),
            )
        )
    except Exception as exc:  # noqa: BLE001 — любой сбой виджета не должен оставить без «Сегодня»
        logger.warning("widget: sendWidget не прошёл (%s) — фолбэк на rich", exc)
        return False
    old = _LIVE.get(user.telegram_id)
    _LIVE[user.telegram_id] = _Live(built.day, msg.message_id, _key(built.data), built.caption)
    if old is not None and old.day == built.day and old.message_id != msg.message_id:
        with suppress(Exception):
            await bot.delete_message(chat_id, old.message_id)
    return True


async def _apply(bot: Bot, chat_id: int, live: _Live, built: Built) -> None:
    """Правка данных и подписи живого виджета; «not modified» — не ошибка."""
    key = _key(built.data)
    if key != live.key:
        try:
            await bot(EditMessageWidgetData(
                chat_id=chat_id, message_id=live.message_id, chat_data=built.data
            ))
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        live.key = key
    if built.caption != live.caption:
        try:
            await bot.edit_message_caption(
                chat_id=chat_id, message_id=live.message_id, caption=built.caption,
                parse_mode=None, reply_markup=open_in_app("day"),
            )
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        live.caption = built.caption


def watching(user: User) -> bool:
    """Есть ли живой виджет дня — дешёвая проверка до похода в БД (пуш после каждого апдейта)."""
    return user.telegram_id in _LIVE


async def _safe_apply(bot: Bot, uid: int, live: _Live, built: Built) -> None:
    try:
        await _apply(bot, uid, live, built)
    except TelegramBadRequest as exc:
        if any(w in str(exc).lower() for w in _GONE):
            _LIVE.pop(uid, None)
        logger.warning("widget: правка виджета дня не прошла: %s", exc)
    except Exception as exc:  # noqa: BLE001 — виджет подтянет сам при показе (запрос t:)
        logger.warning("widget: правка виджета дня не прошла: %s", exc)


async def push(bot: Bot, user: User, assistant: Assistant) -> None:
    """После апдейта человека: виджет дня показывает то, что сейчас в базе (D-46, AC-273)."""
    live = _LIVE.get(user.telegram_id)
    if live is None or not enabled(user):
        return
    built = await build(assistant, user)
    if built.day != live.day:  # наступил новый день — вчерашний виджет остаётся снимком
        return
    await _safe_apply(bot, user.telegram_id, live, built)


def _reply(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def parse_query(q: dict[str, Any]) -> tuple[int, int, str] | None:
    """Строгий разбор `widget_query` (W-INV-4): (telegram id, message_id, день) или None.

    Только личный чат бота (`chat.id == from.id`), `data` ≤ 64 байт ровно `t:YYYY-MM-DD`."""
    data, frm, chat, mid = q.get("data"), q.get("from"), q.get("chat"), q.get("message_id")
    if not isinstance(data, str) or len(data.encode()) > 64:
        return None
    m = _QUERY.fullmatch(data)
    if m is None or not isinstance(frm, dict) or not isinstance(chat, dict):
        return None
    uid = frm.get("id")
    if not isinstance(uid, int) or isinstance(uid, bool) or frm.get("is_bot"):
        return None
    if chat.get("type") != "private" or chat.get("id") != uid:
        return None
    if not isinstance(mid, int) or isinstance(mid, bool) or mid <= 0:
        return None
    try:
        datetime.strptime(m.group(1), "%Y-%m-%d")
    except ValueError:
        return None
    return uid, mid, m.group(1)


async def answer(
    bot: Bot, q: dict[str, Any], user: User | None, assistant: Assistant | None
) -> None:
    """Ответить на `widget_query` за 15 с; правка состояния — после ответа (док. 10.4)."""
    qid = q.get("id")
    if not isinstance(qid, str) or not qid:
        return
    parsed = parse_query(q)
    later: Built | None = None
    if parsed is None or user is None or assistant is None or not enabled(user):
        reply: dict[str, Any] = {"err": "bad"}  # AC-270: данные не читаем
    else:
        uid, mid, day = parsed
        try:
            built = await build(assistant, user)
        except Exception:  # noqa: BLE001 — без ответа виджет ждал бы 15 с и получил TIMEOUT
            logger.exception("widget: сборка данных дня по запросу упала")
            with suppress(Exception):
                await bot(AnswerWidgetQuery(widget_query_id=qid, data=_reply({"err": "fail"})))
            return
        live = _LIVE.get(uid)
        if built.day != day or (live is not None and live.day == day and mid < live.message_id):
            # Вчерашний — «итог дня» (1), старший того же дня — «снимок» (2); не правим (AC-274).
            reply = {"st": 1 if built.day != day else 2}
        else:
            reply = json.loads(built.data)
            if live is None or live.message_id != mid:  # после рестарта: учимся заново
                _LIVE[uid] = _Live(day, mid, "", "")
            later = built
    with suppress(Exception):
        await bot(AnswerWidgetQuery(widget_query_id=qid, data=_reply(reply)))
    if later is not None and parsed is not None and parsed[0] in _LIVE:
        await _safe_apply(bot, parsed[0], _LIVE[parsed[0]], later)


class WidgetQueryMiddleware(BaseMiddleware):
    """`widget_query` — апдейт 10.4, которого aiogram 3.31 не знает (D-45): он лежит в
    `Update.model_extra`, и без перехвата до разбора типа aiogram выкинул бы его с
    «unknown update type». Ловим здесь, на outer-middleware `dp.update`."""

    def __init__(self, container: Container) -> None:
        self._container = container

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        q = (event.model_extra or {}).get("widget_query") if isinstance(event, Update) else None
        if not isinstance(q, dict):
            return await handler(event, data)
        bot: Bot = data["bot"]
        parsed = parse_query(q)
        # OQ-W1: по этой строке в логе видно, что запросы виджета доходят до бота при поллинге.
        logger.info("widget: запрос %r, разобран: %s", q.get("data"), parsed is not None)
        if parsed is None:
            await answer(bot, q, None, None)
            return None
        frm = q["from"]
        try:
            async with self._container.db.session_factory() as session:
                assistant, users = self._container.assistant_for(session)
                user = await users.get_or_create(
                    telegram_id=parsed[0], name=frm.get("first_name"),
                    locale=frm.get("language_code") or "ru",
                    timezone=self._container.settings.default_timezone,
                )
                await answer(bot, q, user, assistant)
                await session.commit()
        except Exception:  # noqa: BLE001 — апдейт виджета не должен ронять поллинг
            logger.exception("widget: запрос виджета не обработан")
        return None
