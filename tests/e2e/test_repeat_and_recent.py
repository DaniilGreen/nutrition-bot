"""«Ещё раз» и «Недавние» (§6.25, MA-INV-67, D-MA-18).

Кейс владельца 13.10.2026: вчера хлеб записан по этикетке, сегодня этикетки нет — повторить
его в сегодня, одну позицию, а не весь приём и не во вчера.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.application.entities import Meal, MealItem, Product
from app.domain.value_objects import Density, Macros
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
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _hdr():
    return {"X-Telegram-Init-Data": valid(), "Idempotency-Key": uuid4().hex}


def _noon(days_ago: int) -> datetime:
    tz = ZoneInfo("Europe/Moscow")
    day = (datetime.now(tz) - timedelta(days=days_ago)).replace(hour=12, minute=0, second=0,
                                                                 microsecond=0)
    return day.astimezone(UTC)


async def _product(repo, key: str, name: str, kcal: float) -> int:
    p = await repo.create(Product(canonical_key=key, display_name=name,
                                  density=Density(kcal, 7, 4, 50), source="label"))
    return p.id


def _meal(uid: int, days_ago: int, *items, input_type: str = "label") -> Meal:
    ms = [MealItem(name_snapshot=nm, grams=g, product_id=pid,
                   macros=Macros(kcal=g * 2.6, protein=g * 0.07, fat=g * 0.04, carb=g * 0.5))
          for pid, nm, g in items]
    total = Macros(sum(i.macros.kcal for i in ms), sum(i.macros.protein for i in ms),
                   sum(i.macros.fat for i in ms), sum(i.macros.carb for i in ms))
    return Meal(user_id=uid, eaten_at=_noon(days_ago), input_type=input_type,
                raw_input="хлеб и йогурт", items=ms, source_confidence=0.9, totals=total)


async def _login(c) -> int:
    return int((await c.get("/api/me", headers=_hdr())).json()["user"]["id"])


async def test_вчерашняя_позиция_повторяется_в_сегодня_одна(client, fake_container):
    async with client as c:
        uid = await _login(c)
        meal = await fake_container.meals.add(
            _meal(uid, 1, (None, "хлеб с изюмом", 84), (None, "йогурт", 60)))
        bread = meal.items[0].id
        r = await c.post(f"/api/meals/{meal.id}/items/{bread}/repeat", json={},
                         headers=_hdr())
        assert r.status_code == 200, r.text
        d = r.json()
        today = (await c.get("/api/day?days_ago=0", headers=_hdr())).json()
    assert [i["name"] for i in d["meal"]["items"]] == ["хлеб с изюмом"]
    assert d["meal"]["kcal"] == pytest.approx(84 * 2.6, abs=0.1)
    # MA-INV-67: снимок, а не каталог; «· этикетка» сохраняется
    assert d["meal"]["input_type"] == "label"
    assert any(m["id"] == d["meal"]["id"] for m in today["meals"])


async def test_другая_граммовка_масштабирует_снимок(client, fake_container):
    async with client as c:
        uid = await _login(c)
        meal = await fake_container.meals.add(_meal(uid, 1, (None, "хлеб с изюмом", 84)))
        r = await c.post(f"/api/meals/{meal.id}/items/{meal.items[0].id}/repeat",
                         json={"grams": 42}, headers=_hdr())
    item = r.json()["meal"]["items"][0]
    assert item["grams"] == 42
    assert r.json()["meal"]["kcal"] == pytest.approx(42 * 2.6, abs=0.1)


async def test_можно_повторить_в_тот_же_прошлый_день(client, fake_container):
    async with client as c:
        uid = await _login(c)
        meal = await fake_container.meals.add(_meal(uid, 1, (None, "пиво", 500)))
        r = await c.post(f"/api/meals/{meal.id}/items/{meal.items[0].id}/repeat",
                         json={"days_ago": 1}, headers=_hdr())
        yesterday = (await c.get("/api/day?days_ago=1", headers=_hdr())).json()
    assert sum(1 for m in yesterday["meals"] if m["id"] == r.json()["meal"]["id"]) == 1


async def test_повтор_позиции_ошибки(client, fake_container):
    async with client as c:
        uid = await _login(c)
        meal = await fake_container.meals.add(_meal(uid, 1, (None, "хлеб", 84)))
        other = await fake_container.meals.add(_meal(uid + 999, 1, (None, "чужой", 50)))
        gone = await c.post(f"/api/meals/{meal.id}/items/999/repeat", json={}, headers=_hdr())
        alien = await c.post(f"/api/meals/{other.id}/items/{other.items[0].id}/repeat",
                             json={}, headers=_hdr())
        bad = await c.post(f"/api/meals/{meal.id}/items/{meal.items[0].id}/repeat",
                           json={"grams": -5}, headers=_hdr())
    assert gone.status_code == 404
    assert alien.status_code == 404  # чужой приём не повторить
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "bad_grams"


async def test_недавние_по_последнему_разу_и_только_свои(client, fake_container):
    repo = fake_container.products
    bread = await _product(repo, "bread", "Хлеб с изюмом", 260)
    yog = await _product(repo, "yog", "йогурт", 158)
    alien = await _product(repo, "beer", "пиво чужое", 43)
    await _product(repo, "never", "ни разу не ел", 100)
    async with client as c:
        uid = await _login(c)
        await fake_container.meals.add(_meal(uid, 3, (yog, "йогурт", 60)))
        await fake_container.meals.add(_meal(uid, 2, (yog, "йогурт", 60)))
        await fake_container.meals.add(_meal(uid, 1, (bread, "Хлеб с изюмом", 84)))
        await fake_container.meals.add(_meal(uid + 999, 0, (alien, "пиво чужое", 500)))
        recent = (await c.get("/api/products?sort=recent", headers=_hdr())).json()
        frequent = (await c.get("/api/products?sort=frequent", headers=_hdr())).json()
        bad = await c.get("/api/products?sort=random", headers=_hdr())
    assert [p["name"] for p in recent["items"]] == ["Хлеб с изюмом", "йогурт"]
    assert recent["total"] == 2
    first = recent["items"][0]
    assert first["last_days_ago"] == 1 and first["last_grams"] == 84
    assert first["last_time"] == _noon(1).astimezone(ZoneInfo("Europe/Moscow")).strftime("%H:%M")
    assert [p["name"] for p in frequent["items"]] == ["йогурт", "Хлеб с изюмом"]
    assert bad.status_code == 400
