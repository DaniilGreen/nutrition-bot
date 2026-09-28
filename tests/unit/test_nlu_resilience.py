"""AI-PRINCIPLES §1 и §2: разбор переживает сеть и считает свои деньги.

Обе дыры найдены сверкой с finance-bot 08.09.2026. Там ретрай был с самого начала,
а расход считался целиком; в burnfat не было ни того, ни другого — сетевой сбой
терял приём, а счётчик «$0.03 из $5» показывал только ассистента, при том что
разбор стоит пяти вызовов на каждый приём с оценкой.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.application.dto import Intent
from app.infrastructure.nlu.openai_nlu import RETRIES, OpenAiNlu


def _ok(intent="meal"):
    parsed = SimpleNamespace(
        intent=intent, weight_kg=None, correction_op=None, day_offset=0,
        confidence=0.9, items=[],
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))],
        usage=SimpleNamespace(prompt_tokens=1500, completion_tokens=200),
    )


class FlakyClient:
    """Падает заданное число раз, потом отвечает."""

    def __init__(self, fails: int, response=None):
        self.fails = fails
        self.tries = 0
        self._response = response or _ok()
        self.chat = SimpleNamespace(completions=self)
        self.beta = SimpleNamespace(chat=SimpleNamespace(completions=self))

    async def parse(self, **kwargs):
        self.tries += 1
        if self.tries <= self.fails:
            raise TimeoutError("сеть моргнула")
        return self._response


class Meter:
    def __init__(self):
        self.notes = []

    async def note(self, *, model, input_tokens, output_tokens, cached_tokens=0):
        self.notes.append((model, input_tokens, output_tokens))
        self.cached = cached_tokens


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Бэкофф настоящий, но ждать его в тесте незачем."""
    import app.infrastructure.nlu.openai_nlu as m

    async def instant(_):
        return None

    monkeypatch.setattr(m.asyncio, "sleep", instant)


async def test_моргнувшая_сеть_не_теряет_приём():
    """Один сбой — и запись всё равно доходит. Раньше приём терялся молча."""
    client = FlakyClient(fails=1)
    nlu = OpenAiNlu(client, "gpt-5.1", sample_k=1)
    parsed = await nlu.parse("200 г куриной грудки", [])
    assert parsed.intent is Intent.MEAL
    assert client.tries == 2


async def test_ретраи_кончаются_и_ошибка_идёт_наверх():
    """Исключение обязано подняться: вызывающий сохранит сырой ввод и предложит
    повтор. Проглоченное сообщение — это потерянная запись."""
    client = FlakyClient(fails=99)
    nlu = OpenAiNlu(client, "gpt-5.1", sample_k=1)
    with pytest.raises(TimeoutError):
        await nlu.parse("курица", [])
    assert client.tries == 1 + RETRIES


async def test_расход_разбора_записывается():
    """До 08.09.2026 разбор не считался вовсе, хотя это основная статья трат."""
    meter = Meter()
    nlu = OpenAiNlu(FlakyClient(fails=0), "gpt-5.1", sample_k=1, meter=meter)
    await nlu.parse("курица", [])
    assert meter.notes == [("gpt-5.1", 1500, 200)]


async def test_каждый_сэмпл_считается_отдельно():
    """Пять сэмплов — пять вызовов и пять записей расхода. Считать только первый
    значило бы недосчитать в пять раз ровно на самой дорогой статье."""
    meter = Meter()
    item = SimpleNamespace(
        name="борщ", canonical_key="borsch", unit="g", quantity=None, grams=300.0,
        portion_hint=None, portion_small_g=None, portion_medium_g=None,
        portion_large_g=None, unit_weight_g=None, kcal_per_100g=45.0,
        zero_calorie=False, protein_per_100g=2.0, fat_per_100g=2.0, carb_per_100g=4.0,
        total_kcal=None, total_protein=None, total_fat=None, total_carb=None,
        clarify_options=None, clarify_kcal_per_100g=None, confidence=0.9,
    )
    resp = _ok()
    resp.choices[0].message.parsed.items = [item]
    nlu = OpenAiNlu(FlakyClient(fails=0, response=resp), "gpt-5.1", sample_k=5, meter=meter)
    samples = await nlu.parse_samples("борщ", [])
    assert len(samples) == 5
    assert len(meter.notes) == 5


async def test_сбой_счётчика_не_роняет_разбор():
    """Недосчитать цент дешевле, чем потерять приём."""
    class Broken:
        async def note(self, **kwargs):
            raise RuntimeError("база отвалилась")

    nlu = OpenAiNlu(FlakyClient(fails=0), "gpt-5.1", sample_k=1, meter=Broken())
    parsed = await nlu.parse("курица", [])
    assert parsed.intent is Intent.MEAL


# --- INV-12: сэмплы только для нового продукта (28.09.2026) -----------------


