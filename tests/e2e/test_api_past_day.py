"""Запись попадает в тот день, который открыт (багрепорт #33).

Человек листает вчерашний день, пишет туда — и запись падала в сегодня. Проверяем
все четыре входа записи (текст, чип, повтор, ответ на уточнение) и приоритет:
слово «вчера» в самой фразе старше открытого экрана.
"""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from tests.fakes import StubNlu
from tests.initdata_fixtures import valid


def _meal(name="куриная грудка", key="chicken_breast", grams=200.0, day_offset=0,
          clarify=None) -> ParsedMessage:
    return ParsedMessage(
        intent=Intent.MEAL, confidence=0.9, day_offset=day_offset,
        items=[ParsedItem(
            name=name, canonical_key=key, unit=Unit.GRAM, grams=grams,
            kcal_per_100g=165.0, protein_per_100g=31.0, fat_per_100g=3.6,
            carb_per_100g=0.0, confidence=0.9, clarify_options=clarify,
        )],
    )


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


def _hdr(**extra):
    # Ключ записи обязателен (§7.5): свежий на каждый запрос, если тест не задал свой.
    return {"X-Telegram-Init-Data": valid(), "Idempotency-Key": uuid4().hex, **extra}


async def _day(c, days_ago: int) -> dict:
    r = await c.get(f"/api/day?days_ago={days_ago}", headers=_hdr())
    assert r.status_code == 200, r.text
    return r.json()


async def test_текст_уходит_в_открытый_день(client, fake_container):
    fake_container.nlu = StubNlu(_meal())
    async with client as c:
        r = await c.post(
            "/api/meals", json={"text": "200 г куриной грудки", "days_ago": 1}, headers=_hdr()
        )
        assert r.json()["kind"] == "logged", r.text
        yesterday = await _day(c, 1)
        today = await _day(c, 0)
    assert len(yesterday["meals"]) == 1
    assert today["meals"] == []


async def test_остаток_считается_от_того_же_дня(client, fake_container):
    """Ответ несёт остаток дня записи, а не сегодняшнего: иначе панель показала бы
    съеденное вчера как сегодняшний расход."""
    fake_container.nlu = StubNlu(_meal())
    async with client as c:
        r = await c.post(
            "/api/meals", json={"text": "200 г куриной грудки", "days_ago": 1}, headers=_hdr()
        )
        d = r.json()
        yesterday = await _day(c, 1)
    assert d["totals"]["kcal"] == pytest.approx(yesterday["totals"]["kcal"], abs=0.5)


async def test_слово_в_фразе_старше_открытого_экрана(client, fake_container):
    """Смотрю на вчера, говорю «позавчера» — значит позавчера, а не «ещё на день
    раньше вчерашнего». Складывать сдвиги значит промахиваться мимо обоих."""
    fake_container.nlu = StubNlu(_meal(day_offset=-2))
    async with client as c:
        await c.post(
            "/api/meals", json={"text": "позавчера ел курицу", "days_ago": 1}, headers=_hdr()
        )
        assert len((await _day(c, 2))["meals"]) == 1
        assert (await _day(c, 1))["meals"] == []
        assert (await _day(c, 3))["meals"] == []


async def test_чип_уходит_в_открытый_день(client, fake_container):
    fake_container.nlu = StubNlu(_meal())
    async with client as c:
        await c.post("/api/meals", json={"text": "200 г куриной грудки"}, headers=_hdr())
        pid = (await c.get("/api/products?limit=5", headers=_hdr())).json()["items"][0]["id"]
        r = await c.post(
            "/api/meals/quick",
            json={"product_id": pid, "grams": 100, "days_ago": 2}, headers=_hdr(),
        )
        assert r.json()["kind"] == "logged", r.text
        assert len((await _day(c, 2))["meals"]) == 1
        assert len((await _day(c, 0))["meals"]) == 1  # первый, сегодняшний


async def test_повтор_уходит_в_открытый_день(client, fake_container):
    fake_container.nlu = StubNlu(_meal())
    async with client as c:
        first = await c.post(
            "/api/meals", json={"text": "200 г куриной грудки"}, headers=_hdr()
        )
        mid = first.json()["meal"]["id"]
        r = await c.post(f"/api/meals/{mid}/repeat?days_ago=1", headers=_hdr())
        assert r.json()["kind"] == "logged", r.text
        assert len((await _day(c, 1))["meals"]) == 1


async def test_ответ_на_уточнение_помнит_день(client, fake_container):
    """Пока человек отвечает на вопрос, он мог пролистать на другой день. День
    берётся из черновика — из момента, когда запись начиналась."""
    fake_container.nlu = StubNlu(_meal(clarify=["с курицей", "с говядиной"]))
    async with client as c:
        r = await c.post(
            "/api/meals", json={"text": "шаверма", "days_ago": 1}, headers=_hdr()
        )
        d = r.json()
        assert d["kind"] == "clarify", r.text
        fake_container.nlu = StubNlu(_meal(name="шаверма с курицей", key="shaverma_chicken"))
        ok = await c.post(
            "/api/meals/clarify",
            json={"draft_id": d["draft_id"], "answer": "с курицей"}, headers=_hdr(),
        )
        assert ok.json()["kind"] == "logged", ok.text
        assert len((await _day(c, 1))["meals"]) == 1
        assert (await _day(c, 0))["meals"] == []


async def test_вперёд_писать_нельзя(client, fake_container):
    """Отрицательный days_ago — это завтра. Клиент туда не пускает, но запись в
    будущее испортила бы «остаток дня» и вечерний итог, поэтому режем на сервере."""
    fake_container.nlu = StubNlu(_meal())
    async with client as c:
        await c.post(
            "/api/meals", json={"text": "курица", "days_ago": -3}, headers=_hdr()
        )
        assert len((await _day(c, 0))["meals"]) == 1


async def test_мусор_вместо_дня_не_ломает_запись(client, fake_container):
    """Потеря записи дороже кривого параметра: не разобрали — пишем в сегодня."""
    fake_container.nlu = StubNlu(_meal())
    async with client as c:
        r = await c.post(
            "/api/meals", json={"text": "курица", "days_ago": "позавчера"}, headers=_hdr()
        )
        assert r.json()["kind"] == "logged", r.text
        assert len((await _day(c, 0))["meals"]) == 1
