"""«🎯 Цель» виджетом (FR-49.9, D-72): дневная норма, кольцо ккал по Б/Ж/У, смена цели чипами.

Смена цели — тем же `rebalance` от текущих калорий и веса и тем же `users.set_goal`, что кнопки
rich-экрана: «как делить», а не «сколько есть» (FR-16). Граница доверия (W-INV-4): цель — одна из
трёх; ключ на тап (W-INV-5) — повтор безвреден и так (цель та же), но ответ не пересчитывается.
"""
from __future__ import annotations

import json
import logging
import re

from aiogram import Bot

from app.application.assistant import Assistant
from app.application.entities import User
from app.bot.ui.widgets import goal_data
from app.bot.widgets import live, template
from app.bot.widgets.transport import EditMessageWidgetData, SendWidget
from app.domain.goals import Goal, rebalance

logger = logging.getLogger(__name__)
_SET = re.compile(r"g:t:(cut|maintain|bulk):([0-9a-z]{4,10})")


async def _weight(assistant: Assistant, user: User) -> float | None:
    trend = await assistant.weight_trend(user, limit=1)
    return trend.logs[0].weight_kg if trend.logs else None


async def show(bot: Bot, chat_id: int, user: User, assistant: Assistant, goal: Goal,
               head: str = "") -> bool:
    """Отправить цель виджетом. False — не вышло: вызывающий шлёт rich-экран (W-INV-2)."""
    try:
        await bot(SendWidget(
            chat_id=chat_id, html=template("goal"),
            chat_data=goal_data(goal, await _weight(assistant, user), head),
            allow_queries=True,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("widget: цель не ушла (%s) — фолбэк на rich", exc)
        return False
    return True


async def on_goal(uid: int, mid: int, data: str, user: User, assistant: Assistant) -> live.Outcome:
    """`g:t:<цель>:<ключ>` — сменить цель (похудение/поддержание/набор)."""
    m = _SET.fullmatch(data)
    users = (live.EXTRA.get() or {}).get("users")
    if m is None or users is None or user.id is None:
        return live.Outcome({"err": "bad"})
    wkg = await _weight(assistant, user)
    if wkg is None:
        return live.Outcome({"err": "noweight"})
    current = await assistant.current_goal(user)
    goal = rebalance(current.daily_kcal, wkg, m.group(1))
    await users.set_goal(user.id, goal)
    text = goal_data(goal, wkg)

    async def after(bot: Bot) -> None:
        await bot(EditMessageWidgetData(chat_id=uid, message_id=mid, chat_data=text))

    return live.Outcome({**json.loads(text), "ok": 1}, after=after)


live.ROUTES["g"] = on_goal
