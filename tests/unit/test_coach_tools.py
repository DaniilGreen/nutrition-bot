"""§6.21: цикл инструментов ассистента.

Раньше вызов был один, сломаться было негде. Теперь их до четырёх, и цена
вопроса зависит от их числа — значит проверяем ровно то, что стоит денег:
расход суммируется по всем раундам, раунды кончаются, сбой инструмента
не съедает ответ.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.infrastructure.nlu.openai_coach import MAX_ROUNDS, OpenAiCoach


def _call(name: str, args: dict, cid: str = "c1"):
    return SimpleNamespace(
        id=cid, type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def _resp(content=None, calls=None, tin=0, tout=0):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))],
        usage=SimpleNamespace(prompt_tokens=tin, completion_tokens=tout),
    )


class FakeClient:
    """Отдаёт заготовленные ответы по очереди и запоминает запросы."""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._script.pop(0)


class FakeData:
    def __init__(self):
        self.seen: list[tuple[str, tuple]] = []

    async def day_totals(self, days=14):
        self.seen.append(("day_totals", (days,)))
        return [{"date": "2026-09-07", "kcal": 434, "protein_g": 32}]

    async def meals_on(self, date_str=None):
        self.seen.append(("meals_on", (date_str,)))
        return {"date": date_str, "meals": [{"at": "12:00", "kcal": 300}]}

    async def top_products(self, days=30, by="kcal"):
        self.seen.append(("top_products", (days, by)))
        return [{"name": "молоко 3.2%", "total": 2850, "unit": by}]

    async def weight_log(self, limit=14):
        self.seen.append(("weight_log", (limit,)))
        return [{"date": "2026-08-28", "kg": 92.1}]


async def test_модель_видит_данные_через_инструмент():
    """Главное, ради чего всё затевалось: названия еды доходят до модели."""
    client = FakeClient([
        _resp(calls=[_call("top_products", {"days": 30, "by": "kcal"})], tin=500, tout=20),
        _resp(content="Больше всего дают молоко и протеин.", tin=800, tout=40),
    ])
    data = FakeData()
    text, usage = await OpenAiCoach(client, "gpt-5.1").ask("откуда калории", "цифры", data)

    assert data.seen == [("top_products", (30, "kcal"))]
    assert text == "Больше всего дают молоко и протеин."
    # Результат инструмента вернулся модели именно как tool-сообщение.
    tool_msgs = [m for m in client.calls[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "молоко 3.2%" in tool_msgs[0]["content"]


async def test_расход_суммируется_по_всем_раундам():
    """Деньги. Учесть только последний вызов — недосчитать в разы, и лимит
    в пять долларов перестаёт ограничивать (MA-INV-40)."""
    client = FakeClient([
        _resp(calls=[_call("day_totals", {"days": 7})], tin=500, tout=20),
        _resp(calls=[_call("weight_log", {"limit": 5}, cid="c2")], tin=900, tout=30),
        _resp(content="Готово.", tin=1400, tout=60),
    ])
    _, usage = await OpenAiCoach(client, "gpt-5.1").ask("почему вес стоит", "цифры", FakeData())

    assert usage.input_tokens == 500 + 900 + 1400
    assert usage.output_tokens == 20 + 30 + 60
    assert usage.cost_usd > 0


async def test_раунды_кончаются_и_ответ_всё_равно_есть():
    """Модель, которая только зовёт инструменты, не должна крутиться вечно
    и не должна оставить человека без ответа."""
    script = [_resp(content="Смотрю…", calls=[_call("day_totals", {"days": 7}, cid=f"c{i}")],
                    tin=100, tout=10) for i in range(MAX_ROUNDS - 1)]
    script.append(_resp(content="Ответ по данным.", tin=100, tout=10))
    client = FakeClient(script)
    text, _ = await OpenAiCoach(client, "gpt-5.1").ask("q", "цифры", FakeData())

    assert len(client.calls) == MAX_ROUNDS
    assert text == "Ответ по данным."
    # На последнем раунде инструменты не предлагаются: модель обязана ответить словами.
    assert "tools" not in client.calls[-1]
    assert "tools" in client.calls[0]


async def test_выдуманный_инструмент_не_роняет_ответ():
    client = FakeClient([
        _resp(calls=[_call("посчитай_калории_борща", {"x": 1})], tin=100, tout=10),
        _resp(content="Такого я не умею, но вот цифры.", tin=200, tout=20),
    ])
    text, _ = await OpenAiCoach(client, "gpt-5.1").ask("q", "цифры", FakeData())
    tool_msg = [m for m in client.calls[1]["messages"] if m.get("role") == "tool"][0]
    assert "нет такого инструмента" in tool_msg["content"]
    assert text == "Такого я не умею, но вот цифры."


async def test_кривые_аргументы_не_роняют_ответ():
    """Аргументы приходят от модели — это ввод, а не константа."""
    broken = SimpleNamespace(
        id="c1", type="function",
        function=SimpleNamespace(name="day_totals", arguments="{это не json"),
    )
    client = FakeClient([
        _resp(calls=[broken], tin=100, tout=10),
        _resp(content="Вот ответ.", tin=200, tout=20),
    ])
    data = FakeData()
    text, _ = await OpenAiCoach(client, "gpt-5.1").ask("q", "цифры", data)
    assert data.seen == [("day_totals", (14,))]   # упали на дефолт, не на исключение
    assert text == "Вот ответ."


async def test_упавший_инструмент_говорит_модели_а_не_взрывается():
    class Broken(FakeData):
        async def top_products(self, days=30, by="kcal"):
            raise RuntimeError("база отвалилась")

    client = FakeClient([
        _resp(calls=[_call("top_products", {"days": 30, "by": "kcal"})], tin=100, tout=10),
        _resp(content="Данных нет, отвечаю по общим цифрам.", tin=200, tout=20),
    ])
    text, _ = await OpenAiCoach(client, "gpt-5.1").ask("q", "цифры", Broken())
    tool_msg = [m for m in client.calls[1]["messages"] if m.get("role") == "tool"][0]
    assert "не получилось" in tool_msg["content"]
    assert text.startswith("Данных нет")


async def test_без_данных_инструменты_не_предлагаются():
    """Нечем обслужить вызов — не зови. Иначе модель получит инструмент,
    на который нечего ответить."""
    client = FakeClient([_resp(content="Ответ.", tin=100, tout=10)])
    text, _ = await OpenAiCoach(client, "gpt-5.1").ask("q", "цифры", None)
    assert "tools" not in client.calls[0]
    assert text == "Ответ."


@pytest.mark.parametrize("name", ["day_totals", "meals_on", "top_products", "weight_log"])
def test_в_схеме_инструмента_нет_пользователя(name):
    """MA-INV-39: модель не может попросить чужие данные, потому что такого
    аргумента не существует — не потому, что мы его проверяем."""
    from app.infrastructure.nlu.openai_coach import TOOLS

    tool = next(t for t in TOOLS if t["function"]["name"] == name)
    props = tool["function"]["parameters"]["properties"]
    assert not any("user" in k or "telegram" in k or "id" == k for k in props), props
