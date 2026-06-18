"""Entrypoint бота (aiogram, long-polling) + фоновая задача напоминаний."""
from __future__ import annotations

import asyncio
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import router
from app.bot.menu import setup_bot_commands
from app.bot.middlewares import ServicesMiddleware
from app.bot.reminders import run_reminder_loop
from app.config import get_settings
from app.container import Container
from app.infrastructure.products.seed import seed_curated
from app.logging import setup_logging


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN не задан")

    container = Container.create(settings)
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    middleware = ServicesMiddleware(container)
    dp.message.middleware(middleware)
    dp.callback_query.middleware(middleware)
    dp.include_router(router)

    await setup_bot_commands(bot)
    await seed_curated(container.db.session_factory)

    reminder_task: asyncio.Task[None] | None = None
    if settings.reminder_enabled:
        reminder_task = asyncio.create_task(
            run_reminder_loop(bot, container, hour=settings.reminder_hour)
        )

    try:
        await dp.start_polling(bot)
    finally:
        if reminder_task is not None:
            reminder_task.cancel()
            with suppress(asyncio.CancelledError):
                await reminder_task
        await container.db.dispose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
