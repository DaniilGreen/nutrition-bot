"""§6.21.1: ответ ассистента приходит кусками, и это же служит индикатором.

С инструментами ответ идёт до трёх раз дольше, и пустой экран на восемь секунд
читается как поломка. Проверяем ровно то, что ломается молча: отказы приходят
кодами ДО потока, расход пишется один раз и по всем раундам, а `end` несёт
полный текст — на нём держится запасной путь для старых клиентов.
"""
from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from app.application.coach import CoachChunk
from app.domain.spend import Usage
from tests.fakes import InMemorySpendRepo
from tests.initdata_fixtures import valid


class StubStream:
    """Порт, отдающий заготовленный поток."""

    def __init__(self, chunks=None, tokens=(1200, 300)):
        self._chunks = chunks
        self.tokens = tokens

    def _default(self):
        return [
            CoachChunk(kind="tool", text="top_products"),
            CoachChunk(kind="delta", text="Больше всего дают "),
            CoachChunk(kind="delta", text="молоко 3.2%."),
        ]

    async def ask(self, question, facts, data=None, history=()):
        return "нестримовый ответ", Usage("gpt-5.1", *self.tokens)

    async def stream(self, question, facts, data=None, history=()):
        self.seen_history = list(history)
        for c in (self._chunks if self._chunks is not None else self._default()):
            yield c
        yield CoachChunk(kind="end", usage=Usage("gpt-5.1", *self.tokens))


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


def _events(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


async def test_поток_отдаёт_инструмент_текст_и_итог(client, fake_container):
    fake_container.coach_port = StubStream()
    fake_container.spend = InMemorySpendRepo()
    async with client as c:
        r = await c.post("/api/coach/stream", json={"question": "откуда калории"},
                         headers=_hdr())
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    ev = _events(r.text)
    kinds = [e["kind"] for e in ev]
    assert kinds == ["tool", "delta", "delta", "end"], kinds
    # Индикатор — это имя инструмента, а не крутилка: видно, куда полезли.
    assert ev[0]["text"] == "top_products"
    assert ev[-1]["text"] == "Больше всего дают молоко 3.2%."


async def test_итог_несёт_полный_текст(client, fake_container):
    """На этом держится запасной путь: клиент, не осиливший поток, всё равно
    получает ответ целиком одним событием."""
    fake_container.coach_port = StubStream()
    fake_container.spend = InMemorySpendRepo()
    async with client as c:
        r = await c.post("/api/coach/stream", json={"question": "q"}, headers=_hdr())
    end = _events(r.text)[-1]
    assert end["kind"] == "end"
    assert end["text"] and end["limit_usd"] > 0
    assert "actions" in end


async def test_преамбула_перед_инструментом_отменяется(client, fake_container):
    """Модель пишет пару слов и передумывает. Без `reset` эта преамбула
    зависла бы над настоящим ответом."""
    fake_container.coach_port = StubStream([
        CoachChunk(kind="delta", text="Сейчас посмотрю…"),
        CoachChunk(kind="reset"),
        CoachChunk(kind="tool", text="meals_on"),
        CoachChunk(kind="delta", text="Во вторник был фо бо."),
    ])
    fake_container.spend = InMemorySpendRepo()
    async with client as c:
        r = await c.post("/api/coach/stream", json={"question": "q"}, headers=_hdr())
    ev = _events(r.text)
    assert "reset" in [e["kind"] for e in ev]
    assert ev[-1]["text"] == "Во вторник был фо бо."   # преамбулы в итоге нет


async def test_сервис_не_пишет_расход_второй_раз(client, fake_container):
    """Расход ассистента пишет адаптер после каждого раунда своей сессией
    (AI-PRINCIPLES §2). Сервис только показывает итог: запиши он ещё раз — учёт
    удвоится, и лимит сработает вдвое раньше."""
    fake_container.coach_port = StubStream(tokens=(2800, 900))
    spend = InMemorySpendRepo()
    fake_container.spend = spend
    async with client as c:
        r = await c.post("/api/coach/stream", json={"question": "q"}, headers=_hdr())
    end = _events(r.text)[-1]
    assert end["spent_usd"] > 0                          # показ итога — как был
    assert await spend.spent_this_month(purpose="coach") == 0


@pytest.mark.parametrize("body", [{}, {"question": "  "}])
async def test_пустой_вопрос_отклоняется_кодом_а_не_событием(client, fake_container, body):
    """MA-INV-41: после отправки заголовков статус не поменять, поэтому всё,
    что может отказать, проверяется до первого байта."""
    fake_container.coach_port = StubStream()
    async with client as c:
        r = await c.post("/api/coach/stream", json=body, headers=_hdr())
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "empty_question"


async def test_исчерпанный_бюджет_отклоняется_кодом(client, fake_container):
    fake_container.coach_port = StubStream()
    spend = InMemorySpendRepo()
    await spend.add(user_id=1, model="gpt-5.1", purpose="coach",
                    input_tokens=0, output_tokens=0, cost_usd=99.0)
    fake_container.spend = spend
    async with client as c:
        r = await c.post("/api/coach/stream", json={"question": "q"}, headers=_hdr())
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "coach_limit"


async def test_без_ключа_ассистента_503(client, fake_container):
    fake_container.coach_port = None
    async with client as c:
        r = await c.post("/api/coach/stream", json={"question": "q"}, headers=_hdr())
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "coach_off"


async def test_лимит_считает_весь_расход_а_не_только_ассистента(client, fake_container):
    """AI-PRINCIPLES §2: разбор еды — основная статья. Лимит, который её не
    видит, не ограничивает ничего (решение владельца 16.09.2026)."""
    fake_container.coach_port = StubStream()
    spend = InMemorySpendRepo()
    await spend.add(user_id=None, model="gpt-5.1", purpose="nlu",
                    input_tokens=0, output_tokens=0, cost_usd=99.0)
    fake_container.spend = spend
    async with client as c:
        r = await c.post("/api/coach/stream", json={"question": "q"}, headers=_hdr())
    assert r.status_code == 429


async def test_поток_получает_историю(client, fake_container):
    """§6.21.2: «разложи» в потоке — тоже с прошлыми репликами."""
    port = StubStream()
    fake_container.coach_port = port
    fake_container.spend = InMemorySpendRepo()
    history = [{"role": "user", "text": "сколько с 20:00"},
               {"role": "assistant", "text": "853 ккал"}]
    async with client as c:
        r = await c.post("/api/coach/stream", json={"question": "разложи", "history": history},
                         headers=_hdr())
    assert r.status_code == 200, r.text
    assert port.seen_history == [("user", "сколько с 20:00"), ("assistant", "853 ккал")]
