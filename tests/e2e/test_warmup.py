"""Прогрев api (MA-INV-77): идёт путём первого экрана, ничего не пишет, не бросает."""
from __future__ import annotations

import logging

from app.web.warmup import CONNECTIONS, warm_up


def _spy(obj, name, calls):
    """Обернуть метод фейка: прогрев обязан реально читать репозитории, а не
    пройти мимо них — тогда греть было бы нечего."""
    real = getattr(obj, name)

    async def wrapped(*args, **kwargs):
        calls.append(name)
        return await real(*args, **kwargs)

    setattr(obj, name, wrapped)


async def test_прогрев_проходит_путь_первого_экрана(fake_container, caplog):
    users = fake_container.users
    await users.get_or_create(telegram_id=1, name="А", locale="ru", timezone="Europe/Moscow")
    await users.get_or_create(telegram_id=2, name="Б", locale="ru", timezone="Etc/GMT-7")
    calls: list[str] = []
    _spy(users, "get_goal", calls)                       # цель — и в настройках, и в дне
    _spy(fake_container.meals, "list_between", calls)    # лента дня
    _spy(fake_container.weights, "history", calls)       # вес под белком
    with caplog.at_level(logging.INFO, logger="app.web.warmup"):
        await warm_up(fake_container, delay=0)
    assert f"прогрев api: {CONNECTIONS} соед. × 2 польз." in caplog.text
    for name in ("get_goal", "list_between", "history"):
        # каждый из соединений прошёл путь каждого пользователя
        assert calls.count(name) >= CONNECTIONS * 2, (name, calls.count(name))
    session = fake_container.db.session
    assert session.rollbacks == CONNECTIONS and session.commits == 0  # ничего не фиксирует


async def test_сбой_прогрева_не_бросает_и_повторяется_раз(fake_container, caplog):
    """Упавший прогрев — медленный первый запрос, как до него. Не упавший старт.
    Один повтор — на случай, если база ещё под миграцией бота."""
    tries = []

    def boom(session):
        tries.append(1)
        raise RuntimeError("база не поднялась")

    fake_container.assistant_for = boom
    with caplog.at_level(logging.WARNING, logger="app.web.warmup"):
        await warm_up(fake_container, delay=0, retry_after=0)   # не бросает
    assert "попытка 1" in caplog.text and "попытка 2" in caplog.text
    assert len(tries) == 2 * CONNECTIONS
