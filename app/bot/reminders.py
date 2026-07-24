"""Вечернее напоминание-итог: фоновая asyncio-задача шлёт сводку дня в ~21:00 локально.

Дедуп по локальной дате в памяти процесса (для 2 пользователей и одного инстанса
достаточно; при горизонтальном масштабировании вынести в Redis/БД).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import date

from aiogram import Bot
from aiogram.types import InputRichMessage

from app.bot.ui import gather, screens
from app.container import Container
from app.domain.timeframe import reminder_due

logger = logging.getLogger(__name__)


async def run_reminder_loop(
    bot: Bot,
    container: Container,
    *,
    hour: int = 23,
    minute: int = 59,
    interval_sec: int = 60,  # окно до полуночи — минута; 10-минутный тик промахнётся
) -> None:
    sent: dict[int, date] = {}
    while True:
        try:
            await _tick(bot, container, hour, minute, sent)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reminder tick failed")
        await asyncio.sleep(interval_sec)


async def _tick(
    bot: Bot, container: Container, hour: int, minute: int, sent: dict[int, date]
) -> None:
    async with container.db.session_factory() as session:
        assistant, users = container.assistant_for(session)
        now = container.clock()
        for user in await users.list_active():
            due, today = reminder_due(
                now, user.timezone, hour, sent.get(user.telegram_id), minute
            )
            if not due:
                continue
            ctx = await gather(assistant, user)
            if ctx.today.meals_count == 0:
                continue  # нечего подытоживать — не дёргаем
            sent[user.telegram_id] = today
            with suppress(Exception):  # вечерний итог единым Rich-стилем, как экран «Итог дня»
                await bot.send_rich_message(
                    chat_id=user.telegram_id,
                    rich_message=InputRichMessage(
                        html=screens.evening(ctx), skip_entity_detection=True
                    ),
                )
        await session.commit()
