"""ИИ-ассистент и лимит трат (§8.7, MA-M6, OQ-M3)."""
from __future__ import annotations

import httpx
import pytest

from app.domain.spend import Usage
from tests.fakes import InMemorySpendRepo
from tests.initdata_fixtures import valid


class StubCoach:
    """Вместо модели: возвращает заданный ответ и заданный расход."""

    def __init__(self, text="Белка мало.", tokens=(1000, 200)):
        self.text, self.tokens = text, tokens
        self.seen_facts = None

    async def ask(self, question, facts):
        self.seen_facts = facts
        return self.text, Usage("gpt-5.1", self.tokens[0], self.tokens[1])


@pytest.fixture
def client(fake_container):
    from app.main_api import app
    from app.web.drafts import DraftStore

    app.state.container = fake_container
    app.state.drafts = DraftStore()
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _hdr():
    return {"X-Telegram-Init-Data": valid()}


async def test_без_ключа_ассистент_честно_недоступен(client):
    """Молча притворяться работающим хуже, чем сказать «недоступен»."""
    async with client as c:
        r = await c.post("/api/coach", json={"question": "почему не худею"}, headers=_hdr())
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "coach_off"


async def test_ответ_и_расход(client, fake_container):
    fake_container.coach_port = StubCoach()
    fake_container.spend = InMemorySpendRepo()
    async with client as c:
        r = await c.post("/api/coach", json={"question": "почему не худею"}, headers=_hdr())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["text"] == "Белка мало."
    # 1000 входных по $1.25/1M + 200 выходных по $10/1M = $0,00325.
    # Наружу отдаётся округлённым до четырёх знаков — для бюджета в $5 это
    # доли цента; в БД лежит точное значение.
    assert d["spent_usd"] == pytest.approx(0.00325, abs=1e-4)
    assert d["limit_usd"] == 5.0


async def test_лимит_проверяется_до_запроса(client, fake_container):
    """Проверка после означала бы «превысил — узнал»: лимит, который не ограничивает."""
    port = StubCoach()
    fake_container.coach_port = port
    fake_container.spend = InMemorySpendRepo(preloaded_usd=5.0)
    async with client as c:
        r = await c.post("/api/coach", json={"question": "х"}, headers=_hdr())
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "coach_limit"
    assert port.seen_facts is None      # модель не вызывалась вовсе


async def test_расход_копится_между_запросами(client, fake_container):
    fake_container.coach_port = StubCoach()
    fake_container.spend = InMemorySpendRepo()
    async with client as c:
        first = (await c.post("/api/coach", json={"question": "а"}, headers=_hdr())).json()
        second = (await c.post("/api/coach", json={"question": "б"}, headers=_hdr())).json()
    assert second["spent_usd"] > first["spent_usd"]


async def test_модель_получает_посчитанные_цифры_а_не_сырьё(client, fake_container):
    """INV-1: модель объясняет числа, а не считает их."""
    port = StubCoach()
    fake_container.coach_port = port
    fake_container.spend = InMemorySpendRepo()
    async with client as c:
        await c.post("/api/coach", json={"question": "как дела"}, headers=_hdr())
    assert "Цель:" in port.seen_facts or "Записей за 30 дней нет." in port.seen_facts


@pytest.mark.parametrize("body", [{}, {"question": ""}, {"question": "  "}])
async def test_пустой_вопрос_400(client, fake_container, body):
    fake_container.coach_port = StubCoach()
    async with client as c:
        r = await c.post("/api/coach", json=body, headers=_hdr())
    assert r.status_code == 400


async def test_слишком_длинный_вопрос_400(client, fake_container):
    fake_container.coach_port = StubCoach()
    async with client as c:
        r = await c.post("/api/coach", json={"question": "я" * 501}, headers=_hdr())
    assert r.status_code == 400


async def test_ассистент_требует_подписи(client):
    async with client as c:
        assert (await c.post("/api/coach", json={"question": "х"})).status_code == 401
