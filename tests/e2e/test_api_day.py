"""Экран «День» на чтение (§8.2, MA-M1): панель остатка и лента приёмов."""
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


def _meal(user_id: int, *, hours_ago: float, name: str, grams: float,
          kcal: float, estimate: bool = False) -> Meal:
    m = Macros(kcal=kcal, protein=kcal / 20, fat=kcal / 40, carb=kcal / 10)
    return Meal(
        user_id=user_id,
        eaten_at=_at_local_noon() + timedelta(minutes=int(hours_ago)),
        input_type="text",
        raw_input=name,
        items=[MealItem(name_snapshot=name, grams=grams, macros=m, is_estimate=estimate)],
        source_confidence=0.9,
        totals=m,
    )


async def _login(c) -> int:
    r = await c.get("/api/me", headers={"X-Telegram-Init-Data": valid()})
    return int(r.json()["user"]["id"])


async def test_пустой_день(client):
    async with client as c:
        await _login(c)
        r = await c.get("/api/day", headers={"X-Telegram-Init-Data": valid()})
    assert r.status_code == 200
    d = r.json()
    assert d["meals"] == []
    assert d["totals"]["kcal"] == 0
    # Ничего не съедено — остаток равен цели целиком.
    assert d["totals"]["kcal_left"] == d["goal"]["kcal"]


async def test_день_с_приёмами(client, fake_container):
    async with client as c:
        uid = await _login(c)
        await fake_container.meals.add(_meal(uid, hours_ago=2, name="грудка", grams=200, kcal=330))
        await fake_container.meals.add(_meal(uid, hours_ago=1, name="рис", grams=190, kcal=248))
        r = await c.get("/api/day", headers={"X-Telegram-Init-Data": valid()})
    d = r.json()
    assert len(d["meals"]) == 2
    assert d["totals"]["kcal"] == 578
    assert d["totals"]["kcal_left"] == round(d["goal"]["kcal"] - 578, 1)


async def test_граммовка_видна_в_позиции(client, fake_container):
    """§6.2: без граммов приём нечем повторить и не с чем сверить."""
    async with client as c:
        uid = await _login(c)
        await fake_container.meals.add(_meal(uid, hours_ago=1, name="грудка", grams=200, kcal=330))
        r = await c.get("/api/day", headers={"X-Telegram-Init-Data": valid()})
    item = r.json()["meals"][0]["items"][0]
    assert item["grams"] == 200
    assert item["name"] == "грудка"


async def test_метка_оценки_доезжает_до_клиента(client, fake_container):
    """INV-13: «≈» нельзя потерять по дороге — иначе оценка выглядит как факт."""
    async with client as c:
        uid = await _login(c)
        await fake_container.meals.add(
            _meal(uid, hours_ago=1, name="борщ", grams=350, kcal=210, estimate=True)
        )
        r = await c.get("/api/day", headers={"X-Telegram-Init-Data": valid()})
    meal = r.json()["meals"][0]
    assert meal["estimated"] is True
    assert meal["items"][0]["is_estimate"] is True


async def test_чужой_день_не_виден(client, fake_container):
    """Приём другого пользователя не должен попасть в ленту."""
    async with client as c:
        await _login(c)
        await fake_container.meals.add(_meal(999, hours_ago=1, name="чужое", grams=100, kcal=500))
        r = await c.get("/api/day", headers={"X-Telegram-Init-Data": valid()})
    assert r.json()["meals"] == []
    assert r.json()["totals"]["kcal"] == 0


@pytest.mark.parametrize("bad", [-1, -100, 366, 10000])
async def test_день_вне_диапазона_400(client, bad):
    """Отрицательный days_ago показал бы «завтра» пустым и молча соврал."""
    async with client as c:
        await _login(c)
        r = await c.get(f"/api/day?days_ago={bad}", headers={"X-Telegram-Init-Data": valid()})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_days_ago"


async def test_день_требует_подписи(client):
    async with client as c:
        r = await c.get("/api/day")
    assert r.status_code == 401


async def test_день_отдаёт_вес_для_грамм_на_кг(client, fake_container):
    """§6.2: под белком — г/кг. Без веса это не посчитать."""
    from app.application.entities import WeightLog

    async with client as c:
        uid = await _login(c)
        await fake_container.weights.add(
            WeightLog(user_id=uid, measured_at=_at_local_noon(1), weight_kg=91.5)
        )
        d = (await c.get("/api/day", headers={"X-Telegram-Init-Data": valid()})).json()
    assert d["weight_kg"] == 91.5


async def test_без_замеров_вес_пустой(client):
    """Выдумывать вес нельзя — фронт по этому признаку прячет строку г/кг."""
    async with client as c:
        await _login(c)
        d = (await c.get("/api/day", headers={"X-Telegram-Init-Data": valid()})).json()
    assert d["weight_kg"] is None