def _meal(key: str, *, options=None):
    item = SimpleNamespace(
        name="борщ", canonical_key=key, unit="g", quantity=None, grams=300.0,
        portion_hint=None, portion_small_g=None, portion_medium_g=None,
        portion_large_g=None, unit_weight_g=None, kcal_per_100g=45.0,
        zero_calorie=False, protein_per_100g=2.0, fat_per_100g=2.0, carb_per_100g=4.0,
        total_kcal=None, total_protein=None, total_fat=None, total_carb=None,
        clarify_options=options, clarify_kcal_per_100g=None, confidence=0.9,
    )
    resp = _ok()
    resp.choices[0].message.parsed.items = [item]
    return resp


async def test_знакомый_продукт_не_сэмплируется():
    """Плотность знакомого продукта берётся из каталога (INV-2): медиана семи
    сэмплов выбрасывалась бы. До 28.09.2026 это были 82% вызовов разбора."""
    client = FlakyClient(fails=0, response=_meal("borsch"))
    nlu = OpenAiNlu(client, "gpt-5.1", sample_k=7)
    samples = await nlu.parse_samples("борщ", ["borsch", "rice"])
    assert len(samples) == 1 and client.tries == 1


async def test_новый_продукт_сэмплируется_k_раз():
    client = FlakyClient(fails=0, response=_meal("borsch_green"))
    nlu = OpenAiNlu(client, "gpt-5.1", sample_k=7)
    samples = await nlu.parse_samples("зелёный борщ", ["borsch", "rice"])
    assert len(samples) == 7 and client.tries == 7


async def test_позиция_с_вариантами_не_сэмплируется():
    """После выбора варианта её всё равно переразберут — сэмплы ушли бы впустую."""
    client = FlakyClient(fails=0, response=_meal("soup", options=["борщ", "щи"]))
    nlu = OpenAiNlu(client, "gpt-5.1", sample_k=7)
    assert len(await nlu.parse_samples("суп", [])) == 1


async def test_упавший_сэмпл_не_отменяет_остальные(monkeypatch):
    """Раньше один исчерпавший повторы сэмпл ронял весь разбор вместе с уже
    оплаченными — человек присылал запись заново, и вызовов выходило 14."""
    nlu = OpenAiNlu(FlakyClient(fails=0, response=_meal("borsch_green")), "gpt-5.1", sample_k=4)
    real = nlu._parse_at
    calls = {"n": 0}

    async def flaky(text, keys, temperature):
        calls["n"] += 1
        if calls["n"] == 2:                      # первый из сэмплов
            raise TimeoutError("этот сэмпл исчерпал повторы")
        return await real(text, keys, temperature)

    monkeypatch.setattr(nlu, "_parse_at", flaky)
    samples = await nlu.parse_samples("зелёный борщ", [])
    assert len(samples) == 3 and samples[0].intent is Intent.MEAL


async def test_параметры_кэша_уходят_у_gpt5_и_не_уходят_у_41():
    from app.infrastructure.openai_compat import request_extras

    assert request_extras("gpt-5.1", "k") == {
        "reasoning_effort": "none", "prompt_cache_retention": "24h", "prompt_cache_key": "k"}
    assert request_extras("gpt-5.4-mini")["reasoning_effort"] == "none"
    # У gpt-4.1 рассуждений нет — reasoning_effort дал бы 400.
    assert request_extras("gpt-4.1-mini", "k") == {"prompt_cache_key": "k"}

    seen = {}

    class Spy(FlakyClient):
        async def parse(self, **kwargs):
            seen.update(kwargs)
            return self._response

    await OpenAiNlu(Spy(fails=0), "gpt-5.1", sample_k=1).parse("курица", [])
    assert seen["extra_body"]["prompt_cache_retention"] == "24h"


# --- порог вопроса — у приложения (FR-20, 28.09.2026) ------------------------


async def test_варианты_с_малой_разницей_не_спрашиваются_и_сэмплируются():
    """Грудка отварная 137 / гриль 165 — разница 1.2×: вопроса нет. И раз вопроса нет,
    новый продукт идёт в сэмплы, а не пиннится по одной оценке."""
    resp = _meal("chicken_breast_x", options=["отварная", "гриль"])
    resp.choices[0].message.parsed.items[0].clarify_kcal_per_100g = [137, 165]
    client = FlakyClient(fails=0, response=resp)
    samples = await OpenAiNlu(client, "gpt-5.1", sample_k=3).parse_samples("грудка", [])
    assert samples[0].items[0].clarify_options is None
    assert len(samples) == 3


async def test_варианты_с_большой_разницей_спрашиваются():
    resp = _meal("tofu_x", options=["мягкий", "плотный"])
    resp.choices[0].message.parsed.items[0].clarify_kcal_per_100g = [55, 145]
    parsed = await OpenAiNlu(FlakyClient(fails=0, response=resp), "gpt-5.1").parse("тофу", [])
    assert parsed.items[0].clarify_options == ["мягкий", "плотный"]
