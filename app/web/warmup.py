"""Прогрев api после старта (MA-INV-77).

24.10.2026 после деплоя первые запросы шли 3,4 с (/api/settings) и 6,6 с (/api/day),
повторы — 9–31 мс. Ленивых импортов на этом пути нет; платит первый запрос за то,
что движок откладывает до первого обращения:

- соединение с Postgres: движок создаётся без него, первый запрос платит TCP,
  SCRAM-аутентификацию, форк бэкенда на стороне базы и разведку типов asyncpg;
- SQLAlchemy на первом запросе настраивает мапперы и компилирует каждый оператор
  (кэш компиляции общий на движок, но пустой); у /api/day их полтора десятка;
- asyncpg готовит каждый оператор на соединении — ещё по обороту до базы;
- и всё это на 1 ГБ RAM со свопом: холодные страницы процесса и базы.

Поэтому греем не «SELECT 1», а тот же путь, что у первого экрана: те же обработчики,
те же операторы, для каждого живого пользователя. Транзакция откатывается — прогрев
ничего не пишет.

Соединений греем четыре: аппка на старте шлёт четыре запроса разом (MA-INV-75), и
каждому нужно своё соединение с уже подготовленными операторами. Старт — через
5 с и с одним повтором: на том же деплое контейнер бота гоняет `alembic upgrade`, и
прогрев не должен бежать наперегонки с миграцией.
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Any

from app.web.routes import day, settings

log = logging.getLogger(__name__)

CONNECTIONS = 4


async def _warm_one(container: Any) -> int:
    """Один прогрев — одна сессия, то есть своё соединение из пула."""
    async with container.db.session_factory() as session:
        _, users = container.assistant_for(session)
        people = await users.list_active()
        # Обработчику настроек нужен только request.app.state.container.
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            container=container)))
        for user in people:
            # Тот же оператор, что у current_user на каждом запросе.
            await users.get_or_create(
                telegram_id=user.telegram_id, name=user.name,
                locale=user.locale, timezone=user.timezone,
            )
            assistant, _ = container.assistant_for(session)
            await settings(user, assistant, request, session)  # type: ignore[arg-type]
            await day(user, assistant, container.body_scans_for(session), 0)
        await session.rollback()
        return len(people)


async def warm_up(container: Any, delay: float = 5.0, retry_after: float = 10.0) -> None:
    """Пройти путь /api/settings и /api/day на CONNECTIONS соединениях разом.

    Best-effort: не бросает никогда (кроме отмены на остановке). Упавший прогрев —
    это медленный первый запрос, как до него; упавший старт api — мёртвая аппка.
    """
    await asyncio.sleep(delay)
    for attempt in (1, 2):
        started = time.monotonic()
        results = await asyncio.gather(
            *(_warm_one(container) for _ in range(CONNECTIONS)), return_exceptions=True
        )
        ms = (time.monotonic() - started) * 1000
        errors = [r for r in results if isinstance(r, BaseException)]
        if not errors:
            log.info("прогрев api: %d соед. × %s польз. за %.0f мс",
                     CONNECTIONS, results[0], ms)
            return
        log.warning("прогрев api не удался (попытка %d) за %.0f мс — первый запрос "
                    "будет медленным", attempt, ms, exc_info=errors[0])
        if attempt == 1:
            await asyncio.sleep(retry_after)
