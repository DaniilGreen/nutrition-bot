"""§6.20 «Добор дня»: карточка появляется, когда день отстал от личной нормы.

Дневник теряет вторую половину дня — 28 дней из 49 у владельца. Карточка сравнивает
сегодняшний темп с его же обычным к этому часу. Цена ошибки двусторонняя: промолчать —
потерять запись, сказать зря — попрекнуть человека ни за что. Поэтому проверяем обе
стороны, а не только «показалось».
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.application.entities import Meal, MealItem
from app.domain.value_objects import Macros
from tests.initdata_fixtures import valid

TZ = ZoneInfo("Europe/Moscow")   # тот же, что default_timezone в настройках


def _meal(user_id: int, when: datetime, kcal: float, *, product_id: int | None = None,
          name: str = "ужин") -> Meal:
    macros = Macros(kcal, kcal * 0.19, kcal * 0.02, kcal * 0.05)
    return Meal(
        user_id=user_id, eaten_at=when, input_type="text", raw_input=name,
        items=[MealItem(name_snapshot=name, grams=250.0, macros=macros,
                        product_id=product_id)],
        source_confidence=1.0, totals=macros,
    )


def _history(repo, user_id: int, *, days: int, kcal_per_day: float,
             product_id: int | None = None, name: str = "ужин") -> None:
    """Прошлые дни, доведённые до конца. Еду кладём в начало суток, чтобы норма
    к ЛЮБОМУ текущему часу была полной — тест не должен зависеть от того,
    в котором часу его запустили."""
    now_local = datetime.now(UTC).astimezone(TZ)
    for back in range(1, days + 1):
        day = (now_local - timedelta(days=back)).replace(
            hour=0, minute=10, second=0, microsecond=0)
        repo._meals.append(_meal(user_id, day.astimezone(UTC), kcal_per_day,
                                 product_id=product_id, name=name))


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


async def _uid(c) -> int:
    return (await c.get("/api/me", headers=_hdr())).json()["user"]["id"]


async def test_отставший_день_получает_карточку(client, fake_container):
    async with client as c:
        uid = await _uid(c)
        _history(fake_container.meals, uid, days=8, kcal_per_day=2000)
        day = (await c.get("/api/day?days_ago=0", headers=_hdr())).json()
    pace = day["pace"]
    assert pace is not None, "норма набрана, сегодня пусто — карточка обязана быть"
    assert pace["norm_kcal"] == pytest.approx(2000.0, abs=1)
    assert pace["today_kcal"] == 0
    assert pace["gap_kcal"] == pytest.approx(2000.0, abs=1)
    assert pace["days_used"] == 8
    # График: две кривые, обе непустые и одинаково заканчиваются на текущем часе.
    assert pace["norm_points"] and pace["today_points"]
    assert pace["norm_points"][-1][0] == pace["today_points"][-1][0] == pace["hour"]


async def test_мало_дошедших_дней_карточки_нет(client, fake_container):
    """MA-INV-37: норма по трём дням — совпадение, а не норма. Попрекать
    человека совпадением нельзя."""
    async with client as c:
        uid = await _uid(c)
        _history(fake_container.meals, uid, days=3, kcal_per_day=2000)
        day = (await c.get("/api/day?days_ago=0", headers=_hdr())).json()
    assert day["pace"] is None


async def test_брошенные_дни_не_опускают_норму(client, fake_container):
    """Если бы норма считалась по всем дням подряд, она сползла бы ровно до уровня
    недозаписи — и сравнивать сегодняшний день стало бы не с чем."""
    async with client as c:
        uid = await _uid(c)
        _history(fake_container.meals, uid, days=6, kcal_per_day=2000)
        # Десять брошенных дней по 400 ккал — их в норму пускать нельзя.
        now_local = datetime.now(UTC).astimezone(TZ)
        for back in range(7, 17):
            when = (now_local - timedelta(days=back)).replace(hour=0, minute=10)
            fake_container.meals._meals.append(
                _meal(uid, when.astimezone(UTC), 400.0))
        day = (await c.get("/api/day?days_ago=0", headers=_hdr())).json()
    assert day["pace"]["norm_kcal"] == pytest.approx(2000.0, abs=1)
    assert day["pace"]["days_used"] == 6


async def test_день_в_графике_карточки_нет(client, fake_container):
    """Человек идёт по своей норме — говорить нечего."""
    async with client as c:
        uid = await _uid(c)
        _history(fake_container.meals, uid, days=8, kcal_per_day=2000)
        now_local = datetime.now(UTC).astimezone(TZ)
        today = now_local.replace(hour=0, minute=5)
        fake_container.meals._meals.append(
            _meal(uid, today.astimezone(UTC), 1800.0))
        day = (await c.get("/api/day?days_ago=0", headers=_hdr())).json()
    assert day["pace"] is None


async def test_только_что_записал_не_дёргаем(client, fake_container):
    """Человеку, который записывает прямо сейчас, подсказка не нужна."""
    async with client as c:
        uid = await _uid(c)
        _history(fake_container.meals, uid, days=8, kcal_per_day=2000)
        fake_container.meals._meals.append(
            _meal(uid, datetime.now(UTC) - timedelta(minutes=5), 100.0))
        day = (await c.get("/api/day?days_ago=0", headers=_hdr())).json()
    assert day["pace"] is None


async def test_у_прошлого_дня_карточки_нет(client, fake_container):
    """Вчерашний темп добирать поздно: карточка только про сегодня."""
    async with client as c:
        uid = await _uid(c)
        _history(fake_container.meals, uid, days=8, kcal_per_day=2000)
        day = (await c.get("/api/day?days_ago=1", headers=_hdr())).json()
    assert day["pace"] is None


async def test_чипы_берут_вечернюю_еду(client, fake_container):
    """Общий список частого уже под полем ввода. В карточке нужна та еда,
    которой в дневнике и не хватает, — вечерняя."""
    from app.application.entities import Product
    from app.domain.value_objects import Density

    async with client as c:
        uid = await _uid(c)
        supper = await fake_container.products.create(Product(
            canonical_key="pho_bo", display_name="Фо бо",
            density=Density(60, 4, 2, 6), source="llm", is_estimate=True))
        lunch = await fake_container.products.create(Product(
            canonical_key="oatmeal", display_name="Овсянка",
            density=Density(70, 3, 2, 12), source="llm", is_estimate=True))
        # Обед в 00:10 строит норму, ужин в 23:00 даёт вечернюю еду. Час 23
        # больше любого текущего, поэтому фильтр «после текущего часа» его
        # пропустит в любой момент суток, а обед — нет.
        now_local = datetime.now(UTC).astimezone(TZ)
        for back in range(1, 9):
            day = now_local - timedelta(days=back)
            fake_container.meals._meals.append(_meal(
                uid, day.replace(hour=0, minute=10).astimezone(UTC), 1600.0,
                product_id=lunch.id, name="Овсянка"))
            fake_container.meals._meals.append(_meal(
                uid, day.replace(hour=23, minute=0).astimezone(UTC), 400.0,
                product_id=supper.id, name="Фо бо"))
        day_json = (await c.get("/api/day?days_ago=0", headers=_hdr())).json()

    pace = day_json["pace"]
    assert pace is not None, "норма 2000, сегодня пусто — карточка обязана быть"
    names = [ch["name"] for ch in pace["chips"]]
    assert "Фо бо" in names, f"вечерней еды нет в чипах: {names}"
    assert "Овсянка" not in names, "обед в чипы добора попадать не должен"
    # Чип пишется без модели, значит нужен product_id и вес.
    chip = next(ch for ch in pace["chips"] if ch["name"] == "Фо бо")
    assert chip["product_id"] == supper.id
    assert chip["grams"] == pytest.approx(250.0)
