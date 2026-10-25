"""Правка на месте и «Вернуть» (§6.27, MA-INV-69, D-MA-19, 21.10.2026)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.entities import Meal, MealItem, Product
from app.domain.value_objects import Density, Macros
from tests.fakes import StubNlu
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


def _meal(uid: int, *items: tuple[str, float, float], template: str | None = None) -> Meal:
    ms = [MealItem(name_snapshot=nm, grams=g, macros=Macros(k, g * 0.1, g * 0.05, g * 0.2))
          for nm, g, k in items]
    tot = Macros(sum(i.macros.kcal for i in ms), sum(i.macros.protein for i in ms),
                 sum(i.macros.fat for i in ms), sum(i.macros.carb for i in ms))
    return Meal(user_id=uid, eaten_at=datetime.now(UTC) - timedelta(hours=1), input_type="text",
                raw_input="x", items=ms, source_confidence=0.9, totals=tot, template_name=template)


def _nlu(name: str, key: str, kcal: float, **kw) -> StubNlu:
    return StubNlu(ParsedMessage(intent=Intent.MEAL, items=[ParsedItem(
        name=name, canonical_key=key, kcal_per_100g=kcal, protein_per_100g=12,
        fat_per_100g=3, carb_per_100g=60, **kw)]))


async def test_поправить_словами_меняет_еду_и_сохраняет_вес(client, fake_container):
    """«это был булгур»: вес не назван — остаётся 250 г, меняется плотность и имя."""
    await fake_container.products.create(Product(
        canonical_key="bulgur", display_name="булгур отварной",
        density=Density(83, 3.1, 0.2, 18.6), source="curated"))
    fake_container.nlu = _nlu("булгур отварной", "bulgur", 999, unit=Unit.PORTION)
    async with client as c:
        uid = await _login(c)
        meal = await fake_container.meals.add(
            _meal(uid, ("гречка", 250, 375), ("котлета", 120, 180)))
        r = await c.post(f"/api/meals/{meal.id}/items/{meal.items[0].id}/fix",
                         json={"text": "это был булгур"}, headers=_hdr())
    d = r.json()
    assert r.status_code == 200, d
    first = d["meal"]["items"][0]
    assert first["name"] == "булгур отварной" and first["grams"] == 250
    assert first["kcal"] == pytest.approx(83 * 2.5, abs=0.5)        # плотность каталога (INV-2)
    assert [i["name"] for i in d["meal"]["items"]][1] == "котлета"  # соседи на месте
    assert d["meal"]["id"] == meal.id                                # тот же приём


async def test_поправить_словами_с_весом_берёт_названный_вес(client, fake_container):
    fake_container.nlu = _nlu("индейка грудка", "turkey", 135, unit=Unit.GRAM, grams=150)
    async with client as c:
        uid = await _login(c)
        meal = await fake_container.meals.add(_meal(uid, ("курица", 200, 330)))
        r = await c.post(f"/api/meals/{meal.id}/items/{meal.items[0].id}/fix",
                         json={"text": "индейка 150 г"}, headers=_hdr())
    item = r.json()["meal"]["items"][0]
    assert item["grams"] == 150 and item["name"] == "индейка грудка"


async def test_поправить_словами_чужой_приём_и_пусто(client, fake_container):
    fake_container.nlu = _nlu("рис", "rice", 130, unit=Unit.GRAM, grams=150)
    async with client as c:
        uid = await _login(c)
        alien = await fake_container.meals.add(_meal(uid + 999, ("суп", 300, 150)))
        mine = await fake_container.meals.add(_meal(uid, ("суп", 300, 150)))
        r1 = await c.post(f"/api/meals/{alien.id}/items/{alien.items[0].id}/fix",
                          json={"text": "рис"}, headers=_hdr())
        r2 = await c.post(f"/api/meals/{mine.id}/items/{mine.items[0].id}/fix",
                          json={"text": "  "}, headers=_hdr())
    assert r1.status_code == 400 and r1.json()["error"]["code"] == "fix_failed"
    assert r2.status_code == 400


async def test_вернуть_удалённый_приём_как_был(client, fake_container):
    """MA-INV-69: время, позиции и имя набора — как до удаления; токен одноразовый."""
    async with client as c:
        uid = await _login(c)
        meal = await fake_container.meals.add(
            _meal(uid, ("курица", 150, 250), ("лаваш", 60, 160), template="курица в лаваше"))
        at = meal.eaten_at
        d = (await c.delete(f"/api/meals/{meal.id}", headers=_hdr())).json()
        r = await c.post("/api/meals/restore", json={"undo": d["undo"]}, headers=_hdr())
        again = await c.post("/api/meals/restore", json={"undo": d["undo"]}, headers=_hdr())
    back = r.json()["meal"]
    assert [(i["name"], i["grams"], i["kcal"]) for i in back["items"]] == \
        [("курица", 150, 250), ("лаваш", 60, 160)]
    restored = next(m for m in fake_container.meals._meals if m.id == back["id"])
    assert restored.eaten_at == at and restored.template_name == "курица в лаваше"
    assert again.status_code == 404 and again.json()["error"]["code"] == "undo_expired"


async def test_вернуть_убранную_позицию_возвращает_приём_целиком(client, fake_container):
    async with client as c:
        uid = await _login(c)
        meal = await fake_container.meals.add(
            _meal(uid, ("гречка", 250, 375), ("котлета", 120, 180)))
        d = (await c.delete(f"/api/meals/{meal.id}/items/{meal.items[1].id}",
                            headers=_hdr())).json()
        assert [i["name"] for i in d["meal"]["items"]] == ["гречка"]
        r = await c.post("/api/meals/restore", json={"undo": d["undo"]}, headers=_hdr())
        day = (await c.get("/api/day", headers=_hdr())).json()
    assert [i["name"] for i in r.json()["meal"]["items"]] == ["гречка", "котлета"]
    assert len(day["meals"]) == 1  # обрезанный вариант не остался рядом с восстановленным


async def test_чужой_токен_не_возвращает(client, fake_container):
    from app.web import routes

    async with client as c:
        uid = await _login(c)
        meal = await fake_container.meals.add(_meal(uid + 999, ("чужое", 100, 100)))
        token = routes._to_trash(meal, None)
        r = await c.post("/api/meals/restore", json={"undo": token}, headers=_hdr())
    assert r.status_code == 404
