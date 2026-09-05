"""Запись и правка веса (§6.12, §8.3): без экрана веса опечатку не починить."""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

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


def _hdr(**extra):
    # Ключ записи обязателен (§7.5): свежий на каждый запрос, если тест не задал свой.
    return {"X-Telegram-Init-Data": valid(), "Idempotency-Key": uuid4().hex, **extra}


async def test_запись_веса_одним_полем(client):
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        r = await c.post("/api/weight", json={"weight_kg": 91.5}, headers=_hdr())
    assert r.status_code == 200, r.text
    assert r.json()["weight_kg"] == 91.5
    assert r.json()["delta_kg"] is None      # первый замер — сравнивать не с чем


async def test_второй_замер_показывает_разницу(client):
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        await c.post("/api/weight", json={"weight_kg": 90.0}, headers=_hdr())
        r = await c.post("/api/weight", json={"weight_kg": 91.2}, headers=_hdr())
    assert r.json()["delta_kg"] == 1.2


@pytest.mark.parametrize("kg", [0, 19.9, 400.1, 1000, -5])
async def test_невозможный_вес_400(client, kg):
    """Проверка живёт в сервисе — значит и запись из аппки мимо неё не пройдёт."""
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        r = await c.post("/api/weight", json={"weight_kg": kg}, headers=_hdr())
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_weight"


@pytest.mark.parametrize("body", [{}, {"weight_kg": "много"}, {"weight_kg": None}])
async def test_негодное_тело_400(client, body):
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        r = await c.post("/api/weight", json=body, headers=_hdr())
    assert r.status_code == 400


async def test_опечатку_можно_удалить(client):
    """Ради этого экран и нужен: выброс 83,6 вместо 93,6 иначе останется навсегда."""
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        await c.post("/api/weight", json={"weight_kg": 91.5}, headers=_hdr())
        bad = (await c.post("/api/weight", json={"weight_kg": 83.6}, headers=_hdr())).json()
        gone = await c.delete(f"/api/weight/{bad['id']}", headers=_hdr())
        left = (await c.get("/api/stats/weight", headers=_hdr())).json()
    assert gone.status_code == 200
    assert gone.json()["weight_kg"] == 83.6
    assert [p["kg"] for p in left["points"]] == [91.5]


async def test_чужой_замер_не_удалить(client, fake_container):
    from datetime import UTC, datetime

    from app.application.entities import WeightLog

    alien = await fake_container.weights.add(
        WeightLog(user_id=999, measured_at=datetime.now(UTC), weight_kg=70.0)
    )
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        r = await c.delete(f"/api/weight/{alien.id}", headers=_hdr())
    assert r.status_code == 404


async def test_повтор_с_ключом_не_пишет_дважды(client):
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        h = _hdr(**{"Idempotency-Key": "weight-1"})
        a = (await c.post("/api/weight", json={"weight_kg": 91.5}, headers=h)).json()
        b = (await c.post("/api/weight", json={"weight_kg": 91.5}, headers=h)).json()
        pts = (await c.get("/api/stats/weight", headers=_hdr())).json()["points"]
    assert a["id"] == b["id"]
    assert len(pts) == 1


async def test_вес_требует_подписи(client):
    async with client as c:
        assert (await c.post("/api/weight", json={"weight_kg": 90})).status_code == 401
        assert (await c.delete("/api/weight/1")).status_code == 401


async def test_серия_отдаёт_id_для_удаления(client):
    """Без id замер нельзя удалить из интерфейса — а ради этого экран и нужен."""
    async with client as c:
        await c.get("/api/me", headers=_hdr())
        await c.post("/api/weight", json={"weight_kg": 91.5}, headers=_hdr())
        pts = (await c.get("/api/stats/weight", headers=_hdr())).json()["points"]
    assert pts[0]["id"] is not None
    assert "at" in pts[0] and "kg" in pts[0]
