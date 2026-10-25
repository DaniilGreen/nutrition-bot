"""«Приём пищи» в API (FR-47, MA-INV-79, MA-INV-80): группы в `/api/day`, «Сохранить набором».

Записи кладём во ВЧЕРА, в полдень по поясу человека: день в аппке режется по его поясу, и
запись «час назад» после полуночи уехала бы во вчера — тест падал бы от времени запуска.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.application.entities import Meal, MealItem
from app.domain.value_objects import Macros, sum_macros
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


def _hdr(key: str | None = None):
    return {"X-Telegram-Init-Data": valid(), "Idempotency-Key": key or uuid4().hex}


async def _login(c) -> int:
    return int((await c.get("/api/me", headers=_hdr())).json()["user"]["id"])


def _noon(fc, minutes: int) -> datetime:
    """Вчера 12:00 + minutes по поясу человека."""
    z = ZoneInfo(fc.settings.default_timezone)
    d = datetime.now(z).date() - timedelta(days=1)
    return (datetime(d.year, d.month, d.day, 12, tzinfo=z) + timedelta(minutes=minutes)
            ).astimezone(UTC)


def _item(name: str, grams: float, kcal: float) -> MealItem:
    return MealItem(name_snapshot=name, grams=grams,
                    macros=Macros(kcal, kcal / 20, kcal / 40, kcal / 10))


async def _record(fc, uid: int, minutes: int, *items: MealItem, raw: str = "x") -> Meal:
    return await fc.meals.add(Meal(
        user_id=uid, eaten_at=_noon(fc, minutes), input_type="text", raw_input=raw,
        items=list(items), source_confidence=0.9, totals=sum_macros(i.macros for i in items)))


async def _burger(fc, uid: int, *, ketchup: bool = False) -> list[Meal]:
    """Бургер пятью записями по 10 минут; `ketchup` — у лаваша вторая позиция."""
    parts = [("сыр", 20, 70), ("сыр", 20, 70), ("лаваш", 50, 137), ("помидор", 40, 8),
             ("курица", 120, 200)]
    out = []
    for k, (n, g, kc) in enumerate(parts):
        items = [_item(n, g, kc)]
        if ketchup and n == "лаваш":
            items.append(_item("кетчуп", 15, 15))
        out.append(await _record(fc, uid, 10 * k, *items, raw=f"{n} {g}"))
    return out


async def test_день_отдаёт_приёмы_пищи_дополнительно(client, fake_container):
    """AC-180: у записи `session`, группы с суммой; прежние поля ответа на месте."""
    async with client as c:
        uid = await _login(c)
        burger = await _burger(fake_container, uid)
        tea = await _record(fake_container, uid, 120, _item("чай", 200, 2))
        day = (await c.get("/api/day?days_ago=1", headers=_hdr())).json()

    assert day["session_gap_min"] == 30
    assert [m["session"] for m in day["meals"]] == [0, 0, 0, 0, 0, 1]
    s0, s1 = day["sessions"]
    assert s0["meal_ids"] == [m.id for m in burger] and s0["records"] == 5
    assert (s0["start"], s0["end"]) == ("12:00", "12:40")
    assert s0["kcal"] == pytest.approx(485) and s0["template_name"] is None
    assert (s1["index"], s1["meal_ids"], s1["records"]) == (1, [tea.id], 1)
    assert day["totals"]["kcal"] == pytest.approx(487)  # итоги дня — как были
    assert {"id", "items", "kcal", "template_name"} <= day["meals"][0].keys()


async def test_сохранить_приём_пищи_набором(client, fake_container):
    """AC-181: одна запись с пятью позициями, остальные удалены, набор в «Моё»."""
    async with client as c:
        uid = await _login(c)
        burger = await _burger(fake_container, uid)
        before = (await c.get("/api/day?days_ago=1", headers=_hdr())).json()["totals"]
        ids = [m.id for m in burger]
        r = await c.post("/api/sessions/template", json={"meal_ids": ids[::-1], "name": "бургер"},
                         headers=_hdr())
        day = (await c.get("/api/day?days_ago=1", headers=_hdr())).json()
        sets = (await c.get("/api/sets", headers=_hdr())).json()["items"]

    body = r.json()
    assert r.status_code == 200, body
    assert body["kind"] == "set_saved" and body["name"] == "бургер"
    assert body["meal"]["id"] == ids[0] and sorted(body["merged_ids"]) == ids[1:]
    assert [i["name"] for i in body["meal"]["items"]] == ["сыр", "сыр", "лаваш", "помидор",
                                                         "курица"]
    assert [m["id"] for m in day["meals"]] == [ids[0]]
    assert day["sessions"][0]["template_name"] == "бургер"
    assert day["totals"]["kcal"] == pytest.approx(before["kcal"], abs=0.1)
    assert [s["name"] for s in sets] == ["бургер"]


@pytest.mark.parametrize("pick", ["missing", "extra", "foreign"])
async def test_не_тот_приём_пищи_409(client, fake_container, pick):
    """AC-182: группу считает сервер; не совпало — 409, ничего не слито."""
    async with client as c:
        uid = await _login(c)
        ids = [m.id for m in await _burger(fake_container, uid)]
        tea = await _record(fake_container, uid, 120, _item("чай", 200, 2))
        alien = await _record(fake_container, uid + 999, 5, _item("чужое", 100, 100))
        ids = {"missing": ids[1:], "extra": [*ids, tea.id], "foreign": [*ids, alien.id]}[pick]
        r = await c.post("/api/sessions/template", json={"meal_ids": ids, "name": "бургер"},
                         headers=_hdr())
        day = (await c.get("/api/day?days_ago=1", headers=_hdr())).json()
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_a_session"
    assert len(day["meals"]) == 6


@pytest.mark.parametrize("body,code", [
    ({"meal_ids": [], "name": "x"}, "bad_meal_ids"),
    ({"meal_ids": [1, 1], "name": "x"}, "bad_meal_ids"),
    ({"meal_ids": ["1"], "name": "x"}, "bad_meal_ids"),
    ({"meal_ids": [True], "name": "x"}, "bad_meal_ids"),
    ({"meal_ids": 7, "name": "x"}, "bad_meal_ids"),
    ({"meal_ids": list(range(1, 52)), "name": "x"}, "bad_meal_ids"),
    ({"meal_ids": [1], "name": ""}, "bad_name"),
    ({"meal_ids": [1], "name": "я" * 65}, "bad_name"),
])
async def test_кривое_тело_400(client, body, code):
    async with client as c:
        await _login(c)
        r = await c.post("/api/sessions/template", json=body, headers=_hdr())
    assert r.status_code == 400 and r.json()["error"]["code"] == code


async def test_без_ключа_идемпотентности_400(client, fake_container):
    async with client as c:
        await _login(c)
        r = await c.post("/api/sessions/template", json={"meal_ids": [1], "name": "x"},
                         headers={"X-Telegram-Init-Data": valid()})
    assert r.status_code == 400 and r.json()["error"]["code"] == "no_idempotency_key"


async def test_без_веса_400(client, fake_container):
    """AC-184: правила набора FR-39.2."""
    async with client as c:
        uid = await _login(c)
        burger = await _burger(fake_container, uid)
        burger[1].items[0].grams = 0
        r = await c.post("/api/sessions/template",
                         json={"meal_ids": [m.id for m in burger], "name": "бургер"},
                         headers=_hdr())
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_set"
    assert len(fake_container.meals._meals) == 5


async def test_двойной_тап_сливает_один_раз(client, fake_container):
    """AC-183: тот же ключ — тот же ответ; новый ключ после слияния — 409."""
    async with client as c:
        uid = await _login(c)
        ids = [m.id for m in await _burger(fake_container, uid)]
        body = {"meal_ids": ids, "name": "бургер"}
        key = uuid4().hex
        r1 = await c.post("/api/sessions/template", json=body, headers=_hdr(key))
        r2 = await c.post("/api/sessions/template", json=body, headers=_hdr(key))
        r3 = await c.post("/api/sessions/template", json=body, headers=_hdr())
    assert r1.status_code == r2.status_code == 200 and r1.json() == r2.json()
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "not_a_session"
    assert len(fake_container.meals._meals) == 1


async def test_одна_запись_обычный_набор(client, fake_container):
    """AC-185: без слияния, `merged_ids` пуст."""
    async with client as c:
        uid = await _login(c)
        meal = await _record(fake_container, uid, 0, _item("курица", 120, 220),
                             _item("лаваш", 50, 137))
        r = await c.post("/api/sessions/template",
                         json={"meal_ids": [meal.id], "name": "курица в лаваше"}, headers=_hdr())
    assert r.status_code == 200
    assert r.json()["merged_ids"] == [] and r.json()["meal"]["id"] == meal.id


async def test_вернуть_после_слияния_отказывает(client, fake_container):
    """AC-187, MA-INV-80: убрал позицию → слил набором → «Вернуть» не задваивает позиции."""
    async with client as c:
        uid = await _login(c)
        burger = await _burger(fake_container, uid, ketchup=True)
        ketchup = burger[2].items[1]
        d = (await c.delete(f"/api/meals/{burger[2].id}/items/{ketchup.id}",
                            headers=_hdr())).json()
        r = await c.post("/api/sessions/template",
                         json={"meal_ids": [m.id for m in burger], "name": "бургер"},
                         headers=_hdr())
        back = await c.post("/api/meals/restore", json={"undo": d["undo"]}, headers=_hdr())
        day = (await c.get("/api/day?days_ago=1", headers=_hdr())).json()
    assert r.status_code == 200
    assert back.status_code == 409 and back.json()["error"]["code"] == "undo_stale"
    assert len(day["meals"]) == 1 and len(day["meals"][0]["items"]) == 5


async def test_вернуть_после_правки_отказывает(client, fake_container):
    """MA-INV-80: убрал позицию, потом поправил вес другой — снимок не откатывает правку."""
    async with client as c:
        uid = await _login(c)
        meal = await _record(fake_container, uid, 0, _item("гречка", 200, 220),
                             _item("котлета", 100, 250))
        a, b = meal.items
        d = (await c.delete(f"/api/meals/{meal.id}/items/{b.id}", headers=_hdr())).json()
        await fake_container.meals.update_item(uid, meal.id, a.id, grams=100,
                                               macros=Macros(110, 4, 1, 22))
        back = await c.post("/api/meals/restore", json={"undo": d["undo"]}, headers=_hdr())
    assert back.status_code == 409 and back.json()["error"]["code"] == "undo_stale"
