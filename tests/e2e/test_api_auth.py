"""API мини-аппа: дверь заперта, и открывается только подписью (§7.1, §8.1).

Через ASGITransport, без сети и без БД: контейнер подменён фейком.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest

from tests.initdata_fixtures import USER, sign, valid


@pytest.fixture
def client(fake_container):
    from app.main_api import app

    app.state.container = fake_container
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_health_открыт_без_подписи(client):
    """Единственное исключение из MA-INV-6: его дёргает деплой."""
    async with client as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_все_ошибки_в_одном_конверте(client):
    """Включая те, что бросает сам фреймворк: иначе фронту два формата."""
    async with client as c:
        no_route = await c.get("/api/нет-такого", headers={"X-Telegram-Init-Data": valid()})
        wrong_method = await c.post("/api/me", headers={"X-Telegram-Init-Data": valid()})
    for r in (no_route, wrong_method):
        assert "error" in r.json(), r.text
        assert "detail" not in r.json()


async def test_схема_api_наружу_не_публикуется(client):
    """MA-INV-6 знает одно исключение, /docs в него не входит."""
    async with client as c:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await c.get(path)).status_code == 404, path


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "hash=deadbeef",
        "hash=%C3%A4",  # не-ASCII: compare_digest бросал бы TypeError → 500
        "user=%7B%7D&auth_date=1",
        # Кириллица прямо в заголовке не проверяется: HTTP-заголовки обязаны быть
        # ASCII, такой запрос не соберёт ни клиент, ни Caddy. Настоящий вектор —
        # процент-кодирование, оно строкой выше.
        "a" * 5000,
    ],
    ids=["нет", "пусто", "битый-hash", "не-ascii", "без-подписи", "длинный"],
)
async def test_из_этого_эндпоинта_не_выходит_ничего_кроме_401(client, header):
    """Свойство целиком: чем бы ни был заголовок, наружу только 401."""
    headers = {} if header is None else {"X-Telegram-Init-Data": header}
    async with client as c:
        r = await c.get("/api/me", headers=headers)
    assert r.status_code == 401, r.text


async def test_конверт_ошибки_по_форме(client):
    """§7.4: ровно один формат — {"error": {"code", "message"}} на верхнем уровне.

    Раньше форм было три: своя у ApiError, своя у FastAPI и своя у обработчиков.
    Фронт читал одну, поэтому тексты 500 и 503 до человека не доходили.
    """
    async with client as c:
        r = await c.get("/api/me", headers={"X-Telegram-Init-Data": "hash=deadbeef"})
    err = r.json()["error"]
    assert err["code"] == "bad_signature"
    assert "подпис" not in err["message"].lower()  # причина наружу не уходит


async def test_протухшая_даёт_свой_код(client):
    old = sign(
        {
            "auth_date": str(int(time.time()) - 40 * 60 * 60),
            "user": json.dumps(USER, ensure_ascii=False, separators=(",", ":")),
        }
    )
    async with client as c:
        r = await c.get("/api/me", headers={"X-Telegram-Init-Data": old})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "init_data_expired"


async def test_me_с_подлинной_подписью(client):
    async with client as c:
        r = await c.get("/api/me", headers={"X-Telegram-Init-Data": valid()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["name"] == "Даниил"
    assert body["goal"]["kcal"] > 0
    # AC-MA-1: цифра за сегодня обязана быть, иначе первый экран нечем наполнить.
    assert body["today"]["kcal"] == 0
    assert body["today"]["meals_count"] == 0
    assert body["today"]["kcal_left"] == body["goal"]["kcal"]


async def test_пользователь_из_подписи_а_не_из_запроса(client):
    """MA-INV-7: личность только из подписанной строки."""
    async with client as c:
        r = await c.get("/api/me?telegram_id=1", headers={"X-Telegram-Init-Data": valid()})
    assert r.json()["user"]["name"] == "Даниил"


async def test_пользователь_заводится_один_раз(client, fake_container):
    """AC-MA-7: второй заход не плодит дубль."""
    async with client as c:
        first = (await c.get("/api/me", headers={"X-Telegram-Init-Data": valid()})).json()
        second = (await c.get("/api/me", headers={"X-Telegram-Init-Data": valid()})).json()
    assert first["user"]["id"] == second["user"]["id"]
    assert len(await fake_container.users.list_active()) == 1


async def test_успешный_запрос_фиксирует_транзакцию(client, fake_container):
    """Без commit запись выглядит успешной и исчезает — тихий класс багов."""
    async with client as c:
        await c.get("/api/me", headers={"X-Telegram-Init-Data": valid()})
    assert fake_container.db.session.commits == 1
    assert fake_container.db.session.rollbacks == 0


async def test_отказ_ничего_не_коммитит(client, fake_container):
    """AC-MA-2: 401 не оставляет следов в БД."""
    async with client as c:
        await c.get("/api/me", headers={"X-Telegram-Init-Data": "hash=deadbeef"})
    assert fake_container.db.session.commits == 0
    assert len(await fake_container.users.list_active()) == 0


async def test_отключённый_пользователь_403(client, fake_container):
    async with client as c:
        await c.get("/api/me", headers={"X-Telegram-Init-Data": valid()})
        (await fake_container.users.list_active())[0].is_active = False
        r = await c.get("/api/me", headers={"X-Telegram-Init-Data": valid()})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "inactive"


async def test_поле_signature_не_ломает_проверку(client):
    """Bot API 8.0 добавил `signature`; оно остаётся внутри data_check_string,
    как у aiogram. Тест держит спеку и код от расхождения."""
    fields = {
        "auth_date": str(int(time.time())),
        "signature": "abcDEF123_-",
        "user": json.dumps(USER, ensure_ascii=False, separators=(",", ":")),
    }
    async with client as c:
        r = await c.get("/api/me", headers={"X-Telegram-Init-Data": sign(fields)})
    assert r.status_code == 200, r.text
