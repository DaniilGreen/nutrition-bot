"""Реализация CoachPort на OpenAI: свободный ответ текстом плюс инструменты (§6.21).

Модель сама решает, какие данные ей нужны, и запрашивает их вызовом инструмента.
Схемы инструментов живут здесь, потому что их формат — деталь провайдера; сами
данные отдаёт `CoachData` из application, привязанный к человеку (MA-INV-39).
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace
from typing import Any, Protocol

from openai import AsyncOpenAI

from app.application.coach import CoachChunk, Turn
from app.domain.spend import Usage
from app.infrastructure.openai_compat import token_counts

log = logging.getLogger(__name__)

# MA-INV-40: потолок обращений к модели на один вопрос. Было ровно одно, стало
# до четырёх — без потолка вопрос мог бы крутиться, пока не кончится бюджет.
MAX_ROUNDS = 4

SYSTEM = """Ты — помощник по дневнику питания. Отвечай на русском, коротко и по делу.

У тебя ЕСТЬ доступ к дневнику этого человека через инструменты. Стартовая сводка —
только общий фон, а не все данные. Если вопрос касается конкретной еды, продуктов,
отдельных дней или веса — СНАЧАЛА посмотри инструментом, потом отвечай. Не спрашивай
разрешения и никогда не говори, что у тебя нет доступа: он есть, просто посмотри.

Правила:
- Опирайся ТОЛЬКО на данные из инструментов и переданные цифры. Не выдумывай.
- Не считай калорийность блюд — это делает приложение. Твоё дело объяснять цифры.
- Не ставь диагнозов и не назначай лечения. Ты не врач.
- Если данных мало для вывода, так и скажи, а не сочиняй.
- Без нравоучений и оценок вроде «плохо» или «ты переел». Факты и следствия.
- Называя продукт, пиши его ровно так, как он записан в дневнике.
- 2–5 предложений. Списком — только если пунктов правда несколько.
- Выше могут быть прошлые реплики этого разговора. Короткий вопрос вроде «разложи» или
  «посмотри ещё раз» — продолжение прошлого ответа, а не новая тема.
