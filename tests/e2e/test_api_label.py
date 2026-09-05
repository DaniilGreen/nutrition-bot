"""Фото этикетки через API (FR-27, MA-INV-32): снимок → КБЖУ/100 г → вес → запись.

Ключевое, ради чего тест: плотность с упаковки НЕ переоценивается моделью при
ответе о весе. Пересчёт через NLU подменил бы точные цифры этикетки оценкой и
запинил бы её навсегда (INV-2) — молча и на все будущие записи продукта.
"""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from app.application.dto import ParsedItem, Unit
from tests.fakes import StubVision
from tests.initdata_fixtures import valid

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 500  # не картинка: до декодера дело не доходит, vision — стаб


def _label(**kw) -> ParsedItem:
    """Творожок с упаковки: 100 г = 90 ккал, пачка 200 г, порция 100 г."""
    base = dict(
        name="творожок Простоквашино 5%", canonical_key="tvorozhok_prostokvashino_5",
        unit=Unit.GRAM, kcal_per_100g=90.0, protein_per_100g=9.0,
        fat_per_100g=5.0, carb_per_100g=2.0, unit_weight_g=200.0, serving_g=100.0,
    )
    base.update(kw)
    return ParsedItem(**base)


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


async def test_снимок_возвращает_кбжу_и_ждёт_вес(client, fake_container):
    fake_container.vision = StubVision(_label())
    async with client as c:
        r = await c.post("/api/meals/label", content=JPEG, headers=_hdr())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["kind"] == "label"
    assert d["per100"]["kcal"] == 90.0
    assert d["package_g"] == 200.0 and d["serving_g"] == 100.0
    # Черновик не покидает сервер: клиент видит только непрозрачный ключ.
    assert d["draft_id"] and "kcal" not in str(d["draft_id"])


async def test_вес_числом_пишет_приём_по_цифрам_этикетки(client, fake_container):
    fake_container.vision = StubVision(_label())
    async with client as c:
        r = await c.post("/api/meals/label", content=JPEG, headers=_hdr())
        token = r.json()["draft_id"]
        r2 = await c.post(
            "/api/meals/clarify", json={"draft_id": token, "answer": "150"}, headers=_hdr()
        )
    assert r2.status_code == 200, r2.text
    d = r2.json()
    assert d["kind"] == "logged"
    # 150 г × 90 ккал/100 г = 135. Считает приложение, не модель (INV-1).
    assert d["meal"]["kcal"] == pytest.approx(135.0, abs=0.5)
    assert d["meal"]["items"][0]["grams"] == pytest.approx(150.0)


async def test_вся_пачка_берёт_вес_с_упаковки(client, fake_container):
    fake_container.vision = StubVision(_label())
    async with client as c:
        r = await c.post("/api/meals/label", content=JPEG, headers=_hdr())
        token = r.json()["draft_id"]
        r2 = await c.post(
            "/api/meals/clarify",
            json={"draft_id": token, "answer": "вся пачка"}, headers=_hdr()
        )
    d = r2.json()
    assert d["kind"] == "logged"
    assert d["meal"]["items"][0]["grams"] == pytest.approx(200.0)
    assert d["meal"]["kcal"] == pytest.approx(180.0, abs=0.5)


async def test_непонятный_вес_не_съедает_снимок(client, fake_container):
    """Черновик одноразовый. Если бы непонятый ответ его расходовал, человек
    терял бы фотографию — и вместе с ней единственные точные цифры."""
    fake_container.vision = StubVision(_label())
    async with client as c:
        r = await c.post("/api/meals/label", content=JPEG, headers=_hdr())
        token = r.json()["draft_id"]
        again = await c.post(
            "/api/meals/clarify",
            json={"draft_id": token, "answer": "ну немного"}, headers=_hdr()
        )
        d = again.json()
        assert d["kind"] == "label" and d["hint"] is True
        # Ключ новый — старый израсходован, но снимок остался в работе.
        assert d["draft_id"] != token
        ok = await c.post(
            "/api/meals/clarify",
            json={"draft_id": d["draft_id"], "answer": "100"}, headers=_hdr()
        )
    assert ok.json()["kind"] == "logged"


async def test_без_vision_отвечает_понятно(client, fake_container):
    """Ключа нет — эндпоинт обязан сказать это словами, а не упасть 500."""
    async with client as c:
        r = await c.post("/api/meals/label", content=JPEG, headers=_hdr())
    assert r.status_code == 200
    assert r.json()["kind"] == "unknown"


async def test_пустое_тело_отклоняется(client, fake_container):
    fake_container.vision = StubVision(_label())
    async with client as c:
        r = await c.post("/api/meals/label", content=b"", headers=_hdr())
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "empty_image"


async def test_повтор_с_тем_же_ключом_не_читает_дважды(client, fake_container):
    """Vision — самый дорогой вызов: ретрай сети не должен стоить второго
    распознавания и второго черновика."""
    fake_container.vision = StubVision(_label())
    async with client as c:
        h = _hdr(**{"Idempotency-Key": "same-shot"})
        a = await c.post("/api/meals/label", content=JPEG, headers=h)
        b = await c.post("/api/meals/label", content=JPEG, headers=h)
    assert a.json()["draft_id"] == b.json()["draft_id"]
