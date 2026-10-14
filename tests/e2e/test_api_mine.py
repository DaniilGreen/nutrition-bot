"""«Моё» в каталоге (FR-39.5): рецепт виден сразу после сохранения, наборы — с составом.

Регрессия 15.10.2026: рецепт заводился, но в «Продуктах» его не было — «Недавние» и
«Частые» показывают только съеденное, а своего раздела не существовало.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

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


async def _login(c) -> int:
    return int((await c.get("/api/me", headers=_hdr())).json()["user"]["id"])


def _product(key: str, name: str, source: str) -> Product:
    return Product(canonical_key=key, display_name=name, density=Density(183, 18, 11, 2),
                   source=source, is_estimate=source == "llm")


async def test_mine_shows_a_recipe_never_eaten(client, fake_container):
    """Рецепт без единого приёма — в «Моё» есть; чужие источники туда не попадают."""
    await fake_container.products.create(_product("kurica_s_mazikom", "курица с мазиком", "recipe"))
    await fake_container.products.create(_product("whey", "Impact whey protein", "manual"))
    await fake_container.products.create(_product("apple", "яблоко", "llm"))
    async with client as c:
        await _login(c)
        r = (await c.get("/api/products?sort=mine", headers=_hdr())).json()

    assert [x["name"] for x in r["items"]] == ["курица с мазиком"]
    assert r["items"][0]["source"] == "recipe"


async def test_recent_still_hides_what_was_not_eaten(client, fake_container):
    """«Недавние» не трогали: несъеденный рецепт там по-прежнему не показывается."""
    await fake_container.products.create(_product("kurica_s_mazikom", "курица с мазиком", "recipe"))
    async with client as c:
        await _login(c)
        r = (await c.get("/api/products?sort=recent", headers=_hdr())).json()

    assert r["items"] == []


async def test_sets_list_the_latest_composition(client, fake_container):
    """Набор отдаётся с составом самого свежего приёма с этим именем."""
    async with client as c:
        uid = await _login(c)
        items = [
            MealItem(name_snapshot="курица с мазиком", grams=120, macros=Macros(220, 22, 13, 2)),
            MealItem(name_snapshot="лаваш", grams=50, macros=Macros(137, 4.5, 0.6, 28)),
        ]
        await fake_container.meals.add(Meal(
            user_id=uid, eaten_at=datetime.now(UTC), input_type="text", raw_input="",
            items=items, source_confidence=0.9, totals=Macros(357, 26.5, 13.6, 30),
            template_name="курица в лаваше",
        ))
        r = (await c.get("/api/sets", headers=_hdr())).json()

    assert len(r["items"]) == 1
    s = r["items"][0]
    assert s["name"] == "курица в лаваше"
    assert [(i["name"], i["grams"]) for i in s["items"]] == [
        ("курица с мазиком", 120), ("лаваш", 50),
    ]
    assert s["kcal"] == 357