- Из разметки — только **жирный**. Заголовков, таблиц и курсива не пиши."""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "day_totals",
            "description": "Съеденное по дням: ккал, белок, жир, углеводы, число приёмов. "
                           "Дни без записей тоже возвращаются — по ним видно пропуски.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Сколько последних дней, 1–90."}
                },
                "required": ["days"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "meals_on",
            "description": "Приёмы конкретного дня: время, что человек записал, "
                           "позиции с граммовкой и калориями.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Дата YYYY-MM-DD."}
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "top_products",
            "description": "Какие продукты дают больше всего калорий (или белка, жира, "
                           "углеводов) за окно. Это вклад, а не частота.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Окно в днях, 1–90."},
                    "by": {"type": "string", "enum": ["kcal", "protein", "fat", "carb"]},
                },
                "required": ["days", "by"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "accuracy",
            "description": "Насколько цифрам дневника можно верить: какая доля калорий "
                           "посчитана оценкой модели, а не взята с этикетки, и какие "
                           "продукты дают эту долю.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Окно в днях, 1–90."}
                },
                "required": ["days"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "weight_log",
            "description": "Замеры веса с датами, свежие первыми.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Сколько замеров, 1–60."}
                },
                "required": ["limit"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "body_scans",
            "description": (
                "Замеры состава тела с бланка InBody: вес, жир (кг и %), безжировая масса, "
                "скелетные мышцы, мышечный индекс, балл прибора; плюс темп изменения жира и "
                "безжировой массы в месяц с погрешностью прибора. Этим отвечают на вопросы "
                "«теряю ли мышцы» и «из чего ушёл вес»."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Сколько замеров, 1–20."}
                },
                "required": ["limit"],
            },
        },
    },
]


class CoachData(Protocol):
    """Что адаптер вправе спросить. Пользователя в сигнатурах нет (MA-INV-39)."""

    async def day_totals(self, days: Any = 14) -> Any: ...
    async def meals_on(self, date_str: Any = None) -> Any: ...
    async def top_products(self, days: Any = 30, by: Any = "kcal") -> Any: ...
    async def weight_log(self, limit: Any = 14) -> Any: ...
    async def body_scans(self, limit: Any = 6) -> Any: ...
    async def accuracy(self, days: Any = 30) -> Any: ...


class OpenAiCoach:
    def __init__(self, client: AsyncOpenAI, model: str, meter: object | None = None) -> None:
        self._client = client
        self._model = model
        # Расход пишется после КАЖДОГО раунда своей сессией (AI-PRINCIPLES §2). Раньше он
        # уходил одной строкой в конце ответа через транзакцию запроса, и оборванный поток
        # терял уже оплаченные раунды.
        self._meter = meter

    async def _note(self, tokens_in: int, cached: int, tokens_out: int) -> None:
        """Сбой счётчика не роняет ответ: недосчитать цент дешевле, чем потерять ответ."""
        if self._meter is None:
            return
        try:
            await self._meter.note(model=self._model, input_tokens=tokens_in,
                                   output_tokens=tokens_out, cached_tokens=cached)
        except Exception:  # noqa: BLE001
            log.warning("не удалось записать расход ассистента", exc_info=True)

    async def ask(
        self, question: str, facts: str, data: CoachData | None = None,
        history: Sequence[Turn] = (),
    ) -> tuple[str, Usage]:
        messages = _opening(question, facts, history)
        # Токены копим по ВСЕМ раундам: учесть только последний значило бы
        # недосчитать в разы, и лимит $5 перестал бы ограничивать (MA-INV-40).
        tokens_in = tokens_out = cached = 0
        text = ""

        for round_no in range(MAX_ROUNDS):
            last = round_no == MAX_ROUNDS - 1
            kwargs: dict[str, Any] = {"model": self._model, "messages": messages}
            # На последнем раунде инструментов не предлагаем вовсе: модель обязана
            # ответить словами, иначе человек не получит ничего.
            if data is not None and not last:
                kwargs["tools"] = TOOLS
            resp = await self._client.chat.completions.create(**kwargs)

            add_in, add_cached, add_out = token_counts(resp.usage)
            tokens_in += add_in
            cached += add_cached
            tokens_out += add_out
            await self._note(add_in, add_cached, add_out)

            msg = resp.choices[0].message
            text = (msg.content or "").strip()
            # Фильтр по типу — из finance-bot. В SDK `tool_calls` это union, и
            # поле `function` есть только у функциональных вызовов; чужой вариант
            # мы не заказывали и молча пропускаем, а не падаем на нём.
            calls = [
                c for c in (getattr(msg, "tool_calls", None) or [])
                if getattr(c, "type", "function") == "function"
            ]
            if not calls:
                break

            messages.append(
                {"role": "assistant", "content": msg.content,
                 "tool_calls": [_call_json(c) for c in calls]}
            )
            for call in calls:
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": await _run(data, call),
                })

        usage = Usage(self._model, tokens_in, tokens_out, cached)
        return text or "Не нашёл, что ответить по этим данным.", usage


    async def stream(
        self, question: str, facts: str, data: CoachData | None = None,
        history: Sequence[Turn] = (),
    ) -> AsyncIterator[CoachChunk]:
        """Тот же цикл, но текст отдаётся по мере генерации (§6.21.1).

        Куски текста отдаём сразу, не дожидаясь конца раунда. Если раунд всё же
        закончился вызовом инструмента, шлём `reset`: модель написала преамбулу
        и передумала, и эта преамбула не должна висеть над настоящим ответом.
        """
        messages = _opening(question, facts, history)
        tokens_in = tokens_out = cached = 0

        for round_no in range(MAX_ROUNDS):
            last = round_no == MAX_ROUNDS - 1
            kwargs: dict[str, Any] = {
                "model": self._model,
                "messages": messages,
                "stream": True,
                # Без этого в потоке расхода нет вовсе, и лимит перестаёт считать.
                "stream_options": {"include_usage": True},
            }
            if data is not None and not last:
                kwargs["tools"] = TOOLS

            said: list[str] = []
            calls: dict[int, dict[str, str]] = {}
            round_in = round_cached = round_out = 0
            async for event in await self._client.chat.completions.create(**kwargs):
                u = getattr(event, "usage", None)
                if u is not None:
                    add_in, add_cached, add_out = token_counts(u)
                    round_in += add_in
                    round_cached += add_cached
                    round_out += add_out
                if not getattr(event, "choices", None):
                    continue
                delta = event.choices[0].delta
                piece = getattr(delta, "content", None)
                if piece:
                    said.append(piece)
                    yield CoachChunk(kind="delta", text=piece)
                for tc in getattr(delta, "tool_calls", None) or []:
                    if getattr(tc, "type", "function") not in ("function", None):
                        continue
                    # Вызов приходит по кускам и собирается по индексу: имя и
                    # аргументы могут приехать разными событиями.
                    slot = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        slot["name"] += fn.name or ""
                        slot["args"] += fn.arguments or ""

            # Раунд закончился — его токены записаны сразу: закроет человек аппку на
            # следующем раунде, этот уже учтён.
            tokens_in += round_in
            cached += round_cached
            tokens_out += round_out
            await self._note(round_in, round_cached, round_out)

            if not calls:
                break

            if said:
                yield CoachChunk(kind="reset")
            content = "".join(said)
            messages.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": c["args"]}}
                    for c in calls.values()
                ],
            })
            for slot in calls.values():
                yield CoachChunk(kind="tool", text=slot["name"])
                messages.append({
                    "role": "tool",
                    "tool_call_id": slot["id"],
                    "content": await _run(data, _slot_call(slot)),
                })

        yield CoachChunk(
            kind="end",
            usage=Usage(self._model, tokens_in, tokens_out, cached),
        )


def _opening(question: str, facts: str, history: Sequence[Turn]) -> list[dict[str, Any]]:
    """Начало разговора: системный промпт, прошлые реплики открытого экрана, вопрос.

    Сводка фактов — только у текущего вопроса: она свежая, а у прошлых реплик была бы
    устаревшей копией (MA-INV-66). Реплики — простой текст, без вызовов инструментов:
    за данными модель сходит заново."""
    return [
        {"role": "system", "content": SYSTEM},
        *({"role": role, "content": text} for role, text in history),
        {"role": "user", "content": f"Мои данные:\n{facts}\n\nВопрос: {question}"},
    ]


def _slot_call(slot: dict[str, str]) -> Any:
    """Собранный из кусков вызов в той же форме, что у нестримового ответа."""
    return SimpleNamespace(
        id=slot["id"],
        function=SimpleNamespace(name=slot["name"], arguments=slot["args"]),
    )


def _call_json(call: Any) -> dict[str, Any]:
    return {
        "id": call.id,
        "type": "function",
        "function": {"name": call.function.name, "arguments": call.function.arguments},
    }


async def _run(data: CoachData | None, call: Any) -> str:
    """Выполнить вызов инструмента. Любой сбой возвращается модели текстом,
    а не исключением: упасть на кривом аргументе значит потерять весь ответ."""
    name = call.function.name
    try:
        args = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        args = {}
    try:
        if data is None:
            result: Any = {"error": "данные недоступны"}
        elif name == "day_totals":
            result = await data.day_totals(args.get("days", 14))
        elif name == "meals_on":
            result = await data.meals_on(args.get("date"))
        elif name == "top_products":
            result = await data.top_products(args.get("days", 30), args.get("by", "kcal"))
        elif name == "weight_log":
            result = await data.weight_log(args.get("limit", 14))
        elif name == "body_scans":
            result = await data.body_scans(args.get("limit", 6))
        elif name == "accuracy":
            result = await data.accuracy(args.get("days", 30))
        else:
            # Модель выдумала инструмент — говорим ей об этом, а не падаем.
            result = {"error": f"нет такого инструмента: {name}"}
    except Exception as exc:  # noqa: BLE001 — сбой одного инструмента не рушит ответ
        log.warning("инструмент ассистента %s упал: %s", name, exc, exc_info=True)
        result = {"error": "не получилось получить эти данные"}
    return json.dumps(result, ensure_ascii=False)
