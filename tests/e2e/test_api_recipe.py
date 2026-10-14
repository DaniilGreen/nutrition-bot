"""API правки рецепта (FR-39.7): состав в карточке и PUT состава. Цифры считает сервер."""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from app.application.entities import Product, Recipe, RecipeItem
from app.domain.value_objects import Density
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


async def _seed(fc, *, with_recipe: bool = True):
    chicken = await fc.products.create(Product(
        canonical_key="chicken", display_name="курица запечённая",
        density=Density(165, 31, 3.6, 0), source="llm", is_estimate=True,
    ))
    mayo = await fc.products.create(Product(
        canonical_key="mayo", display_name="майонез", density=Density(680, 1, 75, 1),
        source="curated", is_estimate=False,
    ))
    rec = await fc.products.create(Product(
        canonical_key="kurica_s_mazikom", display_name="курица с мазиком",
        density=Density(183.496402, 23.73381294, 9.0719424, 0.92805755),
        source="recipe", is_estimate=True,
    ))
    if with_recipe:
        await fc.products.set_recipe(rec.id, Recipe(items=[
            RecipeItem("курица запечённая", 1100, chicken.density, chicken.id, True),
            RecipeItem("майонез", 200, mayo.density, mayo.id, False),
        ]))
    return rec, chicken, mayo


async def test_card_carries_the_composition(client, fake_container):
    rec, _, _ = await _seed(fake_container)
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        d = (await c.get(f"/api/products/{rec.id}", headers=_hdr())).json()

    assert [(i["name"], i["grams"]) for i in d["recipe"]["items"]] == [
        ("курица запечённая", 1100), ("майонез", 200),
    ]
    assert d["recipe"]["total_g"] == 1300


async def test_old_recipe_has_null_composition(client, fake_container):
    """AC-83: состава нет — null, а не пустой список: карточка различает «нет» и «пусто»."""
    rec, _, _ = await _seed(fake_container, with_recipe=False)
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        d = (await c.get(f"/api/products/{rec.id}", headers=_hdr())).json()

    assert d["product"]["source"] == "recipe" and d["recipe"] is None


async def test_put_recomputes_on_the_server(client, fake_container):
    rec, _, _ = await _seed(fake_container)
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        r = await c.put(f"/api/products/{rec.id}/recipe", headers=_hdr(), json={
            "items": [{"src": 0, "grams": 1100, "kcal": 1}, {"src": 1, "grams": 100}],
            "yield_g": None,
        })

    assert r.status_code == 200
    expected = (1100 * 1.65 + 100 * 6.8) / 1200 * 100
    assert abs(r.json()["product"]["kcal_per_100g"] - expected) < 1


@pytest.mark.parametrize("body", [
    {"items": []},
    {"items": [{"src": 0, "grams": 0}]},
    {"items": [{"src": 0, "grams": 25000}]},
    {"items": [{"src": 0, "product_id": 1, "grams": 10}]},
    {"items": [{"src": 7, "grams": 10}]},
    {"items": [{"src": 0, "grams": 10}], "yield_g": -5},
    {"items": "мусор"},
])
async def test_put_refuses_bad_bodies(client, fake_container, body):
    rec, _, _ = await _seed(fake_container)
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        r = await c.put(f"/api/products/{rec.id}/recipe", headers=_hdr(), json=body)

    assert r.status_code == 400


async def test_put_on_a_plain_product_is_refused(client, fake_container):
    _, chicken, _ = await _seed(fake_container)
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        r = await c.put(f"/api/products/{chicken.id}/recipe", headers=_hdr(), json={
            "items": [{"product_id": chicken.id, "grams": 100}],
        })

    assert r.status_code == 400
