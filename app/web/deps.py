"""Зависимости FastAPI: одна дверь, через которую входит любой запрос аппки.

MA-INV-6: без валидной initData не работает ни один endpoint, кроме /health.
MA-INV-7: пользователь берётся только из проверенной подписи.
MA-INV-8: проверка живёт здесь, на границе web; application про Telegram не знает.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.entities import User
from app.container import Container
from app.web.auth import InitDataError, verify_init_data


def get_container(request: Request) -> Container:
    return request.app.state.container


async def get_session(
    container: Annotated[Container, Depends(get_container)],
) -> AsyncIterator[AsyncSession]:
    async with container.db.session_factory() as session:
        yield session


async def current_user(
    container: Annotated[Container, Depends(get_container)],
    session: Annotated[AsyncSession, Depends(get_session)],
    init_data: Annotated[str | None, Header(alias="X-Telegram-Init-Data")] = None,
) -> User:
    """Пользователь запроса. Заголовок, а не cookie и не query: строка не должна
    попадать в URL — оттуда она уезжает в логи, историю и Referer (§7.1)."""
    try:
        tg = verify_init_data(init_data or "", container.settings.bot_token)
    except InitDataError as exc:
        # Наружу причина не уходит: она подсказывала бы, как подобрать подпись.
        raise HTTPException(status_code=401, detail="unauthorized") from exc

    _, users = container.assistant_for(session)
    return await users.get_or_create(
        telegram_id=tg.telegram_id,
        name=tg.first_name or tg.username,
        locale=tg.language_code or "ru",
        timezone="Europe/Moscow",
    )


CurrentUser = Annotated[User, Depends(current_user)]
