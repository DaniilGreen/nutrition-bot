"""Реализация CoachPort через Claude (D-41, NFR-6; AI-PRINCIPLES §5, §6, §9).

Тот же системный промпт, те же инструменты и тот же `CoachData`, что у `OpenAiCoach`:
меняется только транспорт. Отличия, на которых легко споткнуться:

- инструменты не меняются между раундами: смена `tools` посреди ответа у Claude — промах
  кэша и 400 на новых аккаунтах (рассуждения привязаны к беседе). На последнем раунде они
  остаются в запросе, а звать их запрещено (`tool_choice: none`);
- ответ модели возвращается в историю как пришёл — с блоками рассуждений;
- в потоке у инструментов `eager_input_streaming`, и сервер аргументы не проверяет: не
  словарь — ошибка модели текстом, оборванный `max_tokens` вызов не исполняется;
- отказ — это `stop_reason: refusal`, а не исключение: напечатанное стираем, человеку — фраза;
- кэш пятиминутный (раунды идут секундами, D-41): метка на системе плюс автоматическая на
  хвосте беседы — раунды 2–4 читают историю из кэша, а не платят за неё заново.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace
from typing import Any

from app.application.coach import CoachChunk, Turn
from app.domain.spend import Usage
from app.infrastructure.claude_compat import (
    CACHE_5M,
    answer_text,
    cached_text,
    meter_fields,
    request_params,
)
from app.infrastructure.nlu.openai_coach import (
    MAX_ROUNDS,
    SYSTEM,
    TOOLS,
    CoachData,
    _opening,
    _run,
)

log = logging.getLogger(__name__)

# Ответ — 2–5 предложений, но рассуждения входят в лимит (на medium их бывает много); потолок
# заодно держит цену раунда: $0.32 на Opus 5.5 в худшем случае.
MAX_TOKENS = 16000
# SDK не смог разобрать JSON аргумента вовсе (бывает с eager-потоком) — раунд повторяем, но не
# бесконечно: у вызова нет id, ответить модели ошибкой не на что.
JSON_RETRIES = 2
REFUSED = "С этим я не помогу. Спроси по-другому — например, про свои записи и цифры."
EMPTY = "Не нашёл, что ответить по этим данным."
# AC-265: обрывок по max_tokens не выдаём молча за ответ.
CUT = "✂️ Ответ оборвался — спроси короче."


def _claude_tools(*, eager: bool) -> list[dict[str, Any]]:
    """Схемы инструментов OpenAI → формат Claude. Порядок прежний и неизменный: инструменты
    стоят в самом начале префикса, и любая перестановка сбросила бы кэш."""
    out = []
    for t in TOOLS:
        fn = t["function"]
        tool: dict[str, Any] = {
            "name": fn["name"], "description": fn["description"],
            "input_schema": fn["parameters"],
        }
        if eager:
            # Без этого сервер буферизует аргумент целиком. Цена — проверка на нашей стороне.
            tool["eager_input_streaming"] = True
        out.append(tool)
    return out


TOOLS_ASK = _claude_tools(eager=False)
TOOLS_STREAM = _claude_tools(eager=True)


class ClaudeCoach:
    def __init__(
        self, client: Any, model: str, meter: object | None = None, *, effort: str = "medium",
    ) -> None:
        self._client = client
        self._model = model
        self._meter = meter
        # Рассуждения адаптивные: ассистент планирует вызовы инструментов, а у Opus 5.5 их и не
        # выключить — глубину держит effort.
        self._params = request_params(model, effort)

    def _request(
        self, messages: list[dict[str, Any]], data: CoachData | None, last: bool, *, stream: bool,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": MAX_TOKENS,
            # Метка на системе кэширует и инструменты: они рендерятся раньше неё.
            "system": [cached_text(SYSTEM, CACHE_5M)],
            # Автоматическая метка на последнем блоке: следующий раунд читает историю из кэша.
            "cache_control": dict(CACHE_5M),
            "messages": messages,
            **self._params,
        }
        if data is not None:
            kwargs["tools"] = TOOLS_STREAM if stream else TOOLS_ASK
            if last:
                # MA-INV-40: на последнем раунде модель обязана ответить словами.
                kwargs["tool_choice"] = {"type": "none"}
        return kwargs

    async def _note(self, usage: object) -> Counter[str]:
        """Расход раунда — сразу своей сессией (AI-PRINCIPLES §2). Сбой счётчика не роняет ответ."""
        fields = meter_fields(usage)
        if self._meter is not None:
            try:
                await self._meter.note(model=self._model, **fields)
            except Exception:  # noqa: BLE001
                log.warning("не удалось записать расход ассистента", exc_info=True)
        return Counter(fields)

    async def ask(
        self, question: str, facts: str, data: CoachData | None = None,
        history: Sequence[Turn] = (),
    ) -> tuple[str, Usage]:
        messages = _messages(question, facts, history)
        total: Counter[str] = Counter()
        text = ""
        for round_no in range(MAX_ROUNDS):
            last = round_no == MAX_ROUNDS - 1
            resp = await self._client.messages.create(
                **self._request(messages, data, last, stream=False)
            )
            total += await self._note(resp.usage)
            if resp.stop_reason == "refusal":
                text = REFUSED
                break
            text = answer_text(resp).strip()
            if resp.stop_reason == "max_tokens":
                text = f"{text}\n\n{CUT}" if text else CUT
                break
            calls = _calls(resp)
            if not calls:
                break
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": [await _result(data, c) for c in calls]})
        return text or EMPTY, _usage(self._model, total)

    async def stream(
        self, question: str, facts: str, data: CoachData | None = None,
        history: Sequence[Turn] = (),
    ) -> AsyncIterator[CoachChunk]:
        """Тот же цикл кусками (§6.21.1): delta по мере генерации, reset — если раунд кончился
        вызовом инструмента или отказом, tool — куда модель полезла, end — расход всех раундов."""
        messages = _messages(question, facts, history)
        total: Counter[str] = Counter()
        for round_no in range(MAX_ROUNDS):
            last = round_no == MAX_ROUNDS - 1
            kwargs = self._request(messages, data, last, stream=True)
            said: list[str] = []
            final = None
            for attempt in range(1 + JSON_RETRIES):
                stream = None
                try:
                    async with self._client.messages.stream(**kwargs) as stream:
                        async for event in stream:
                            if getattr(event, "type", None) == "text" and event.text:
                                said.append(event.text)
                                yield CoachChunk(kind="delta", text=event.text)
                        final = await stream.get_final_message()
                    break
                except Exception as exc:
                    # Поток оборвался посреди ответа (кривой JSON аргумента, `event: error`,
                    # сеть): уже потраченное — в учёт по снимку потока, иначе оплаченный обрывок
                    # не попал бы в лимит (AI-PRINCIPLES §2).
                    snap = _snapshot_usage(stream)
                    if snap is not None:
                        total += await self._note(snap)
                    if not isinstance(exc, ValueError) or attempt == JSON_RETRIES:
                        raise
                    # ValueError — SDK не разобрал аргумент инструмента вовсе: у вызова нет id,
                    # ответить модели ошибкой не на что — раунд заново, напечатанное стираем.
                    log.warning("кривой JSON аргумента, раунд заново (%d)", attempt + 1)
                    if said:
                        yield CoachChunk(kind="reset")
                        said = []
            assert final is not None
            # Раунд закончился — его токены записаны сразу: закроет человек аппку на
            # следующем раунде, этот уже учтён.
            total += await self._note(final.usage)

            if final.stop_reason == "refusal":
                if said:
                    yield CoachChunk(kind="reset")   # обрывок отказа — не ответ
                yield CoachChunk(kind="delta", text=REFUSED)
                break
            if final.stop_reason == "max_tokens":
                yield CoachChunk(kind="delta", text=f"\n\n{CUT}" if said else CUT)
                break
            calls = _calls(final)
            if not calls:
                break
            if said:
                yield CoachChunk(kind="reset")
            messages.append({"role": "assistant", "content": final.content})
            results = []
            for call in calls:
                yield CoachChunk(kind="tool", text=call.name)
                results.append(await _result(data, call))
            messages.append({"role": "user", "content": results})

        yield CoachChunk(kind="end", usage=_usage(self._model, total))


def _messages(question: str, facts: str, history: Sequence[Turn]) -> list[dict[str, Any]]:
    """Те же реплики, что у OpenAI, без системной (у Claude она отдельным полем). Первой
    должна идти реплика человека — ответ ассистента в начале истории API отвергнет."""
    turns = _opening(question, facts, history)[1:]
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return turns


def _calls(message: Any) -> list[Any]:
    """Вызовы инструментов, которые можно исполнять. Обрезанный max_tokens вызов разбирается
    в «правдоподобный» неполный объект — его не исполняем вовсе (AI-PRINCIPLES §9)."""
    if getattr(message, "stop_reason", None) != "tool_use":
        return []
    return [b for b in (message.content or []) if getattr(b, "type", None) == "tool_use"]


async def _result(data: CoachData | None, call: Any) -> dict[str, Any]:
    """Исполнить вызов. Аргумент — ввод от модели: не словарь — ошибка ей текстом, а не
    исполнение; числа зажимает сам `CoachData` (AI-PRINCIPLES §5)."""
    args = getattr(call, "input", None)
    if not isinstance(args, dict):
        return {
            "type": "tool_result", "tool_use_id": call.id, "is_error": True,
            "content": json.dumps(
                {"INVALID_JSON": json.dumps(args, ensure_ascii=False, default=str)},
                ensure_ascii=False,
            ),
        }
    as_openai = SimpleNamespace(
        function=SimpleNamespace(name=call.name, arguments=json.dumps(args, ensure_ascii=False))
    )
    return {"type": "tool_result", "tool_use_id": call.id, "content": await _run(data, as_openai)}


def _snapshot_usage(stream: Any) -> object | None:
    try:
        return stream.current_message_snapshot.usage if stream is not None else None
    except Exception:  # noqa: BLE001 — снимка может не быть: поток упал до первого события
        return None


def _usage(model: str, total: Counter[str]) -> Usage:
    return Usage(model, **{k: total[k] for k in meter_fields(None)})
