"""Entrypoint бота (aiogram, long-polling)."""
from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import router
from app.bot.menu import setup_bot_commands
from app.bot.middlewares import ServicesMiddleware
from app.config import get_settings
from app.container import Container
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
    # MemoryStorage — для 2 пользователей достаточно; FSM-черновик уточнения живёт
    # в памяти процесса. Для масштабирования — заменить на Redis-storage.
    dp = Dispatcher(storage=MemoryStorage())
    middleware = ServicesMiddleware(container)
    dp.message.middleware(middleware)
    dp.callback_query.middleware(middleware)
    dp.include_router(router)

    await setup_bot_commands(bot)

    try:
        await dp.start_polling(bot)
    finally:
        await container.db.dispose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
