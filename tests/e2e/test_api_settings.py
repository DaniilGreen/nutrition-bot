"""Настройки (§6.5): оформление, цели, часовой пояс.

MA-INV-31: за каждой настройкой стоит поведение. Декоративных тут нет.
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.application.entities import WeightLog
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
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _hdr():
    return {"X-Telegram-Init-Data": valid()}


async def _login(c) -> int:
    return int((await c.get("/api/me", headers=_hdr())).json()["user"]["id"])


async def test_настройки_одним_запросом(client):
    async with client as c:
        await _login(c)
        d = (await c.get("/api/settings", headers=_hdr())).json()
    assert d["prefs"]["hero"] == "bars"      # принятый вариант Е по умолчанию
    assert d["timezone"] == "Europe/Moscow"
    assert d["coach"]["limit_usd"] == 5.0
    assert d["coach"]["available"] is False  # ключа в тестах нет — и это видно


async def test_смена_вида_панели_сохраняется(client):
    async with client as c:
        await _login(c)
        r = await c.patch("/api/settings/prefs", json={"hero": "rings"}, headers=_hdr())
        again = (await c.get("/api/settings", headers=_hdr())).json()
    assert r.json()["prefs"]["hero"] == "rings"
    assert again["prefs"]["hero"] == "rings"


async def test_частичная_правка_не_сбрасывает_остальное(client):
    async with client as c:
        await _login(c)
        await c.patch("/api/settings/prefs", json={"hero": "rings"}, headers=_hdr())
        r = await c.patch("/api/settings/prefs", json={"period": 7}, headers=_hdr())
    assert r.json()["prefs"] == {"hero": "rings", "period": 7,
                                 "foot_third": "meals", "mic_first": False}


@pytest.mark.parametrize(
    "patch", [{"hero": "чушь"}, {"period": 999}, {"неизвестно": 1}],
    ids=["негодное-значение", "негодный-период", "чужой-ключ"],
)
async def test_мусор_в_настройках_отбрасывается(client, patch):
    """JSONB примет что угодно — белый список не даёт этому случиться."""
    async with client as c:
        await _login(c)
        r = await c.patch("/api/settings/prefs", json=patch, headers=_hdr())
    assert r.status_code == 200
    assert r.json()["prefs"]["hero"] == "bars"
    assert "неизвестно" not in r.json()["prefs"]


async def test_не_объект_в_теле_отдаёт_конверт(client):
    """FastAPI по умолчанию шлёт 422 своей схемой — фронту пришлось бы
    разбирать два формата ошибок, и он разобрал бы неправильно."""
    from app.main_api import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as c:
        await _login(c)
        r = await c.patch("/api/settings/prefs", json="не объект", headers=_hdr())
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "malformed_body"


async def test_правка_целей(client):
    async with client as c:
        await _login(c)
        r = await c.patch(
            "/api/settings/goal",
            json={"kcal": 2200, "protein_g": 170, "fat_g": 80, "carb_g": 200, "target": "cut"},
            headers=_hdr(),
        )
        me = (await c.get("/api/me", headers=_hdr())).json()
    assert r.status_code == 200
    assert me["goal"]["kcal"] == 2200
    assert me["goal"]["target"] == "cut"


async def test_белок_в_граммах_на_килограмм(client, fake_container):
    """Цель поедет вслед за весом, а не устареет в момент похудения."""
    async with client as c:
        uid = await _login(c)
        await fake_container.weights.add(
            WeightLog(user_id=uid, measured_at=datetime.now(UTC), weight_kg=91.5)
        )
        r = await c.patch("/api/settings/goal", json={"protein_per_kg": 2.0}, headers=_hdr())
    assert r.json()["goal"]["protein_g"] == 183.0


async def test_без_веса_граммы_на_килограмм_невозможны(client):
    """Умножать не на что — говорим прямо, а не подставляем выдуманный вес."""
    async with client as c:
        await _login(c)
        r = await c.patch("/api/settings/goal", json={"protein_per_kg": 2.0}, headers=_hdr())
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "no_weight"


@pytest.mark.parametrize(
    "body",
    [{"kcal": 100}, {"kcal": 99999}, {"protein_g": 5000}, {"target": "чушь"}, {"kcal": "много"}],
    ids=["мало-ккал", "много-ккал", "много-белка", "чужой-режим", "не-число"],
)
async def test_негодные_цели_400(client, body):
    async with client as c:
        await _login(c)
        r = await c.patch("/api/settings/goal", json=body, headers=_hdr())
    assert r.status_code == 400


async def test_смена_часового_пояса(client):
    async with client as c:
        await _login(c)
        r = await c.patch(
            "/api/settings/timezone", json={"timezone": "Asia/Tbilisi"}, headers=_hdr()
        )
    assert r.status_code == 200
    assert r.json()["timezone"] == "Asia/Tbilisi"


@pytest.mark.parametrize("tz", ["", "Марс/Олимп", "UTC+3", "../../etc/passwd"])
async def test_негодный_часовой_пояс_400(client, tz):
    async with client as c:
        await _login(c)
        r = await c.patch("/api/settings/timezone", json={"timezone": tz}, headers=_hdr())
    assert r.status_code == 400


async def test_настройки_требуют_подписи(client):
    async with client as c:
        assert (await c.get("/api/settings")).status_code == 401
        assert (await c.patch("/api/settings/prefs", json={})).status_code == 401
        assert (await c.patch("/api/settings/goal", json={})).status_code == 401


async def test_багрепорт_уходит_с_контекстом(client, fake_container):
    """FR-26: без последних приёмов «посчитало не так» не воспроизвести."""
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        r = await c.post("/api/bug", json={"note": "борщ посчитало вдвое"}, headers=_hdr())
    assert r.status_code == 200
    assert fake_container.bugs.reports, "багрепорт не сохранился"
    assert "борщ" in fake_container.bugs.reports[-1]["note"]


@pytest.mark.parametrize("note", ["", "   ", None])
async def test_пустой_багрепорт_400(client, note):
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        r = await c.post("/api/bug", json={"note": note}, headers=_hdr())
    assert r.status_code == 400


async def test_режим_меняется(client):
    """Колонка user_goals.target существовала, а контрола не было."""
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        r = (await c.patch("/api/settings/goal", json={"target": "cut"},
                           headers=_hdr())).json()
        back = (await c.get("/api/settings", headers=_hdr())).json()
    assert r["goal"]["target"] == "cut"
    assert back["goal"]["target"] == "cut"


async def test_багрепорт_требует_подписи(client):
    async with client as c:
        assert (await c.post("/api/bug", json={"note": "х"})).status_code == 401
