"""Реализация VisionPort через Claude (D-41, NFR-6): фото блюда, бланк, этикетка.

Промпты, схемы и перевод ответа в доменные объекты — общие с `OpenAiVision`; здесь
подменён только один вызов (`_parse`). Снимок — base64-блоком перед текстом, системный
промпт — с часовой меткой кэша: у фото он ~3k токенов и одинаков от снимка к снимку.
Отказ модели и обрезка по max_tokens — «не прочитал» (None), как пустой разбор у OpenAI.
"""
from __future__ import annotations

import logging
from typing import Any

from app.infrastructure.claude_compat import (
    billed_model,
    cached_text,
    image_block,
    meter_fields,
    read_json,
    request_params,
)
from app.infrastructure.vision.openai_vision import OpenAiVision, _Out

log = logging.getLogger(__name__)

# Бланк InBody с черновиком строк — самый длинный ответ (~1.5k токенов); рассуждения тоже
# входят в лимит. Потолок заодно ограничивает цену вызова.
MAX_TOKENS = 8000


class ClaudeVision(OpenAiVision):
    def __init__(
        self, client: Any, model: str, meter: object | None = None, *,
        effort: str = "low", thinking_off: bool = False, fast: bool = False,
    ) -> None:
        super().__init__(client, model, meter)
        self._effort = effort
        self._thinking_off = thinking_off
        self._fast = fast

    async def _parse(
        self, system: str, text: str, image: bytes, schema: type[_Out],
        *, detail: str | None = None,
    ) -> _Out | None:
        # `detail` — ручка OpenAI; Claude сам решает, как ужать снимок.
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            system=[cached_text(system)],
            messages=[{
                "role": "user",
                "content": [image_block(image), {"type": "text", "text": text}],
            }],
            **request_params(
                self._model, self._effort, thinking_off=self._thinking_off, schema=schema,
                fast=self._fast,
            ),
        )
        await self._meter_claude(resp)  # до разбора: отказ тоже оплачен
        return read_json(resp, schema)

    async def _meter_claude(self, resp: object) -> None:
        if self._meter is None:
            return
        try:
            await self._meter.note(
                model=billed_model(self._model, resp.usage), **meter_fields(resp.usage)
            )
        except Exception:  # noqa: BLE001 — недосчитать цент дешевле, чем потерять фото
            log.warning("не удалось записать расход фото", exc_info=True)
