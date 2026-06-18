"""Реализация SttPort через OpenAI Whisper (audio transcriptions)."""
from __future__ import annotations

from openai import AsyncOpenAI


class OpenAiStt:
    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    async def transcribe(self, audio: bytes, *, filename: str = "voice.ogg") -> str:
        result = await self._client.audio.transcriptions.create(
            model=self._model,
            file=(filename, audio),
            language="ru",
        )
        return result.text.strip()
