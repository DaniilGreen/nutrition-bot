"""Middleware: сессия БД на апдейт + резолв пользователя + инъекция Assistant."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from aiogram.types import User as TgUser

from app.container import Container


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
                return result
            except Exception:
                await session.rollback()
                raise
