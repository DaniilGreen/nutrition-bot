"""Свой ингредиент в листе состава — текстом и фото этикетки (FR-39.8, 19.10.2026).

Владелец: «если нажимаю произвольный и потом сохранить, он пропадает… только то, что выпадает
из списка». Теперь любой текст идёт через разбор и каталог, а этикетка — через vision."""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.entities import Product
from app.domain.value_objects import Density
from tests.fakes import StubNlu, StubVision
from tests.initdata_fixtures import valid

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 500


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


def _item(name, key, grams, kcal, **kw) -> ParsedItem:
    return ParsedItem(name=name, canonical_key=key, unit=Unit.GRAM, grams=grams,
                      kcal_per_100g=kcal, protein_per_100g=20, fat_per_100g=5,
                      carb_per_100g=1, **kw)


async def test_текст_разбирается_в_строки_с_весами_через_каталог(client, fake_container):
    known = await fake_container.products.create(Product(
        canonical_key="chicken_breast", display_name="куриная грудка",
        density=Density(165, 31, 3.6, 0), source="curated"))
    fake_container.nlu = StubNlu(ParsedMessage(intent=Intent.MEAL, items=[
        _item("куриная грудка", "chicken_breast", 300, 999),  # плотность модели не берём (INV-2)
        _item("соус терияки", "teriyaki_sauce", 40, 89),
    ]))
    async with client as c:
        r = await c.post("/api/ingredients/parse", json={"text": "курица 300 г, терияки 40"},
                         headers=_hdr())
    d = r.json()
    assert r.status_code == 200, d
    assert [(x["product"]["name"], x["grams"]) for x in d["items"]] == \
        [("куриная грудка", 300), ("соус терияки", 40)]
    assert d["items"][0]["product"]["id"] == known.id
    assert d["items"][0]["product"]["kcal_per_100g"] == 165   # запиннутая, не 999
    assert d["items"][1]["product"]["id"] is not None        # новый — закреплён в каталоге
    assert d["skipped"] == []


async def test_названные_ккал_без_плотности_не_подставляются_молча(client, fake_container):
    fake_container.nlu = StubNlu(ParsedMessage(intent=Intent.MEAL, items=[
        ParsedItem(name="соус фирменный", canonical_key="sauce", unit=Unit.PORTION,
                   total_kcal=120)]))
    async with client as c:
        r = await c.post("/api/ingredients/parse", json={"text": "соус 120 ккал"},
                         headers=_hdr())
    assert r.json() == {"items": [], "skipped": ["соус фирменный"]}


async def test_пустой_и_длинный_текст_отказ(client, fake_container):
    async with client as c:
        empty = await c.post("/api/ingredients/parse", json={"text": " "}, headers=_hdr())
        long = await c.post("/api/ingredients/parse", json={"text": "х" * 301}, headers=_hdr())
    assert empty.status_code == 400 and long.status_code == 400


async def test_этикетка_становится_ингредиентом_с_весом_порции(client, fake_container):
    label = ParsedItem(name="йогурт греческий 2%", canonical_key="greek_yogurt_2",
                       unit=Unit.GRAM, kcal_per_100g=66, protein_per_100g=10,
                       fat_per_100g=2, carb_per_100g=4, serving_g=150)
    fake_container.vision = StubVision(label)
    async with client as c:
        r = await c.post("/api/ingredients/label", content=JPEG, headers=_hdr())
    x = r.json()["items"][0]
    assert x["grams"] == 150 and x["product"]["source"] == "label"
    assert x["product"]["kcal_per_100g"] == 66


async def test_этикетку_не_прочли_понятный_отказ(client, fake_container):
    fake_container.vision = StubVision(None)
    async with client as c:
        r = await c.post("/api/ingredients/label", content=JPEG, headers=_hdr())
    assert r.status_code == 400 and r.json()["error"]["code"] == "label_unread"


async def test_поиск_по_словам_в_любом_порядке(client, fake_container):
    await fake_container.products.create(Product(
        canonical_key="chicken_fillet", display_name="куриное филе",
        density=Density(110, 23, 1.2, 0), source="curated"))
    async with client as c:
        r = await c.get("/api/products?sort=name&q=филе%20кур", headers=_hdr())
    assert [p["name"] for p in r.json()["items"]] == ["куриное филе"]
