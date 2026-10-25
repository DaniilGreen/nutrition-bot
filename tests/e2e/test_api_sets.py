"""Наборы в аппке (FR-39.2/39.6): сохранить из карточки, записать с правкой состава.

Главное, что здесь доказывается: «Только сегодня» не подменяет набор, а цифры записанного
считает сервер — клиент шлёт только что и сколько.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.application.entities import Meal, MealItem, Product
from app.domain.value_objects import Density, Macros
from tests.initdata_fixtures import valid

SET = "курица в лаваше"


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


def _wrap(uid: int, *, name: str | None = None, ago_min: int = 60) -> Meal:
    items = [
        MealItem(name_snapshot="курица с мазиком", grams=120, macros=Macros(220, 22, 13, 2)),
        MealItem(name_snapshot="лаваш", grams=50, macros=Macros(137, 4.5, 0.6, 28)),
    ]
    return Meal(
        user_id=uid, eaten_at=datetime.now(UTC) - timedelta(minutes=ago_min),
        input_type="text", raw_input="курица с мазиком 120, лаваш 50", items=items,
        source_confidence=0.9, totals=Macros(357, 26.5, 13.6, 30), template_name=name,
    )


async def test_save_set_from_day_card(client, fake_container):
    async with client as c:
        uid = await _login(c)
        meal = await fake_container.meals.add(_wrap(uid))
        r = await c.post(f"/api/meals/{meal.id}/template", json={"name": SET}, headers=_hdr())
        sets = (await c.get("/api/sets", headers=_hdr())).json()["items"]

    assert r.status_code == 200 and r.json()["name"] == SET
    assert [s["name"] for s in sets] == [SET]


async def test_single_item_meal_is_not_a_set(client, fake_container):
    async with client as c:
        uid = await _login(c)
        one = _wrap(uid)
        one.items = one.items[:1]
        meal = await fake_container.meals.add(one)
        r = await c.post(f"/api/meals/{meal.id}/template", json={"name": SET}, headers=_hdr())

    assert r.status_code == 400


async def test_only_today_does_not_replace_the_set(client, fake_container):
    """Правка «только сегодня»: записано с правкой, а набор остался прежним."""
    async with client as c:
        uid = await _login(c)
        await fake_container.meals.add(_wrap(uid, name=SET))
        r = await c.post("/api/sets/log", headers=_hdr(), json={
            "name": SET, "update": False,
            "items": [{"src": 0, "grams": 180}],  # больше курицы, без лаваша
        })
        sets = (await c.get("/api/sets", headers=_hdr())).json()["items"]

    assert r.status_code == 200 and r.json()["kind"] == "logged"
    meal = r.json()["meal"]
    assert [(i["name"], i["grams"]) for i in meal["items"]] == [("курица с мазиком", 180)]
    assert abs(meal["kcal"] - 220 * 180 / 120) < 0.5  # снапшот × (180/120) — точно
    assert meal["template_name"] is None
    assert [(i["name"], i["grams"]) for i in sets[0]["items"]] == [
        ("курица с мазиком", 120), ("лаваш", 50),
    ], "«только сегодня» подменило набор"


async def test_update_makes_the_new_composition_the_set(client, fake_container):
    async with client as c:
        uid = await _login(c)
        await fake_container.meals.add(_wrap(uid, name=SET))
        await c.post("/api/sets/log", headers=_hdr(), json={
            "name": SET, "update": True,
            "items": [{"src": 0, "grams": 150}, {"src": 1, "grams": 50}],
        })
        sets = (await c.get("/api/sets", headers=_hdr())).json()["items"]

    assert [(i["name"], i["grams"]) for i in sets[0]["items"]] == [
        ("курица с мазиком", 150), ("лаваш", 50),
    ]


async def test_untouched_set_stays_alive_on_log(client, fake_container):
    """Записал как есть — имя едет дальше, набор жив, даже если старый приём удалят."""
    async with client as c:
        uid = await _login(c)
        await fake_container.meals.add(_wrap(uid, name=SET))
        r = await c.post("/api/sets/log", headers=_hdr(), json={
            "name": SET, "update": False,
            "items": [{"src": 0, "grams": 120}, {"src": 1, "grams": 50}],
        })

    assert r.json()["meal"]["template_name"] == SET


async def test_added_product_is_priced_by_the_server(client, fake_container):
    """Добавленное из каталога считается по ЗАПИННУТОЙ плотности; ккал от клиента не нужны."""
    bun = await fake_container.products.create(Product(
        canonical_key="bun", display_name="булочка", density=Density(270, 9, 4, 50),
        source="curated", is_estimate=False,
    ))
    async with client as c:
        uid = await _login(c)
        await fake_container.meals.add(_wrap(uid, name=SET))
        r = await c.post("/api/sets/log", headers=_hdr(), json={
            "name": SET, "update": False,
            "items": [{"src": 0, "grams": 120}, {"product_id": bun.id, "grams": 70, "kcal": 1}],
        })

    items = r.json()["meal"]["items"]
    assert [i["name"] for i in items] == ["курица с мазиком", "булочка"]
    assert abs(items[1]["kcal"] - 270 * 0.7) < 0.5, "ккал посчитаны не сервером"


@pytest.mark.parametrize("bad", [
    [{"src": 7, "grams": 100}],                       # индекс мимо состава
    [{"src": 0, "product_id": 1, "grams": 100}],      # оба источника сразу
    [{"src": 0, "grams": 0}],                         # нулевой вес
    [],                                               # пустой состав
])
async def test_bad_composition_is_refused(client, fake_container, bad):
    async with client as c:
        uid = await _login(c)
        await fake_container.meals.add(_wrap(uid, name=SET))
        r = await c.post("/api/sets/log", headers=_hdr(), json={"name": SET, "items": bad})

    assert r.status_code == 400


async def test_sets_report_uses_within_30_days(client, fake_container):
    """Чипы ставят набор в общий рейтинг по `uses_30d` (FR-40, §6.26): давний набор — 0."""
    async with client as c:
        uid = await _login(c)
        for ago in (60, 120):
            await fake_container.meals.add(_wrap(uid, name=SET, ago_min=ago))
        await fake_container.meals.add(_wrap(uid, name="Бургер", ago_min=60 * 24 * 31))
        sets = (await c.get("/api/sets", headers=_hdr())).json()["items"]

    assert {s["name"]: s["uses_30d"] for s in sets} == {SET: 2, "Бургер": 0}
