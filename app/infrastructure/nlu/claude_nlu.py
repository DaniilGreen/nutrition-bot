"""Реализация NluPort через Claude (D-41, NFR-6).

Повторы (AI-PRINCIPLES §1), сэмплы согласия только на новых продуктах (D-15, INV-12) и
перевод схемы в DTO — общие с `OpenAiNlu`: класс наследует их и подменяет транспорт одного
вызова. Промпт и pydantic-схема те же — сравнивать провайдеров в eval можно только так.

Отличия транспорта:
- temperature нет вовсе (400 на Claude 5.5) — сэмплы согласия просто независимые вызовы:
  разброс даёт сама модель. Первый вызов идёт раньше остальных (так устроен
  `parse_samples`), и сэмплы читают кэш, который он записал: параллельные — не читали бы;
- JSON — `output_config.format`, и разбор после проверки `stop_reason`: отказ и обрезка —
  «не понял», а не исключение в повторы;
- кэш явный: часовые метки на системном промпте и на каталоге ключей.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from app.application.dto import Intent, ParsedMessage
from app.infrastructure.claude_compat import (
    billed_model,
    cached_text,
    meter_fields,
    read_json,
    request_params,
)
from app.infrastructure.nlu.openai_nlu import TIMEOUT_S, OpenAiNlu, _to_dto
from app.infrastructure.nlu.prompts import SYSTEM_PROMPT, catalog_prompt, message_prompt
from app.infrastructure.nlu.schema import NluResult

log = logging.getLogger(__name__)

# Рассуждения входят в max_tokens. Разбор — ~150 токенов на позицию; 8000 — запас на low/medium,
# и одновременно потолок цены вызова, если модель разговорится ($0.16 на Opus 5.5).
MAX_TOKENS = 8000


class ClaudeNlu(OpenAiNlu):
    def __init__(
        self,
        client: Any,
        model: str,
        sample_k: int = 5,
        meter: object | None = None,
        timeout: float = TIMEOUT_S,
        *,
        effort: str = "low",
        thinking_off: bool = False,
        fast: bool = False,
    ) -> None:
        super().__init__(client, model, sample_k=sample_k, meter=meter, timeout=timeout)
        # Параметры семейства — один раз: effort и рассуждения не меняются между вызовами,
        # а их смена сбросила бы кэш сообщений.
        self._params = request_params(
            model, effort, thinking_off=thinking_off, schema=NluResult, fast=fast
        )

    async def _parse_once(
        self, text: str, known_keys: Sequence[str], temperature: float
    ) -> ParsedMessage:
        # `temperature` из общего цикла сэмплов не используется: у Claude её нет (D-41).
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            system=[cached_text(SYSTEM_PROMPT)],
            messages=[{
                "role": "user",
                "content": [
                    # Каталог меняется только с новым продуктом — своя метка, и сообщение
                    # человека после неё не сбрасывает кэш каталога.
                    cached_text(catalog_prompt(known_keys)),
                    {"type": "text", "text": message_prompt(text)},
                ],
            }],
            timeout=self._timeout,
            **self._params,
        )
        await self._meter_usage(resp)  # до разбора: отказ и сломанный JSON тоже оплачены
        parsed = read_json(resp, NluResult)
        if parsed is None:
            return ParsedMessage(intent=Intent.UNKNOWN, confidence=0.0)
        return _to_dto(parsed)

    async def _meter_usage(self, completion: object) -> None:
        if self._meter is None:
            return
        try:
            await self._meter.note(
                model=billed_model(self._model, completion.usage), **meter_fields(completion.usage)
            )
        except Exception:  # noqa: BLE001 — недосчитать цент дешевле, чем потерять запись
            log.warning("не удалось записать расход разбора", exc_info=True)
