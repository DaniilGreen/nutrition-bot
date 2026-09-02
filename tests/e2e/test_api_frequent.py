"""Частое и повтор одним тапом (§8.5, MA-M4)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.application.entities import Meal, MealItem
from app.domain.value_objects import Macros
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


def _at_local_noon(days_ago: int = 0) -> datetime:
    """Полдень нужного дня в зоне пользователя, в UTC.

    Не «N часов назад»: прогон после полуночи сдвигал бы приём во вчера, и тесты
    краснели раз в сутки по причине, не связанной с кодом.
    """
    tz = ZoneInfo("Europe/Moscow")
    day = (datetime.now(tz) - timedelta(days=days_ago)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    return day.astimezone(UTC)


def _hdr():
    return {"X-Telegram-Init-Data": valid()}


def _meal(uid: int, days_ago: float, *items) -> Meal:
    ms = [
        MealItem(
            name_snapshot=nm, grams=g,
            macros=Macros(kcal=g, protein=g * 0.2, fat=g * 0.02, carb=0),
        )
        for nm, g in items
    ]
    total = Macros(
        kcal=sum(i.macros.kcal for i in ms), protein=sum(i.macros.protein for i in ms),
        fat=sum(i.macros.fat for i in ms), carb=0,
    )
    return Meal(
        user_id=uid,
        eaten_at=_at_local_noon(int(days_ago)),
        input_type="text", raw_input=", ".join(i.name_snapshot for i in ms),
        items=ms, source_confidence=0.9, totals=total,
    )


async def _login(c) -> int:
    return int((await c.get("/api/me", headers=_hdr())).json()["user"]["id"])


async def test_частое_сортируется_по_числу_повторов(client, fake_container):
    async with client as c:
        uid = await _login(c)
        for i in range(4):
            await fake_container.meals.add(_meal(uid, i, ("грудка", 200)))
        for i in range(2):
            await fake_container.meals.add(_meal(uid, i, ("рис", 190)))
        r = (await c.get("/api/frequent", headers=_hdr())).json()
    assert [x["name"] for x in r["items"]] == ["грудка", "рис"]
    assert r["items"][0]["times"] == 4


async def test_граммовка_это_медиана_а_не_среднее(client, fake_container):
    """Одна ошибка «2000 г» не должна сдвигать подсказку."""
    async with client as c:
        uid = await _login(c)
        for g in (200, 210, 190, 2000, 205):
            await fake_container.meals.add(_meal(uid, 1, ("грудка", g)))
        r = (await c.get("/api/frequent", headers=_hdr())).json()
    assert r["items"][0]["grams"] == 205   # медиана; среднее было бы 561


async def test_старое_не_попадает_в_подсказки(client, fake_container):
    """Рацион меняется — прошлогоднее не подсказка."""
    async with client as c:
        uid = await _login(c)
        await fake_container.meals.add(_meal(uid, 200, ("гречка", 150)))
        await fake_container.meals.add(_meal(uid, 2, ("грудка", 200)))
        r = (await c.get("/api/frequent?days=30", headers=_hdr())).json()
    assert [x["name"] for x in r["items"]] == ["грудка"]


async def test_чужое_частое_не_видно(client, fake_container):
    async with client as c:
        await _login(c)
        await fake_container.meals.add(_meal(999, 1, ("чужое", 100)))
        r = (await c.get("/api/frequent", headers=_hdr())).json()
    assert r["items"] == []


@pytest.mark.parametrize("qs", ["days=0", "days=400", "limit=0", "limit=99"])
async def test_негодные_параметры_400(client, qs):
    async with client as c:
        await _login(c)
        r = await c.get("/api/frequent?" + qs, headers=_hdr())
    assert r.status_code == 400


async def test_повтор_несуществующего_404(client):
    async with client as c:
        await _login(c)
        r = await c.post("/api/meals/9999/repeat", headers=_hdr())
    assert r.status_code == 404


async def test_частое_требует_подписи(client):
    async with client as c:
        assert (await c.get("/api/frequent")).status_code == 401
        assert (await c.post("/api/meals/1/repeat")).status_code == 401


async def test_быстрая_запись_не_ходит_в_модель(client, fake_container):
    """AC-MA-31: тап по чипу обязан быть мгновенным и бесплатным."""
    from app.application.entities import Product
    from app.domain.value_objects import Density

    class Explodes:
        async def parse_samples(self, text, known_keys):
            raise AssertionError("модель не должна вызываться при быстрой записи")

        async def parse(self, text, known_keys):
            return await self.parse_samples(text, known_keys)

    fake_container.nlu = Explodes()
    p = await fake_container.products.create(
        Product(
            canonical_key="chicken",
            display_name="куриная грудка",
            density=Density(kcal_per_100g=165, protein_per_100g=31,
                            fat_per_100g=3.6, carb_per_100g=0),
            source="curated",
        )
    )
    async with client as c:
        await _login(c)
        r = await c.post(
            "/api/meals/quick", json={"product_id": p.id, "grams": 200}, headers=_hdr()
        )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["kind"] == "logged"
    assert d["meal"]["kcal"] == 330
    assert d["meal"]["items"][0]["grams"] == 200


async def test_быстрая_запись_несуществующего_продукта(client):
    async with client as c:
        await _login(c)
        r = await c.post(
            "/api/meals/quick", json={"product_id": 9999, "grams": 100}, headers=_hdr()
        )
    assert r.json()["kind"] == "unknown"


@pytest.mark.parametrize(
    "body", [{}, {"product_id": 1}, {"product_id": 1, "grams": 0},
             {"product_id": 1, "grams": 99999}, {"product_id": "нет", "grams": 1}],
)
async def test_негодная_быстрая_запись_400(client, body):
    async with client as c:
        await _login(c)
        r = await c.post("/api/meals/quick", json=body, headers=_hdr())
    assert r.status_code == 400


async def test_частое_отдаёт_product_id(client, fake_container):
    """Без него чип не может писать мгновенно и падает на путь через модель."""
    from app.application.entities import MealItem

    async with client as c:
        uid = await _login(c)
        m = _meal(uid, 1, ("грудка", 200))
        m.items = [MealItem(name_snapshot="грудка", grams=200,
                            macros=m.totals, product_id=7)]
        await fake_container.meals.add(m)
        r = (await c.get("/api/frequent", headers=_hdr())).json()
    assert r["items"][0]["product_id"] == 7
