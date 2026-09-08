"""Реализация SttPort через OpenAI Whisper (audio transcriptions)."""
from __future__ import annotations

import logging

from openai import AsyncOpenAI

log = logging.getLogger(__name__)


class OpenAiStt:
    def __init__(
        self, client: AsyncOpenAI, model: str, meter: object | None = None
    ) -> None:
        self._client = client
        self._model = model
        # AI-PRINCIPLES §2: голос тарифицируется за МИНУТЫ. До 08.09.2026 он не
        # учитывался вовсе, и месячный лимит его не видел.
        self._meter = meter

    async def transcribe(self, audio: bytes, *, filename: str = "voice.ogg") -> str:
        result = await self._client.audio.transcriptions.create(
            model=self._model,
            file=(filename, audio),
            language="ru",
            # Длительность нужна для учёта: обычный json её не отдаёт, а считать
            # минуты по размеру файла нельзя — битрейт у каждого клиента свой.
            response_format="verbose_json",
        )
        await self._note(getattr(result, "duration", None))
        return (result.text or "").strip()

    async def _note(self, seconds: float | None) -> None:
        if self._meter is None or not seconds:
            return
        try:
            await self._meter.note_audio(model=self._model, seconds=float(seconds))
        except Exception:  # noqa: BLE001 — учёт не должен ронять расшифровку
            log.warning("не удалось записать расход на голос", exc_info=True)
