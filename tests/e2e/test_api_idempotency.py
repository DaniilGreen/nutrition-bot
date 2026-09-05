"""MA-INV-10 (§7.5): двойной тап не пишет второй приём.

Худший класс бага для этого проекта: ничего не падает, приложение выглядит
исправным, а калории за день удвоены. До 05.09.2026 защита была половинчатой —
сервер помнил только ЗАВЕРШЁННЫЕ ответы, а тап дублируется именно пока первый
запрос ещё идёт; клиент же выдавал новый ключ на каждый клик.
"""
from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from tests.fakes import StubNlu
from tests.initdata_fixtures import valid


def _meal() -> ParsedMessage:
    return ParsedMessage(intent=Intent.MEAL, confidence=0.9, items=[ParsedItem(
        name="куриная грудка", canonical_key="chicken_breast", unit=Unit.GRAM, grams=200,
        kcal_per_100g=165, protein_per_100g=31, fat_per_100g=3.6, carb_per_100g=0)])


class SlowNlu(StubNlu):
    """Модель отвечает не мгновенно — как в жизни. Ради этого окна всё и затевалось."""

    def __init__(self, parsed, *, delay: float = 0.05) -> None:
        super().__init__(parsed)
        self._delay = delay

    async def parse_samples(self, text, known_keys):  # type: ignore[override]
        await asyncio.sleep(self._delay)
        return await super().parse_samples(text, known_keys)


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
    return {"X-Telegram-Init-Data": valid(), "Idempotency-Key": uuid4().hex, **extra}


async def _day(c):
    return (await c.get("/api/day?days_ago=0", headers=_hdr())).json()


async def test_запрос_без_ключа_отклоняется(client, fake_container):
    """Молчаливое «работает и без ключа» прячет дыру до первой удвоенной записи."""
    fake_container.nlu = StubNlu(_meal())
    async with client as c:
        r = await c.post("/api/meals", json={"text": "грудка"},
                         headers={"X-Telegram-Init-Data": valid()})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "no_idempotency_key"


async def test_двойной_тап_по_чипу_пишет_один_приём(client, fake_container):
    """Тот самый баг: у чипа «частого» ключ выдавался на клике, у двух кликов
    он разный — и защита не срабатывала."""
    fake_container.nlu = StubNlu(_meal())
    async with client as c:
        await c.post("/api/meals", json={"text": "грудка"}, headers=_hdr())
        pid = (await c.get("/api/products?limit=5", headers=_hdr())).json()["items"][0]["id"]
        h = _hdr(**{"Idempotency-Key": "chip-tap"})
        first = await c.post("/api/meals/quick",
                             json={"product_id": pid, "grams": 100}, headers=h)
        second = await c.post("/api/meals/quick",
                              json={"product_id": pid, "grams": 100}, headers=h)
        day = await _day(c)
    assert first.status_code == second.status_code == 200
    # Второй ответ — сохранённый первый, а не новая запись.
    assert first.json()["meal"]["id"] == second.json()["meal"]["id"]
    assert len(day["meals"]) == 2   # исходный текстовый приём + один по чипу


async def test_второй_тап_во_время_первого_отбивается(client, fake_container):
    """Ключевое окно §7.5: первый запрос ушёл в модель на секунды, кнопка молчит,
    палец жмёт ещё раз. Запоминать пока нечего — значит нужен «уже работаю»."""
    fake_container.nlu = SlowNlu(_meal(), delay=0.08)
    h = _hdr(**{"Idempotency-Key": "same-tap"})
    async with client as c:
        a, b = await asyncio.gather(
            c.post("/api/meals", json={"text": "грудка"}, headers=h),
            c.post("/api/meals", json={"text": "грудка"}, headers=h),
        )
        day = await _day(c)
    codes = sorted([a.status_code, b.status_code])
    assert codes == [200, 409], f"получили {codes}"
    losing = a if a.status_code == 409 else b
    assert losing.json()["error"]["code"] == "in_flight"
    assert len(day["meals"]) == 1


async def test_отказ_отпускает_ключ(client, fake_container):
    """Сеть моргнула, запись не прошла — повторить обязано быть можно тем же
    ключом. Иначе «попробуй ещё раз» упирается в блокировку на пять минут."""
    fake_container.nlu = StubNlu(_meal())
    h = _hdr(**{"Idempotency-Key": "retry-after-fail"})
    async with client as c:
        bad = await c.post("/api/meals", json={"text": ""}, headers=h)
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "empty_text"
        ok = await c.post("/api/meals", json={"text": "грудка"}, headers=h)
    assert ok.status_code == 200, ok.text
    assert ok.json()["kind"] == "logged"


async def test_повтор_приёма_под_тем_же_ключом_один(client, fake_container):
    """«Повторить» семантически НЕ идемпотентен — тем нужнее ключ: отличить
    осознанный второй повтор от продублировавшегося тапа может только он."""
    fake_container.nlu = StubNlu(_meal())
    async with client as c:
        m = (await c.post("/api/meals", json={"text": "грудка"},
                          headers=_hdr())).json()["meal"]
        h = _hdr(**{"Idempotency-Key": "rep-tap"})
        await c.post(f"/api/meals/{m['id']}/repeat", headers=h)
        await c.post(f"/api/meals/{m['id']}/repeat", headers=h)
        # Осознанный повтор позже — другой ключ, вторая запись появляется.
        await c.post(f"/api/meals/{m['id']}/repeat", headers=_hdr())
        day = await _day(c)
    assert len(day["meals"]) == 3   # оригинал + один по залипшему тапу + один осознанный


async def test_вес_тоже_под_ключом(client, fake_container):
    async with client as c:
        h = _hdr(**{"Idempotency-Key": "kg-tap"})
        await c.post("/api/weight", json={"weight_kg": 91.5}, headers=h)
        await c.post("/api/weight", json={"weight_kg": 91.5}, headers=h)
        pts = (await c.get("/api/stats/weight?limit=10", headers=_hdr())).json()["points"]
    assert len(pts) == 1
