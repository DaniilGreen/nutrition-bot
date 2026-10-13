"""Скриншот меню с напечатанными КБЖУ (FR-38, AC-60…62): цифры как свои, без оценки модели."""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from app.application.dto import PlateReading
from app.domain.menu import MenuLine
from tests.fakes import StubVision
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


def _menu(*lines: MenuLine) -> StubVision:
    return StubVision(None, plate=PlateReading(kind="menu", menu=list(lines)))


async def test_карточка_меню_пишется_цифрами_с_картинки(client, fake_container):
    """AC-60: цифры как напечатаны, не оценка; без вопроса; отметка «меню»."""
    fake_container.vision = _menu(MenuLine("Chicken Caesar Salad", 410, 45.6, 11.4, 32.2),
                                  MenuLine("Super C Boost", 100, 1.4, 0.4, 24.9, portion="360 мл"))
    async with client as c:
        r = await c.post("/api/meals/photo", content=JPEG, headers=_hdr())
    d = r.json()
    assert r.status_code == 200 and d["kind"] == "logged", d
    m = d["meal"]
    assert m["input_type"] == "menu"
    assert [i["name"] for i in m["items"]] == ["Chicken Caesar Salad", "Super C Boost 360 мл"]
    caesar = m["items"][0]
    assert (caesar["kcal"], caesar["protein_g"], caesar["fat_g"], caesar["carb_g"]) == \
        (410, 45.6, 11.4, 32.2)
    assert not caesar["is_estimate"] and m["kcal"] == 510


async def test_перепутанная_цифра_не_пишется(client, fake_container):
    """AC-61: 41 ккал при Б 45.6 — ошибка чтения; молча записать неверное хуже вопроса."""
    fake_container.vision = _menu(MenuLine("Chicken Caesar Salad", 41, 45.6, 11.4, 32.2))
    async with client as c:
        r = await c.post("/api/meals/photo", content=JPEG, headers=_hdr())
        day = (await c.get("/api/day", headers=_hdr())).json()
    assert r.json()["kind"] != "logged"
    assert "текстом" in r.json()["message"]
    assert day["meals"] == []


async def test_счётчик_корзины_умножает_и_отсекает_незаказанное(client, fake_container):
    """AC-62: «2» в корзине — вдвое; блюдо из списка без счётчика не пишется."""
    fake_container.vision = _menu(MenuLine("Protein Yogurt", 462, 28.8, 13.5, 62.9, count=2),
                                  MenuLine("Acai Bowl", 380, 6, 12, 60))
    async with client as c:
        r = await c.post("/api/meals/photo", content=JPEG, headers=_hdr())
    m = r.json()["meal"]
    assert [i["name"] for i in m["items"]] == ["Protein Yogurt"]
    assert m["kcal"] == 924 and m["items"][0]["protein_g"] == 57.6


async def test_только_ккал_раскладывается_по_правилу_текста(client, fake_container):
    """INV-11.4: Б/Ж/У не напечатаны — не пишем нули, раскладываем и помечаем оценкой."""
    fake_container.vision = _menu(MenuLine("Борщ", 250))
    async with client as c:
        r = await c.post("/api/meals/photo", content=JPEG, headers=_hdr())
    item = r.json()["meal"]["items"][0]
    assert item["kcal"] == 250 and item["is_estimate"]
    assert item["protein_g"] > 0
