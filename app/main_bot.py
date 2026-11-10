"""Entrypoint бота (aiogram, long-polling) + фоновая задача напоминаний."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import router
from app.bot.menu import setup_bot_commands, setup_miniapp_button
from app.bot.middlewares import ServicesMiddleware, WidgetQueryMiddleware
from app.bot.reminders import run_reminder_loop
from app.config import get_settings
from app.container import Container, check_provider
from app.infrastructure.products.seed import seed_curated
from app.logging import setup_logging


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN не задан")

    container = Container.create(settings)
    try:
        await check_provider(settings)
    except Exception:
        # AC-264: без polling деплой краснеет — чинить ключ/модель, а не ловить «недоступна».
        logging.getLogger(__name__).exception(
            "провайдер модели не принял ключ или модель — бот не стартует"
        )
        await container.db.dispose()
        raise SystemExit(1) from None
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp["container"] = container  # error-handler шлёт владельцу алерт о балансе (NFR-4)
    middleware = ServicesMiddleware(container)
    dp.message.middleware(middleware)
    dp.callback_query.middleware(middleware)
    # FR-49, D-45: апдейт 10.4 `widget_query` aiogram не знает — перехват до разбора типа.
    dp.update.outer_middleware(WidgetQueryMiddleware(container))
    dp.include_router(router)

    await setup_bot_commands(bot)
    await setup_miniapp_button(bot, settings.miniapp_url)
    await seed_curated(container.db.session_factory)

    reminder_task: asyncio.Task[None] | None = None
    if settings.reminder_enabled:
        reminder_task = asyncio.create_task(
            run_reminder_loop(
                bot, container, hour=settings.reminder_hour, minute=settings.reminder_minute
            )
        )

    try:
        # allowed_updates явно: по умолчанию aiogram просит только типы своих хендлеров, и
        # Telegram не прислал бы `widget_query` вовсе (FR-49).
        await dp.start_polling(
            bot, allowed_updates=[*dp.resolve_used_update_types(), "widget_query"]
        )
    finally:
        if reminder_task is not None:
            reminder_task.cancel()
            with suppress(asyncio.CancelledError):
                await reminder_task
        await container.db.dispose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
