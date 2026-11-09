"""Middleware: сессия БД на апдейт + резолв пользователя + инъекция Assistant."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from aiogram.types import User as TgUser

from app.application.entities import User
from app.bot.widgets import live
from app.container import Container

logger = logging.getLogger(__name__)


class ServicesMiddleware(BaseMiddleware):
    def __init__(self, container: Container) -> None:
        self._container = container

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        async with self._container.db.session_factory() as session:
            assistant, users = self._container.assistant_for(session)
            user = await users.get_or_create(
                telegram_id=tg_user.id,
                name=tg_user.full_name,
                locale=tg_user.language_code or "ru",
                timezone=self._container.settings.default_timezone,
            )
            data["assistant"] = assistant
            data["scans"] = self._container.body_scans_for(session)
            data["users"] = users
            data["app_user"] = user
            data["session"] = session
            try:
                result = await handler(event, data)
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        if live.watching(user) and live.enabled(user):
            await self._push(data, user)
        return result

    async def _push(self, data: dict[str, Any], user: User) -> None:
        """FR-49.1, D-46: после апдейта — виджет дня показывает то, что теперь в базе. Своя
        сессия: хендлер уже закоммитил. Сбой здесь не должен ронять ответ человеку."""
        bot = data.get("bot")
        if bot is None:
            return
        try:
            async with self._container.db.session_factory() as session:
                assistant, _ = self._container.assistant_for(session)
                await live.push(bot, user, assistant)
        except Exception:  # noqa: BLE001
            logger.exception("widget: пуш виджета дня упал")
