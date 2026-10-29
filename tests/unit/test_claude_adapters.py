"""NFR-6, D-41, AC-255…AC-262: Claude — второй провайдер разбора, фото и ассистента.

Всё на фейках, без сети. Проверяем ровно то, что фейки остальных тестов пропустили бы:
форму запроса (нет temperature, схема в output_config, метки кэша, картинка блоком), учёт
кэша по трём ценам, разбор отказов и пустого баланса, проводку переключателя.
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from app.application.dto import Intent
from app.config import Settings
from app.domain.spend import Usage
from app.infrastructure import claude_compat, llm_errors
from app.infrastructure.nlu import claude_coach, claude_nlu
from app.infrastructure.nlu.claude_coach import REFUSED, ClaudeCoach
from app.infrastructure.nlu.claude_nlu import ClaudeNlu
from app.infrastructure.nlu.openai_coach import MAX_ROUNDS
from app.infrastructure.nlu.prompts import SYSTEM_PROMPT
from app.infrastructure.vision import claude_vision
from app.infrastructure.vision.claude_vision import ClaudeVision
from app.infrastructure.vision.openai_vision import PHOTO_PROMPT
from tests.eval.harness import BudgetExceeded, EvalMeter

SAMPLING = ("temperature", "top_p", "top_k")

# --- фейки ------------------------------------------------------------------------------


def _usage(fresh=100, read=0, write=0, out=50):
    return SimpleNamespace(input_tokens=fresh, cache_read_input_tokens=read,
                           cache_creation_input_tokens=write, output_tokens=out)


def _msg(text="", *, stop="end_turn", usage=None, blocks=None):
    content = blocks if blocks is not None else [
        SimpleNamespace(type="thinking", thinking=""),   # ответ может начинаться с рассуждений
        SimpleNamespace(type="text", text=text),
    ]
    return SimpleNamespace(content=content, stop_reason=stop, usage=usage or _usage(),
                           stop_details=SimpleNamespace(category="cyber"))


def _meal_json(key="chicken_breast_grilled", total_kcal=None):
    return json.dumps({
        "intent": "meal", "confidence": 0.9, "items": [{
            "name": "курица", "canonical_key": key, "est_note": "грудка ~165 ккал/100 г",
            "unit": "g", "grams": 200, "kcal_per_100g": 165, "protein_per_100g": 31,
            "fat_per_100g": 3.6, "carb_per_100g": 0, "confidence": 0.9,
            "total_kcal": total_kcal,
        }],
    })


class Client:
    """`messages.create` отдаёт заготовки по очереди (последняя повторяется), помнит запросы."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests: list[dict] = []
        self.events: list[tuple[str, int]] = []
        self.messages = self

    async def create(self, **kwargs):
        n = len(self.requests)
        self.requests.append(kwargs)
        self.events.append(("start", n))
        await asyncio.sleep(0)            # дать параллельным вызовам шанс стартовать
        self.events.append(("end", n))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return item


class Meter:
    def __init__(self):
        self.notes: list[dict] = []

    async def note(self, **kw):
        self.notes.append(kw)


def _status(code: int, err_type: str, message: str) -> anthropic.APIStatusError:
    """Настоящее исключение SDK: класс по коду, как его строит клиент."""
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx2.Response(code, request=req)
    body = {"type": "error", "error": {"type": err_type, "message": message}}
    cls = {400: anthropic.BadRequestError, 429: anthropic.RateLimitError,
           529: anthropic.OverloadedError}.get(code, anthropic.APIStatusError)
    return cls(f"Error code: {code} - {body}", response=resp, body=body)


@pytest.fixture
def no_backoff(monkeypatch):
    import app.infrastructure.nlu.openai_nlu as m

    async def instant(_):
        return None

    monkeypatch.setattr(m.asyncio, "sleep", instant)


# --- совместимость семейств (AI-PRINCIPLES §3, §9) ---------------------------------------


def test_opus_рассуждения_не_выключаются_глубину_держит_effort():
    p = claude_compat.request_params("claude-opus-5-5", "low", thinking_off=True)
    assert p == {"output_config": {"effort": "low"}}   # disabled на Opus 5.5 — 400


def test_sonnet_выключается_between_tools_и_только_до_high():
    assert claude_compat.request_params("claude-sonnet-5-5", "high", thinking_off=True)[
        "thinking"] == {"type": "between_tools"}
    assert "thinking" not in claude_compat.request_params(
        "claude-sonnet-5-5", "xhigh", thinking_off=True)


