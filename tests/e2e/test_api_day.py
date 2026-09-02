"""Экран «День» на чтение (§8.2, MA-M1): панель остатка и лента приёмов."""
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

    app.state.container = fake_container
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _meal(user_id: int, *, hours_ago: float, name: str, grams: float,
          kcal: float, estimate: bool = False) -> Meal:
    m = Macros(kcal=kcal, protein=kcal / 20, fat=kcal / 40, carb=kcal / 10)
    return Meal(
        user_id=user_id,
        eaten_at=datetime.now(UTC) - timedelta(hours=hours_ago),
        input_type="text",
        raw_input=name,
        items=[MealItem(name_snapshot=name, grams=grams, macros=m, is_estimate=estimate)],
        source_confidence=0.9,
        totals=m,
    )


async def _login(c) -> int:
    r = await c.get("/api/me", headers={"X-Telegram-Init-Data": valid()})
    return int(r.json()["user"]["id"])


async def test_пустой_день(client):
    async with client as c:
        await _login(c)
        r = await c.get("/api/day", headers={"X-Telegram-Init-Data": valid()})
    assert r.status_code == 200
    d = r.json()
    assert d["meals"] == []
    assert d["totals"]["kcal"] == 0
    # Ничего не съедено — остаток равен цели целиком.
    assert d["totals"]["kcal_left"] == d["goal"]["kcal"]


async def test_день_с_приёмами(client, fake_container):
    async with client as c:
        uid = await _login(c)
        await fake_container.meals.add(_meal(uid, hours_ago=2, name="грудка", grams=200, kcal=330))
        await fake_container.meals.add(_meal(uid, hours_ago=1, name="рис", grams=190, kcal=248))
        r = await c.get("/api/day", headers={"X-Telegram-Init-Data": valid()})
    d = r.json()
    assert len(d["meals"]) == 2
    assert d["totals"]["kcal"] == 578
    assert d["totals"]["kcal_left"] == round(d["goal"]["kcal"] - 578, 1)


async def test_граммовка_видна_в_позиции(client, fake_container):
    """§6.2: без граммов приём нечем повторить и не с чем сверить."""
    async with client as c:
        uid = await _login(c)
        await fake_container.meals.add(_meal(uid, hours_ago=1, name="грудка", grams=200, kcal=330))
        r = await c.get("/api/day", headers={"X-Telegram-Init-Data": valid()})
    item = r.json()["meals"][0]["items"][0]
    assert item["grams"] == 200
    assert item["name"] == "грудка"


async def test_метка_оценки_доезжает_до_клиента(client, fake_container):
    """INV-13: «≈» нельзя потерять по дороге — иначе оценка выглядит как факт."""
    async with client as c:
        uid = await _login(c)
        await fake_container.meals.add(
            _meal(uid, hours_ago=1, name="борщ", grams=350, kcal=210, estimate=True)
        )
        r = await c.get("/api/day", headers={"X-Telegram-Init-Data": valid()})
    meal = r.json()["meals"][0]
    assert meal["estimated"] is True
    assert meal["items"][0]["is_estimate"] is True


async def test_чужой_день_не_виден(client, fake_container):
    """Приём другого пользователя не должен попасть в ленту."""
    async with client as c:
        await _login(c)
        await fake_container.meals.add(_meal(999, hours_ago=1, name="чужое", grams=100, kcal=500))
        r = await c.get("/api/day", headers={"X-Telegram-Init-Data": valid()})
    assert r.json()["meals"] == []
    assert r.json()["totals"]["kcal"] == 0


@pytest.mark.parametrize("bad", [-1, -100, 366, 10000])
async def test_день_вне_диапазона_400(client, bad):
    """Отрицательный days_ago показал бы «завтра» пустым и молча соврал."""
    async with client as c:
        await _login(c)
        r = await c.get(f"/api/day?days_ago={bad}", headers={"X-Telegram-Init-Data": valid()})
    assert r.status_code == 400
    assert r.json()["detail"]["error"]["code"] == "bad_days_ago"


async def test_день_требует_подписи(client):
    async with client as c:
        r = await c.get("/api/day")
    assert r.status_code == 401
