"""Флагманские панели §4.0/§3.3/§3.5: сверка с весами, вклады, точность."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.application.entities import Meal, MealItem, WeightLog
from app.domain.value_objects import Macros
from tests.initdata_fixtures import valid


@pytest.fixture
def client(fake_container):
    from app.main_api import app
    from app.web.drafts import DraftStore
    from app.web.limits import IdempotencyStore, RateLimiter

    app.state.container = fake_container
    app.state.drafts = DraftStore()
    app.state.idempotency = IdempotencyStore()
    app.state.limiter = RateLimiter()
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _hdr():
    return {"X-Telegram-Init-Data": valid()}


def _noon(days_ago: int = 0) -> datetime:
    tz = ZoneInfo("Europe/Moscow")
    d = (datetime.now(tz) - timedelta(days=days_ago)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    return d.astimezone(UTC)


def _meal(uid: int, days_ago: int, kcal: float, name="еда", estimate=False) -> Meal:
    m = Macros(kcal=kcal, protein=kcal * 0.2, fat=kcal * 0.02, carb=0)
    return Meal(
        user_id=uid, eaten_at=_noon(days_ago), input_type="text", raw_input=name,
        items=[MealItem(name_snapshot=name, grams=100, macros=m, is_estimate=estimate)],
        source_confidence=0.9, totals=m,
    )


async def _login(c) -> int:
    return int((await c.get("/api/me", headers=_hdr())).json()["user"]["id"])


async def test_без_весов_сверка_честно_говорит_что_нечем(client, fake_container):
    """Притворяться, что вывод есть, хуже, чем сказать «замеров нет»."""
    async with client as c:
        uid = await _login(c)
        for d in range(30):
            await fake_container.meals.add(_meal(uid, d, 1400))
        r = (await c.get("/api/stats/reconcile?days=30", headers=_hdr())).json()
    assert r["verdict"] == "no_weight"


async def test_короткое_окно_вывода_не_даёт(client, fake_container):
    """На нескольких днях вес говорит про воду, а не про еду."""
    async with client as c:
        uid = await _login(c)
        for d in range(7):
            await fake_container.meals.add(_meal(uid, d, 1400))
        for d, kg in ((6, 90.0), (0, 91.0)):
            await fake_container.weights.add(
                WeightLog(user_id=uid, measured_at=_noon(d), weight_kg=kg)
            )
        r = (await c.get("/api/stats/reconcile?days=7", headers=_hdr())).json()
    assert r["verdict"] == "not_enough_data"


async def test_реальный_случай_владельца_показывает_разрыв(client, fake_container):
    """Дневник 1429 ккал/день при весе 86,8 → 91,5. Разрыв обязан быть виден."""
    async with client as c:
        uid = await _login(c)
        for d in range(28):
            await fake_container.meals.add(_meal(uid, d, 1429))
        for d, kg in ((29, 86.8), (0, 91.5)):
            await fake_container.weights.add(
                WeightLog(user_id=uid, measured_at=_noon(d), weight_kg=kg)
            )
        r = (await c.get("/api/stats/reconcile?days=30", headers=_hdr())).json()
    assert r["verdict"] == "enough_data_gap"
    assert r["agrees"] is False
    assert r["gap_kcal_per_day"] > 500
    assert r["weight_delta_kg"] == 4.7


async def test_когда_сходится_так_и_говорим(client, fake_container):
    async with client as c:
        uid = await _login(c)
        for d in range(30):
            await fake_container.meals.add(_meal(uid, d, 2000))
        for d, kg in ((29, 90.0), (0, 90.1)):
            await fake_container.weights.add(
                WeightLog(user_id=uid, measured_at=_noon(d), weight_kg=kg)
            )
        r = (await c.get("/api/stats/reconcile?days=30", headers=_hdr())).json()
    assert r["agrees"] is True


async def test_вклады_считаются_в_калориях_а_не_в_долях(client, fake_container):
    """Доли нормируются на съеденное и прячут проблему (§4.1)."""
    async with client as c:
        uid = await _login(c)
        await fake_container.meals.add(_meal(uid, 1, 900, name="пицца"))
        await fake_container.meals.add(_meal(uid, 2, 100, name="огурец"))
        r = (await c.get("/api/stats/contributions", headers=_hdr())).json()
    assert [i["name"] for i in r["items"]] == ["пицца", "огурец"]
    assert r["items"][0]["kcal"] == 900
    assert r["total_kcal"] == 1000


async def test_точность_считается_по_калориям(client, fake_container):
    """Одна оценённая тарелка весит больше десяти точных специй."""
    async with client as c:
        uid = await _login(c)
        await fake_container.meals.add(_meal(uid, 1, 800, name="борщ", estimate=True))
        await fake_container.meals.add(_meal(uid, 1, 200, name="хлеб"))
        r = (await c.get("/api/stats/accuracy", headers=_hdr())).json()
    assert r["estimated_kcal"] == 800
    assert r["total_kcal"] == 1000
    assert r["estimated_share"] == 0.8


async def test_пустая_точность_не_делит_на_ноль(client):
    async with client as c:
        await _login(c)
        r = (await c.get("/api/stats/accuracy", headers=_hdr())).json()
    assert r["estimated_share"] == 0.0


async def test_чужие_данные_не_попадают(client, fake_container):
    async with client as c:
        await _login(c)
        await fake_container.meals.add(_meal(999, 1, 5000, name="чужое"))
        contrib = (await c.get("/api/stats/contributions", headers=_hdr())).json()
        acc = (await c.get("/api/stats/accuracy", headers=_hdr())).json()
    assert contrib["items"] == []
    assert acc["total_kcal"] == 0


async def test_панели_требуют_подписи(client):
    async with client as c:
        for path in ("/api/stats/reconcile", "/api/stats/contributions", "/api/stats/accuracy"):
            assert (await c.get(path)).status_code == 401, path
