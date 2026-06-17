"""Entrypoint бота (aiogram, long-polling)."""
from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot.handlers import router
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
    dp = Dispatcher()
    dp.message.middleware(ServicesMiddleware(container))
    dp.include_router(router)

    try:
        await dp.start_polling(bot)
    finally:
        await container.db.dispose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
