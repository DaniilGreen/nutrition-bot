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
from aiogram.types import BufferedInputFile

from app.bot.dashboard_image import render_today
from app.bot.formatters import today_photo_caption
from app.container import Container
from app.domain.timeframe import reminder_due

logger = logging.getLogger(__name__)


async def run_reminder_loop(
    bot: Bot, container: Container, *, hour: int = 21, interval_sec: int = 600
) -> None:
    sent: dict[int, date] = {}
    while True:
        try:
            await _tick(bot, container, hour, sent)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reminder tick failed")
        await asyncio.sleep(interval_sec)


async def _tick(bot: Bot, container: Container, hour: int, sent: dict[int, date]) -> None:
    async with container.db.session_factory() as session:
        assistant, users = container.assistant_for(session)
        now = container.clock()
        for user in await users.list_active():
            due, today = reminder_due(now, user.timezone, hour, sent.get(user.telegram_id))
            if not due:
                continue
            stats = await assistant.today(user)
            if stats.meals_count == 0:
                continue  # нечего подытоживать — не дёргаем
            sent[user.telegram_id] = today
            png = render_today(stats.remaining)  # бары+кольцо картинкой, как 📊 Сегодня
            caption = "🌙 Итог дня\n\n" + today_photo_caption(stats)
            with suppress(Exception):
                if len(caption) <= 1024:  # лимит подписи Telegram
                    await bot.send_photo(
                        user.telegram_id, BufferedInputFile(png, "today.png"), caption=caption
                    )
                else:  # длинный список — фото с краткой подписью + список отдельным сообщением
                    await bot.send_photo(
                        user.telegram_id, BufferedInputFile(png, "today.png"), caption="🌙 Итог дня"
                    )
                    await bot.send_message(user.telegram_id, today_photo_caption(stats))
        await session.commit()