def test_haiku_выключается_disabled_и_только_до_high():
    assert claude_compat.request_params("claude-haiku-5-5", "low", thinking_off=True)[
        "thinking"] == {"type": "disabled"}
    assert "thinking" not in claude_compat.request_params(
        "claude-haiku-5-5", "max", thinking_off=True)
    assert "thinking" not in claude_compat.request_params("claude-haiku-5-5", "low")


def test_кривой_effort_ловится_до_запроса():
    with pytest.raises(ValueError):
        claude_compat.request_params("claude-opus-5-5", "none")


def test_схема_ответа_в_output_config_строгая():
    from app.infrastructure.nlu.schema import NluResult

    fmt = claude_compat.request_params("claude-opus-5-5", "low", schema=NluResult)[
        "output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False


def test_адаптеры_claude_не_шлют_сэмплинг():
    """Страж класса, как у OpenAI: temperature/top_p/top_k у Claude 5.5 — 400 на КАЖДОМ
    вызове, а фейки этого не заметят. Ищем ключ в исходниках адаптеров и совместимости."""
    offenders = []
    for mod in (claude_nlu, claude_vision, claude_coach, claude_compat):
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [kw.arg for kw in getattr(node, "keywords", [])] if isinstance(
                node, ast.Call) else []
            if isinstance(node, ast.Dict):
                names = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            offenders += [f"{Path(mod.__file__).name}:{node.lineno}" for n in names
                          if n in SAMPLING]
    assert not offenders, offenders


def test_картинка_блоком_с_типом_по_сигнатуре():
    png = claude_compat.image_block(b"\x89PNG\r\n\x1a\nxxxx")
    jpg = claude_compat.image_block(b"\xff\xd8\xff\xe0xxxx")
    assert png["source"]["media_type"] == "image/png"
    assert jpg["source"]["media_type"] == "image/jpeg"
    assert png["type"] == "image" and png["source"]["type"] == "base64"


# --- разбор (AC-255, AC-256, AC-260) -----------------------------------------------------


async def test_разбор_форма_запроса_и_ответ():
    client = Client(_msg(_meal_json()))
    nlu = ClaudeNlu(client, "claude-opus-5-5", sample_k=1, effort="low")
    parsed = await nlu.parse("200 г курицы", ["rice_boiled"])

    assert parsed.intent is Intent.MEAL and parsed.items[0].grams == 200
    req = client.requests[0]
    assert not set(req) & set(SAMPLING)
    assert req["output_config"]["effort"] == "low"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["timeout"] == claude_nlu.TIMEOUT_S
    # Кэш: часовая метка на системе и на каталоге, сообщение человека — без метки.
    assert req["system"] == [{"type": "text", "text": SYSTEM_PROMPT,
                              "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
    catalog, message = req["messages"][0]["content"]
    assert "rice_boiled" in catalog["text"] and catalog["cache_control"]["ttl"] == "1h"
    assert "cache_control" not in message and "200 г курицы" in message["text"]


async def test_сэмплы_согласия_независимые_вызовы_после_первого():
    """AC-256: три вызова без temperature; сэмплы стартуют после первого — иначе не прочли бы
    кэш, который он записал."""
    client = Client(_msg(_meal_json()))
    nlu = ClaudeNlu(client, "claude-opus-5-5", sample_k=3)
    samples = await nlu.parse_samples("200 г курицы", [])
    assert len(samples) == 3 and len(client.requests) == 3
    assert client.events.index(("end", 0)) < client.events.index(("start", 1))
    assert all(not set(r) & set(SAMPLING) for r in client.requests)


async def test_знакомый_продукт_не_сэмплируется():
    client = Client(_msg(_meal_json()))
    nlu = ClaudeNlu(client, "claude-opus-5-5", sample_k=3)
    await nlu.parse_samples("200 г курицы", ["chicken_breast_grilled"])
    assert len(client.requests) == 1   # INV-12


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
async def test_отказ_и_обрезка_это_не_понял_а_расход_записан(stop):
    meter = Meter()
    client = Client(_msg('{"intent": "me', stop=stop))
    nlu = ClaudeNlu(client, "claude-opus-5-5", sample_k=1, meter=meter)
    parsed = await nlu.parse("…", [])
    assert parsed.intent is Intent.UNKNOWN
    assert len(client.requests) == 1 and len(meter.notes) == 1


# --- учёт (AC-257) -----------------------------------------------------------------------


async def test_расход_с_кэшем_по_трём_ценам():
    meter = Meter()
    client = Client(_msg(_meal_json(), usage=_usage(fresh=1000, read=9000, write=2000, out=300)))
    await ClaudeNlu(client, "claude-opus-5-5", sample_k=1, meter=meter).parse("x", [])
    note = meter.notes[0]
    assert note == {"model": "claude-opus-5-5", "input_tokens": 12000, "output_tokens": 300,
                    "cached_tokens": 9000, "cache_write_tokens": 2000,
                    "cache_write_5m_tokens": 0}
    usd = Usage(note["model"], 12000, 300, 9000, 2000).cost_usd
    assert usd == pytest.approx((1000 * 4 + 9000 * 0.20 + 2000 * 8 + 300 * 20) / 1e6)


def test_только_input_tokens_занизил_бы_счёт():
    f = claude_compat.meter_fields(_usage(1000, 9000, 2000, 300))
    assert (f["input_tokens"], f["cached_tokens"], f["cache_write_tokens"]) == (12000, 9000, 2000)


def test_запись_кэша_делится_по_ttl():
    """Ассистент пишет пятиминутный кэш (1.25×), разбор — часовой (2×): одна цена на оба
    завысила бы ассистента почти вдвое. Разбивки нет — всё по часовой (дорогая сторона)."""
    u = _usage(100, 0, 3000, 10)
    u.cache_creation = SimpleNamespace(ephemeral_5m_input_tokens=1000,
                                       ephemeral_1h_input_tokens=2000)
    f = claude_compat.meter_fields(u)
    assert (f["cache_write_tokens"], f["cache_write_5m_tokens"]) == (2000, 1000)
    usd = Usage("claude-opus-5-5", **f).cost_usd
    assert usd == pytest.approx((100 * 4 + 2000 * 8 + 1000 * 5 + 10 * 20) / 1e6)


# --- сбои (AC-258, AC-259) ---------------------------------------------------------------


@pytest.mark.parametrize("exc", [
    _status(402, "billing_error", "Billing issue"),
    _status(400, "invalid_request_error",
            "Your credit balance is too low to access the Anthropic API."),
])
async def test_пустой_баланс_не_повторяется(exc, no_backoff):
    assert llm_errors.out_of_credits(exc) and llm_errors.credits_provider(exc) == "anthropic"
    client = Client(exc)
    with pytest.raises(anthropic.APIStatusError):
        await ClaudeNlu(client, "claude-opus-5-5", sample_k=1).parse("x", [])
    assert len(client.requests) == 1


def test_обычный_400_не_баланс():
    exc = _status(400, "invalid_request_error", "messages: roles must alternate")
    assert not llm_errors.out_of_credits(exc) and not llm_errors.overloaded(exc)


async def test_перегрузка_529_повторяется_своим_циклом(no_backoff):
    overloaded = _status(529, "overloaded_error", "Overloaded")
    assert llm_errors.overloaded(overloaded) and not llm_errors.out_of_credits(overloaded)
    client = Client(overloaded, _msg(_meal_json()))
    parsed = await ClaudeNlu(client, "claude-opus-5-5", sample_k=1).parse("x", [])
    assert parsed.intent is Intent.MEAL and len(client.requests) == 2


def test_бот_говорит_перегружен_на_529_и_недоступна_на_балансе():
    from app.bot import coach_chat

    assert coach_chat._failure(_status(529, "overloaded_error", "x")) == coach_chat.OVERLOADED
    assert coach_chat._failure(_status(429, "rate_limit_error", "x")) == coach_chat.OVERLOADED
    assert coach_chat._failure(_status(402, "billing_error", "x")) == coach_chat.NO_CREDITS


# --- фото (AC-255, AC-260) ---------------------------------------------------------------


def _plate_json():
    return json.dumps({
        "kind": "dish", "dish": "курица с рисом",
        "components": [{"name": "рис отварной", "share": 0.6, "grams": 180, "fixed": False}],
        "caption_total_g": None, "cooking_fat": "none", "fat_kind": "масло подсолнечное",
        "dressing": "none", "liquid": False, "menu": [],
    })


async def test_фото_картинка_до_текста_и_кэш_на_системе():
    meter = Meter()
    client = Client(_msg(_plate_json(), usage=_usage(500, 2800, 0, 200)))
    vision = ClaudeVision(client, "claude-opus-5-5", meter, effort="low")
    plate = await vision.read_photo(b"\x89PNG\r\n\x1a\nxx", "тарелка")

    assert plate.kind == "dish" and plate.components[0].grams == 180
    req = client.requests[0]
    assert not set(req) & set(SAMPLING)
    assert req["system"][0]["text"] == PHOTO_PROMPT
    assert req["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    image, text = req["messages"][0]["content"]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/png"
    assert "тарелка" in text["text"]
    assert meter.notes[0]["cached_tokens"] == 2800 and meter.notes[0]["input_tokens"] == 3300


async def test_фото_отказ_это_не_прочитал():
    client = Client(_msg("", stop="refusal"))
    vision = ClaudeVision(client, "claude-opus-5-5")
    assert (await vision.read_photo(b"\xff\xd8x")).kind == "none"
    assert await vision.read_scan(b"\xff\xd8x") is None
    assert await vision.read_label(b"\xff\xd8x") is None


# --- ассистент (AC-262) ------------------------------------------------------------------


class Data:
    def __init__(self):
        self.seen = []

    async def day_totals(self, days=14):
        self.seen.append(("day_totals", days))
        return [{"date": "2026-10-28", "kcal": 1800}]

    async def meals_on(self, date_str=None):
        return {}

    async def top_products(self, days=30, by="kcal"):
        return []

    async def weight_log(self, limit=14):
        return []

    async def body_scans(self, limit=6):
        return []

    async def accuracy(self, days=30):
        return {}


def _tool_use(name="day_totals", args=None, cid="tu_1"):
    return SimpleNamespace(type="tool_use", id=cid, name=name,
                           input={"days": 7} if args is None else args)


def _calls_msg(*calls, usage=None):
    return _msg(blocks=[SimpleNamespace(type="thinking", thinking=""), *calls],
                stop="tool_use", usage=usage)


async def test_ассистент_инструменты_одни_на_все_раунды_и_последний_без_вызовов():
    client = Client(*[_calls_msg(_tool_use(cid=f"tu_{i}")) for i in range(MAX_ROUNDS)])
    coach = ClaudeCoach(client, "claude-opus-5-5", effort="medium")
    text, usage = await coach.ask("сколько я ел?", "цель 2000", Data())

    assert len(client.requests) == MAX_ROUNDS
    tools = [r["tools"] for r in client.requests]
    assert all(t == tools[0] for t in tools)            # смена tools — 400 и промах кэша
    assert {t["name"] for t in tools[0]} >= {"day_totals", "meals_on", "body_scans"}
    assert "input_schema" in tools[0][0] and "eager_input_streaming" not in tools[0][0]
    assert "tool_choice" not in client.requests[0]
    assert client.requests[-1]["tool_choice"] == {"type": "none"}
    assert client.requests[0]["output_config"] == {"effort": "medium"}
    # Кэш ассистента пятиминутный (раунды — секунды), хвост беседы — автоматической меткой.
    assert client.requests[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert client.requests[0]["cache_control"] == {"type": "ephemeral"}
    assert usage.input_tokens == 100 * MAX_ROUNDS     # сумма по всем раундам
    # Ответ модели — в историю как пришёл (с рассуждениями), результат — по id вызова.
    # (фейк хранит ссылку на растущий список — смотрим с начала: вопрос, ответ, результат)
    hist = client.requests[1]["messages"]
    assert hist[1]["role"] == "assistant" and hist[1]["content"][0].type == "thinking"
    assert hist[2]["content"][0]["tool_use_id"] == "tu_0"


async def test_ассистент_отказ_честной_фразой():
    client = Client(_msg("частичный ответ", stop="refusal"))
    text, _ = await ClaudeCoach(client, "claude-opus-5-5").ask("?", "", Data())
    assert text == REFUSED


async def test_история_начинается_с_человека():
    client = Client(_msg("ок"))
    await ClaudeCoach(client, "claude-opus-5-5").ask(
        "и?", "", None, history=[("assistant", "прошлый ответ"), ("user", "вопрос"),
                                 ("assistant", "ответ")])
    msgs = client.requests[0]["messages"]
    assert msgs[0]["role"] == "user" and "tools" not in client.requests[0]


class StreamClient:
    """`messages.stream` — менеджер контекста: события, потом итоговое сообщение."""

    def __init__(self, rounds):
        self.rounds = list(rounds)       # [(тексты, итоговое сообщение)]
        self.requests: list[dict] = []
        self.messages = self

    def stream(self, **kwargs):
        self.requests.append(kwargs)
        texts, final = self.rounds.pop(0)

        class Manager:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def __aiter__(self):
                return self._events()

            async def _events(self):
                for t in texts:
                    yield SimpleNamespace(type="text", text=t)
                yield SimpleNamespace(type="thinking", thinking="…")

            async def get_final_message(self):
                return final

        return Manager()


async def _collect(gen):
    return [c async for c in gen]


async def test_поток_eager_инструменты_reset_и_кривой_аргумент():
    meter = Meter()
    client = StreamClient([
        (["сейчас гляну"], _calls_msg(_tool_use(), _tool_use("meals_on", "не словарь", "tu_2"),
                                      usage=_usage(100, 0, 900, 20))),
        (["Ты ел ", "1800 ккал."], _msg("Ты ел 1800 ккал.", usage=_usage(50, 900, 0, 30))),
    ])
    data = Data()
    chunks = await _collect(ClaudeCoach(client, "claude-sonnet-5-5", meter).stream(
        "сколько?", "", data))
    kinds = [c.kind for c in chunks]

    assert kinds == ["delta", "reset", "tool", "tool", "delta", "delta", "end"]
    assert all(t.get("eager_input_streaming") for t in client.requests[0]["tools"])
    results = client.requests[1]["messages"][-1]["content"]
    assert data.seen == [("day_totals", 7)]                     # кривой вызов не исполнен
    assert results[1]["is_error"] and "INVALID_JSON" in results[1]["content"]
    end = chunks[-1].usage
    assert (end.input_tokens, end.cached_tokens, end.cache_write_tokens) == (1950, 900, 900)
    assert len(meter.notes) == 2                                # запись после каждого раунда


async def test_поток_отказ_стирает_обрывок():
    client = StreamClient([(["начал было"], _msg("начал было", stop="refusal"))])
    chunks = await _collect(ClaudeCoach(client, "claude-opus-5-5").stream("?", "", Data()))
    assert [c.kind for c in chunks] == ["delta", "reset", "delta", "end"]
    assert chunks[2].text == REFUSED


async def test_обрезанный_вызов_не_исполняется():
    cut = _msg(blocks=[_tool_use()], stop="max_tokens")
    data = Data()
    client = Client(cut)
    await ClaudeCoach(client, "claude-opus-5-5").ask("?", "", data)
    assert data.seen == [] and len(client.requests) == 1


# --- переключатель (AC-261) --------------------------------------------------------------


def _settings(**kw) -> Settings:
    kw.setdefault("openai_api_key", "")
    return Settings(_env_file=None, **kw)


def test_по_умолчанию_openai_и_прежние_модели():
    s = _settings()
    assert s.llm_provider == "openai"
    assert (s.model_for("nlu"), s.model_for("vision"), s.model_for("coach")) == (
        "gpt-5.1", "gpt-5.1", "gpt-5.1")


def test_anthropic_собирает_адаптеры_claude_а_голос_остаётся_whisper():
    from app.container import build_coach_port, build_nlu, build_stt, build_vision
    from app.infrastructure.stt.whisper_client import OpenAiStt

    s = _settings(llm_provider="anthropic", anthropic_api_key="sk-ant-test",
                  openai_api_key="sk-test", vision_model="claude-sonnet-5-5")
    nlu, vision, coach = build_nlu(s), build_vision(s), build_coach_port(s)
    assert isinstance(nlu, ClaudeNlu) and nlu._model == "claude-opus-5-5"
    assert nlu._params["output_config"]["effort"] == "low"
    assert isinstance(vision, ClaudeVision) and vision._model == "claude-sonnet-5-5"
    assert isinstance(coach, ClaudeCoach)
    assert coach._params == {"output_config": {"effort": "medium"}}
    assert nlu._client.max_retries == 0          # у разбора свой цикл повторов
    assert vision._client.max_retries == s.openai_max_retries
    assert isinstance(build_stt(s), OpenAiStt)


def test_anthropic_без_ключа():
    from app.container import build_coach_port, build_nlu, build_vision

    s = _settings(llm_provider="anthropic")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        build_nlu(s)
    assert build_vision(s) is None and build_coach_port(s) is None


def test_чужая_модель_в_маршруте_отказ_на_старте():
    from app.container import Container

    s = _settings(llm_provider="anthropic", anthropic_api_key="k", vision_model="gpt-5.1")
    with pytest.raises(ValueError, match="VISION_MODEL=gpt-5.1"):
        Container.create(s)
    with pytest.raises(ValueError):
        _settings(nlu_model="claude-opus-5-5").model_for("nlu")


def test_ключ_не_светится_в_настройках():
    s = _settings(anthropic_api_key="sk-ant-secret")
    assert "sk-ant-secret" not in repr(s) and "sk-ant-secret" not in str(s.model_dump())


# --- eval: потолок трат и задержка -------------------------------------------------------


async def test_потолок_eval_не_даёт_начать_вызов():
    meter = EvalMeter(max_usd=0.01)

    class Adapter:
        calls = 0

        async def _parse(self):
            Adapter.calls += 1
            await meter.note(model="claude-opus-5-5", input_tokens=10_000, output_tokens=0)

    a = Adapter()
    meter.timed(a, "_parse")
    await a._parse()                       # $0.04 > потолка — но начатый вызов доплачен
    with pytest.raises(BudgetExceeded):
        await a._parse()
    assert Adapter.calls == 1
    summary = meter.summary(records=1)
    assert summary["usd_total"] == pytest.approx(0.04) and summary["latency_s"]["n"] == 1


# --- настоящий SDK поверх подменённого транспорта ------------------------------------------
# Фейки выше не ловят, если SDK перестанет принимать нашу форму запроса или поменяет версию
# API. Здесь — настоящий AsyncAnthropic, сеть подменена httpx2.MockTransport.


def _sdk(script, seen):
    def handler(req):
        seen.append((req.headers.get("anthropic-version"), json.loads(req.content)))
        item = script.pop(0)
        if isinstance(item, str):
            return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                                   content=item.encode())
        return httpx2.Response(200, json=item)

    return anthropic.AsyncAnthropic(
        api_key="test", max_retries=0, timeout=5,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )


def _wire(content, stop="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100,
                      "cache_creation_input_tokens": 20}}


def _sse(*events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


async def test_sdk_принимает_запрос_разбора_без_сэмплинга():
    seen: list = []
    reply = _wire([{"type": "thinking", "thinking": "", "signature": "s"},
                   {"type": "text", "text": _meal_json()}])
    nlu = ClaudeNlu(_sdk([reply], seen), "claude-opus-5-5", sample_k=1)
    assert (await nlu.parse("200 г курицы", ["a"])).intent is Intent.MEAL
    _, body = seen[0]
    # Сэмплинга нет ни на какой версии API: Opus/Sonnet 5.5 отвергают его уже сейчас.
    assert not set(body) & set(SAMPLING)
    assert sorted(body) == ["max_tokens", "messages", "model", "output_config", "system"]


async def test_sdk_поток_ассистента_с_инструментом():
    seen: list = []
    start = {"type": "message_start", "message": _wire([])}
    tool_round = _sse(
        start,
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "tu9", "name": "day_totals", "input": {}}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"days": 5}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
         "usage": {"output_tokens": 7}},
        {"type": "message_stop"},
    )
    text_round = _sse(
        start,
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "Итог."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    )
    data = Data()
    coach = ClaudeCoach(_sdk([tool_round, text_round], seen), "claude-opus-5-5")
    chunks = await _collect(coach.stream("?", "", data))
    assert [(c.kind, c.text) for c in chunks[:3]] == [("tool", "day_totals"), ("delta", "Итог."),
                                                     ("end", "")]
    assert data.seen == [("day_totals", 5)]
    _, second = seen[1]
    assert second["stream"] is True and second["tools"][0]["eager_input_streaming"] is True
    assert second["messages"][1]["content"][0]["type"] == "tool_use"   # ответ — как пришёл


# --- ревью: схема, неверная настройка, алерты, обрезка, старт ------------------------------


def test_схема_claude_все_поля_обязательны_в_порядке_pydantic():
    """AC-267: иначе structured outputs выводит обязательные первыми — est_note (рассуждение до
    чисел) уехал бы после КБЖУ, confidence — вперёд items. Схема OpenAI не тронута."""
    from app.infrastructure.nlu.schema import NluItem, NluResult

    schema = claude_compat.json_format(NluResult)["schema"]
    item = schema["$defs"]["NluItem"]
    assert item["required"] == list(NluItem.model_fields) == list(item["properties"])
    assert item["required"].index("est_note") < item["required"].index("kcal_per_100g")
    assert schema["required"] == list(NluResult.model_fields)
    assert schema["required"][-1] == "confidence"
    # Pydantic-схема для OpenAI — прежняя: необязательные там необязательны.
    assert "weight_kg" not in NluResult.model_json_schema()["required"]


@pytest.mark.parametrize("code,err", [(401, "authentication_error"), (403, "permission_error"),
                                      (404, "not_found_error")])
async def test_неверный_ключ_или_модель_без_повторов(code, err, no_backoff):
    """AC-263: повтор не поможет — нужен человек с ключом."""
    exc = _status(code, err, "model: claude-opus-5-5")
    assert llm_errors.provider_down(exc) == ("anthropic", "config")
    assert not llm_errors.overloaded(exc)
    client = Client(exc)
    with pytest.raises(anthropic.APIStatusError):
        await ClaudeNlu(client, "claude-opus-5-5", sample_k=1).parse("x", [])
    assert len(client.requests) == 1


def test_неверный_ключ_openai_тоже_настройка():
    import httpx
    import openai

    resp = httpx.Response(401, request=httpx.Request("POST", "https://api.openai.com"))
    exc = openai.AuthenticationError("bad key", response=resp, body=None)
    assert llm_errors.provider_down(exc) == ("openai", "config")


def test_ассистент_в_боте_на_неверный_ключ_говорит_недоступна():
    from app.bot import coach_chat

    assert coach_chat._failure(_status(401, "authentication_error", "x")) == coach_chat.NO_CREDITS


def _alert_world(sent):
    from contextlib import asynccontextmanager

    class Bot:
        async def send_message(self, chat_id, text):
            sent.append(text)

    async def list_active():
        return [SimpleNamespace(role="owner", telegram_id=1)]

    @asynccontextmanager
    async def session():
        yield None

    users = SimpleNamespace(list_active=list_active)
    return Bot(), SimpleNamespace(db=SimpleNamespace(session_factory=session),
                                  assistant_for=lambda s: (None, users),
                                  settings=SimpleNamespace(llm_provider="anthropic"))


async def test_алерты_раздельно_по_провайдеру_и_причине(monkeypatch):
    """AC-266: голос на OpenAI и текст на Anthropic кончаются независимо."""
    from app.bot import handlers

    monkeypatch.setattr(handlers, "_no_credits_alerted_at", {})
    sent: list[str] = []
    bot, container = _alert_world(sent)
    await handlers._alert_owner_no_credits(bot, container, "openai")
    await handlers._alert_owner_no_credits(bot, container, "anthropic")
    await handlers._alert_owner_no_credits(bot, container, "anthropic", "config")
    await handlers._alert_owner_no_credits(bot, container, "openai")      # повтор — тишина
    assert len(sent) == 3
    # Текст OpenAI — дословно прежний.
    assert sent[0] == ("🔌 <b>Закончился баланс OpenAI</b>\n\nРазбор еды, голос и фото не "
                       "работают, пока не пополнишь: platform.openai.com → Settings → Billing.")
    assert "platform.claude.com → Settings → Billing" in sent[1]
    assert "Ключ Anthropic не принят или модель недоступна" in sent[2]


async def test_обрезанный_ответ_ассистента_с_припиской():
    """AC-265: обрывок по max_tokens не выдаётся молча за ответ."""
    client = Client(_msg("Ты съел", stop="max_tokens"))
    text, _ = await ClaudeCoach(client, "claude-opus-5-5").ask("?", "", Data())
    assert text == f"Ты съел\n\n{claude_coach.CUT}"
    assert client.requests[0]["max_tokens"] == 16000

    stream = StreamClient([(["Ты съел"], _msg("Ты съел", stop="max_tokens"))])
    chunks = await _collect(ClaudeCoach(stream, "claude-opus-5-5").stream("?", "", Data()))
    assert [c.kind for c in chunks] == ["delta", "delta", "end"]
    assert chunks[1].text == f"\n\n{claude_coach.CUT}"


async def test_проверка_моделей_на_старте(monkeypatch):
    """AC-264: каждая модель маршрута — один бесплатный запрос; отказ — исключение наверх."""
    from app import container as wiring

    asked: list[str] = []

    class Models:
        async def retrieve(self, model):
            asked.append(model)
            if model == "claude-haiku-5-5":
                raise _status(404, "not_found_error", f"model: {model}")

    class Fake:
        models = Models()

        async def close(self):
            asked.append("closed")

    monkeypatch.setattr(wiring, "_anthropic", lambda s, max_retries: Fake())
    ok = _settings(llm_provider="anthropic", anthropic_api_key="k",
                   coach_model="claude-sonnet-5-5")
    await wiring.check_provider(ok)
    assert asked == ["claude-opus-5-5", "claude-sonnet-5-5", "closed"]
    asked.clear()
    bad = _settings(llm_provider="anthropic", anthropic_api_key="k",
                    vision_model="claude-haiku-5-5")
    with pytest.raises(anthropic.APIStatusError):
        await wiring.check_provider(bad)
    assert asked[-1] == "closed"
    await wiring.check_provider(_settings())          # OpenAI — без проверки и без сети


def test_путь_openai_не_тянет_sdk_anthropic():
    import subprocess
    import sys

    code = ("import sys, app.container, app.bot.handlers, app.infrastructure.nlu.openai_nlu, "
            "app.infrastructure.vision.openai_vision; print('anthropic' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, cwd=Path(__file__).resolve().parents[2])
    assert out.stdout.strip() == "False"


async def test_eval_считает_отказы_отдельно():
    meter = EvalMeter(max_usd=1.0)
    client = Client(_msg("", stop="refusal"))
    await ClaudeNlu(client, "claude-opus-5-5", sample_k=1, meter=meter).parse("x", [])
    assert meter.summary(1)["stopped_early"] == {"refusal:cyber": 1}


# --- путь OpenAI — байт в байт прежний ---------------------------------------------------


def test_промпт_разбора_байт_в_байт_прежний():
    from app.infrastructure.nlu.prompts import build_user_prompt

    assert build_user_prompt("кофе", ["a", "b"]) == (
        "Известные canonical_key (переиспользуй, только если тот же продукт): a, b\n\n"
        "Сообщение пользователя: 'кофе'")
    assert "(пусто)" in build_user_prompt("x", [])


async def test_openai_vision_запросы_прежние():
    """После выноса транспорта в `_parse` аргументы вызова OpenAI — ровно те, что были."""
    import base64

    from openai import Omit

    from app.infrastructure.vision import openai_vision as ov

    calls: list[dict] = []

    async def parse(**kw):
        calls.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=None))],
                               usage=None)

    client = SimpleNamespace(beta=SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(parse=parse))))
    vision = ov.OpenAiVision(client, "gpt-5.1")
    img = b"\xff\xd8img"
    url = f"data:image/jpeg;base64,{base64.b64encode(img).decode()}"
    await vision.read_photo(img, "  420 ")
    await vision.read_scan(img)
    await vision.read_label(img)

    expected = [
        (ov.PHOTO_PROMPT, "Подпись человека: «420»", {"url": url, "detail": "high"},
         ov.PhotoReadingOut),
        (ov.SCAN_PROMPT, "Перепиши числа с этого бланка.", {"url": url, "detail": "high"},
         ov.ScanReadingOut),
        (ov.SYSTEM_PROMPT, "Прочитай таблицу КБЖУ с этой этикетки.", {"url": url},
         ov.LabelReading),
    ]
    for kw, (system, text, picture, schema) in zip(calls, expected, strict=True):
        assert set(kw) == {"model", "temperature", "messages", "response_format"}
        assert kw["model"] == "gpt-5.1" and isinstance(kw["temperature"], Omit)
        assert kw["response_format"] is schema
        assert kw["messages"] == [
            {"role": "system", "content": system},
            {"role": "user", "content": [{"type": "text", "text": text},
                                         {"type": "image_url", "image_url": picture}]},
        ]


