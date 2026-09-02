"""Зависимости FastAPI: одна дверь, через которую входит любой запрос аппки.

MA-INV-6: без валидной initData не работает ни один endpoint, кроме /health.
MA-INV-7: пользователь берётся только из проверенной подписи.
MA-INV-8: проверка живёт здесь, на границе web; application про Telegram не знает.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.assistant import Assistant
from app.application.entities import User
from app.container import Container
from app.web.auth import InitDataError, verify_init_data
from app.web.errors import forbidden, unauthorized

log = logging.getLogger(__name__)


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


async def get_session(
    container: Annotated[Container, Depends(get_container)],
) -> AsyncIterator[AsyncSession]:
    """Сессия на запрос, с фиксацией результата.

    Без commit `flush` выдаёт id, ответ выглядит успешным, а на выходе из
    контекста транзакция откатывается — запись исчезает молча. Бот делает
    ровно это в `app/bot/middlewares.py:42`; у аппки должно быть так же.
    """
    async with container.db.session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


async def get_assistant(
    container: Annotated[Container, Depends(get_container)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Assistant:
    """Единственный вход в application из web (§8.9). Обработчик, который лезет
    в Container сам, нетестируем — это признак нарушения MA-INV-5."""
    assistant, _ = container.assistant_for(session)
    return assistant


async def current_user(
    container: Annotated[Container, Depends(get_container)],
    session: Annotated[AsyncSession, Depends(get_session)],
    init_data: Annotated[str | None, Header(alias="X-Telegram-Init-Data")] = None,
) -> User:
    """Пользователь запроса. Заголовок, а не cookie и не query: строка не должна
    попадать в URL — оттуда она уезжает в логи, историю и Referer (§7.1)."""
    if not init_data:
        raise unauthorized("no_init_data")
    try:
        tg = verify_init_data(init_data, container.settings.bot_token)
    except InitDataError as exc:
        # В лог — код и хвост хэша: без них разбирать 401 в проде нечем, а целиком
        # хэш класть нельзя, это половина подписи.
        log.warning("initData отклонена: %s (hash…%s)", exc, init_data[-16:])
        raise unauthorized(_code_for(exc)) from exc

    _, users = container.assistant_for(session)
    user = await users.get_or_create(
        telegram_id=tg.telegram_id,
        name=tg.full_name,
        locale=tg.language_code or "ru",
        timezone=container.settings.default_timezone,
    )
    if not user.is_active:
        raise forbidden("inactive", "Доступ отключён.")
    return user


def _code_for(exc: InitDataError) -> str:
    text = str(exc)
    if "просрочена" in text or "будущего" in text:
        return "init_data_expired"
    if "user" in text:
        return "no_user"
    return "bad_signature"


CurrentUser = Annotated[User, Depends(current_user)]
CurrentAssistant = Annotated[Assistant, Depends(get_assistant)]
