"""Частое и повтор одним тапом (§8.5, MA-M4)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.application.entities import Meal, MealItem
from app.domain.value_objects import Macros
from tests.initdata_fixtures import valid


@pytest.fixture
def client(fake_container):
    from app.main_api import app
    from app.web.drafts import DraftStore

    app.state.container = fake_container
    app.state.drafts = DraftStore()
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _hdr():
    return {"X-Telegram-Init-Data": valid()}


def _meal(uid: int, days_ago: float, *items) -> Meal:
    ms = [
        MealItem(
            name_snapshot=nm, grams=g,
            macros=Macros(kcal=g, protein=g * 0.2, fat=g * 0.02, carb=0),
        )
        for nm, g in items
    ]
    total = Macros(
        kcal=sum(i.macros.kcal for i in ms), protein=sum(i.macros.protein for i in ms),
        fat=sum(i.macros.fat for i in ms), carb=0,
    )
    return Meal(
        user_id=uid,
        eaten_at=datetime.now(UTC) - timedelta(days=days_ago),
        input_type="text", raw_input=", ".join(i.name_snapshot for i in ms),
        items=ms, source_confidence=0.9, totals=total,
    )


async def _login(c) -> int:
    return int((await c.get("/api/me", headers=_hdr())).json()["user"]["id"])


async def test_частое_сортируется_по_числу_повторов(client, fake_container):
    async with client as c:
        uid = await _login(c)
        for i in range(4):
            await fake_container.meals.add(_meal(uid, i, ("грудка", 200)))
        for i in range(2):
            await fake_container.meals.add(_meal(uid, i, ("рис", 190)))
        r = (await c.get("/api/frequent", headers=_hdr())).json()
    assert [x["name"] for x in r["items"]] == ["грудка", "рис"]
    assert r["items"][0]["times"] == 4


async def test_граммовка_это_медиана_а_не_среднее(client, fake_container):
    """Одна ошибка «2000 г» не должна сдвигать подсказку."""
    async with client as c:
        uid = await _login(c)
        for g in (200, 210, 190, 2000, 205):
            await fake_container.meals.add(_meal(uid, 1, ("грудка", g)))
        r = (await c.get("/api/frequent", headers=_hdr())).json()
    assert r["items"][0]["grams"] == 205   # медиана; среднее было бы 561


async def test_старое_не_попадает_в_подсказки(client, fake_container):
    """Рацион меняется — прошлогоднее не подсказка."""
    async with client as c:
        uid = await _login(c)
        await fake_container.meals.add(_meal(uid, 200, ("гречка", 150)))
        await fake_container.meals.add(_meal(uid, 2, ("грудка", 200)))
        r = (await c.get("/api/frequent?days=30", headers=_hdr())).json()
    assert [x["name"] for x in r["items"]] == ["грудка"]


async def test_чужое_частое_не_видно(client, fake_container):
    async with client as c:
        await _login(c)
        await fake_container.meals.add(_meal(999, 1, ("чужое", 100)))
        r = (await c.get("/api/frequent", headers=_hdr())).json()
    assert r["items"] == []


@pytest.mark.parametrize("qs", ["days=0", "days=400", "limit=0", "limit=99"])
async def test_негодные_параметры_400(client, qs):
    async with client as c:
        await _login(c)
        r = await c.get("/api/frequent?" + qs, headers=_hdr())
    assert r.status_code == 400


async def test_повтор_несуществующего_404(client):
    async with client as c:
        await _login(c)
        r = await c.post("/api/meals/9999/repeat", headers=_hdr())
    assert r.status_code == 404


async def test_частое_требует_подписи(client):
    async with client as c:
        assert (await c.get("/api/frequent")).status_code == 401
        assert (await c.post("/api/meals/1/repeat")).status_code == 401
