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


# ---------- по итогам ревью 15.10.2026 ----------

@pytest.mark.parametrize("item", [
    {"src": True, "grams": 10},            # bool — подкласс int, но не id
    {"src": 1.5, "grams": 10},             # дробь не округляем молча
    {"src": "0", "grams": 10},             # строка — не id
    {"product_id": 2**40, "grams": 10},    # за пределом int4 — раньше 500 из asyncpg
    {"src": 0, "grams": True},             # bool как граммы
    {"src": 0, "grams": "100"},            # строка как граммы
    "не объект",                           # раньше AttributeError → 500
])
async def test_put_strict_ids_and_types(client, fake_container, item):
    rec, _, _ = await _seed(fake_container)
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        r = await c.put(f"/api/products/{rec.id}/recipe", headers=_hdr(), json={"items": [item]})

    assert r.status_code == 400


async def test_put_infinity_is_400_not_500(client, fake_container):
    rec, _, _ = await _seed(fake_container)
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        r = await c.put(f"/api/products/{rec.id}/recipe", headers={
            **_hdr(), "Content-Type": "application/json",
        }, content='{"items": [{"src": 0, "grams": Infinity}]}')

    assert r.status_code == 400


async def test_put_impossible_yield_says_why(client, fake_container):
    rec, _, _ = await _seed(fake_container)
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        r = await c.put(f"/api/products/{rec.id}/recipe", headers=_hdr(), json={
            "items": [{"src": 0, "grams": 1100}, {"src": 1, "grams": 200}], "yield_g": 100,
        })

    assert r.status_code == 400
    assert "вес готового" in r.json()["error"]["message"].lower()


async def test_put_refuses_a_stale_position(client, fake_container):
    rec, _, _ = await _seed(fake_container)
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        r = await c.put(f"/api/products/{rec.id}/recipe", headers=_hdr(), json={
            "items": [{"src": 0, "name": "майонез", "grams": 1100}],
        })

    assert r.status_code == 400


async def test_put_on_plain_product_with_foreign_pick_changes_nothing(client, fake_container):
    """M4: правка обычного продукта как рецепта — отказ, и его плотность не тронута."""
    _, chicken, mayo = await _seed(fake_container)
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        r = await c.put(f"/api/products/{chicken.id}/recipe", headers=_hdr(), json={
            "items": [{"product_id": mayo.id, "grams": 100}],
        })
        after = (await c.get(f"/api/products/{chicken.id}", headers=_hdr())).json()["product"]

    assert r.status_code == 400
    assert after["source"] == "llm" and after["kcal_per_100g"] == 165


async def test_rename_via_edit_form_keeps_recipe_and_composition(client, fake_container):
    """Форма «Править»: и переименование, и правка плотности оставляют рецепт рецептом."""
    rec, _, _ = await _seed(fake_container)
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        await c.patch(f"/api/products/{rec.id}", headers=_hdr(), json={"name": "курица с соусом"})
        await c.patch(f"/api/products/{rec.id}", headers=_hdr(), json={
            "kcal_per_100g": 190, "protein_per_100g": 24, "fat_per_100g": 9, "carb_per_100g": 1,
        })
        d = (await c.get(f"/api/products/{rec.id}", headers=_hdr())).json()

    assert d["product"]["source"] == "recipe"
    assert d["recipe"] is not None and len(d["recipe"]["items"]) == 2


async def test_card_preview_density_matches_the_server(client, fake_container):
    """MA-INV-68: в карточку уходит плотность ПОСЛЕ gate — превью = результат сервера."""
    rec, _, _ = await _seed(fake_container)
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        d = (await c.get(f"/api/products/{rec.id}", headers=_hdr())).json()
        items = d["recipe"]["items"]
        preview = sum(i["kcal_per_100g"] * i["grams"] / 100 for i in items) / 1300 * 100
        r = await c.put(f"/api/products/{rec.id}/recipe", headers=_hdr(), json={
            "items": [
                {"src": k, "name": i["name"], "grams": i["grams"]} for k, i in enumerate(items)
            ],
        })

    assert abs(r.json()["product"]["kcal_per_100g"] - preview) < 0.05
