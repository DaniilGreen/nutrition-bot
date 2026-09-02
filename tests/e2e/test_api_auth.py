"""API мини-аппа: дверь заперта, и открывается только подписью (§7.1).

Через ASGITransport, без сети и без БД: контейнер подменён фейком.
"""
from __future__ import annotations

import httpx
import pytest

from tests.unit.test_initdata import TOKEN, valid


@pytest.fixture
def client(fake_container):
    from app.main_api import app

    app.state.container = fake_container
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_health_открыт_без_подписи(client):
    """Единственное исключение из MA-INV-6: его дёргает деплой."""
    async with client as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_me_без_заголовка_401(client):
    async with client as c:
        r = await c.get("/api/me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_me_с_битой_подписью_401(client):
    async with client as c:
        r = await c.get("/api/me", headers={"X-Telegram-Init-Data": "hash=deadbeef"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_me_причина_отказа_не_раскрывается(client):
    """Текст ошибки не должен подсказывать, чем именно подпись плоха."""
    async with client as c:
        r = await c.get("/api/me", headers={"X-Telegram-Init-Data": "hash=deadbeef"})
    assert r.json() == {"detail": "unauthorized"}


@pytest.mark.asyncio
async def test_me_с_подлинной_подписью(client):
    async with client as c:
        r = await c.get("/api/me", headers={"X-Telegram-Init-Data": valid()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["id"] is not None
    assert body["goal"]["kcal"] > 0
    assert body["goal"]["is_default"] is True


@pytest.mark.asyncio
async def test_пользователь_из_подписи_а_не_из_запроса(client):
    """MA-INV-7: telegram_id 777 зашит в подписанной строке; тело не влияет."""
    async with client as c:
        r = await c.get(
            "/api/me?telegram_id=1", headers={"X-Telegram-Init-Data": valid()}
        )
    assert r.status_code == 200
    assert r.json()["user"]["name"] == "Даниил"
