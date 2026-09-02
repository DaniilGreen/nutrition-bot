"""Аналитика (§8.6, MA-M5): ряд по дням и вес."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.application.entities import Meal, MealItem, WeightLog
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


def _meal(uid: int, days_ago: int, kcal: float, protein: float) -> Meal:
    m = Macros(kcal=kcal, protein=protein, fat=kcal * 0.02, carb=0)
    return Meal(
        user_id=uid,
        eaten_at=datetime.now(UTC) - timedelta(days=days_ago, hours=2),
        input_type="text", raw_input="еда",
        items=[MealItem(name_snapshot="еда", grams=100, macros=m)],
        source_confidence=0.9, totals=m,
    )


async def _login(c) -> int:
    return int((await c.get("/api/me", headers=_hdr())).json()["user"]["id"])


async def test_ряд_отдаёт_ровно_запрошенное_число_дней(client):
    """Пустые дни обязаны быть в ряду: дыра в графике это тоже факт."""
    async with client as c:
        await _login(c)
        r = (await c.get("/api/stats/series?days=7", headers=_hdr())).json()
    assert len(r["days"]) == 7
    assert all(d["kcal"] == 0 for d in r["days"])


async def test_ряд_идёт_старые_к_новым(client, fake_container):
    async with client as c:
        uid = await _login(c)
        await fake_container.meals.add(_meal(uid, 2, 1000, 50))
        await fake_container.meals.add(_meal(uid, 0, 2000, 100))
        r = (await c.get("/api/stats/series?days=3", headers=_hdr())).json()
    days = r["days"]
    assert days[0]["date"] < days[-1]["date"]
    assert days[0]["kcal"] == 1000
    assert days[-1]["kcal"] == 2000


async def test_в_ряду_есть_белок_и_число_приёмов(client, fake_container):
    """Главный график — белок по дням, без него ряд бесполезен (§6.9)."""
    async with client as c:
        uid = await _login(c)
        await fake_container.meals.add(_meal(uid, 0, 500, 40))
        await fake_container.meals.add(_meal(uid, 0, 300, 20))
        r = (await c.get("/api/stats/series?days=1", headers=_hdr())).json()
    today = r["days"][-1]
    assert today["protein_g"] == 60
    assert today["meals"] == 2


async def test_чужие_дни_не_попадают(client, fake_container):
    async with client as c:
        await _login(c)
        await fake_container.meals.add(_meal(999, 0, 5000, 200))
        r = (await c.get("/api/stats/series?days=2", headers=_hdr())).json()
    assert all(d["kcal"] == 0 for d in r["days"])


@pytest.mark.parametrize("days", [0, -1, 366, 9999])
async def test_негодный_период_400(client, days):
    async with client as c:
        await _login(c)
        r = await c.get(f"/api/stats/series?days={days}", headers=_hdr())
    assert r.status_code == 400


async def test_вес_отдаётся_по_возрастанию_даты(client, fake_container):
    async with client as c:
        uid = await _login(c)
        for d, kg in ((10, 86.8), (5, 89.0), (0, 91.5)):
            await fake_container.weights.add(
                WeightLog(
                    user_id=uid,
                    measured_at=datetime.now(UTC) - timedelta(days=d),
                    weight_kg=kg,
                )
            )
        r = (await c.get("/api/stats/weight", headers=_hdr())).json()
    assert [p["kg"] for p in r["points"]] == [86.8, 89.0, 91.5]


async def test_мусорный_вес_отсекается(client, fake_container):
    """Опечатка «915» вместо 91.5 не должна ломать масштаб графика."""
    async with client as c:
        uid = await _login(c)
        for kg in (91.5, 915.0):
            await fake_container.weights.add(
                WeightLog(user_id=uid, measured_at=datetime.now(UTC), weight_kg=kg)
            )
        r = (await c.get("/api/stats/weight", headers=_hdr())).json()
    assert [p["kg"] for p in r["points"]] == [91.5]


async def test_аналитика_требует_подписи(client):
    async with client as c:
        assert (await c.get("/api/stats/series")).status_code == 401
        assert (await c.get("/api/stats/weight")).status_code == 401