# --- настоящий SDK: фото и ошибка посреди потока -----------------------------------------


async def test_sdk_запрос_фото():
    seen: list = []
    plate = _wire([{"type": "text", "text": _plate_json()}])
    vision = ClaudeVision(_sdk([plate], seen), "claude-opus-5-5")
    assert (await vision.read_photo(b"\xff\xd8\xffjpeg", "")).kind == "dish"
    _, body = seen[0]
    assert not set(body) & set(SAMPLING)
    image, text = body["messages"][0]["content"]
    assert image["source"] == {"type": "base64", "media_type": "image/jpeg",
                               "data": "/9j/anBlZw=="}
    assert text == {"type": "text", "text": "Подписи нет."}
    assert body["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert body["output_config"]["format"]["type"] == "json_schema"


async def test_sdk_ошибка_посреди_потока_это_перегрузка_и_обрывок_учтён():
    """SSE `event: error` приходит после 200: SDK бросает APIStatusError со статусом 200 —
    узнаём перегрузку по типу; уже потраченное по снимку потока — в учёт."""
    seen: list = []
    broken = _sse(
        {"type": "message_start", "message": _wire([])},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "нача"}},
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    )
    meter = Meter()
    coach = ClaudeCoach(_sdk([broken], seen), "claude-opus-5-5", meter)
    with pytest.raises(anthropic.APIStatusError) as caught:
        await _collect(coach.stream("?", "", Data()))
    assert llm_errors.overloaded(caught.value) and not llm_errors.provider_down(caught.value)
    from app.bot import coach_chat

    assert coach_chat._failure(caught.value) == coach_chat.OVERLOADED
    assert meter.notes and meter.notes[0]["input_tokens"] == 130
