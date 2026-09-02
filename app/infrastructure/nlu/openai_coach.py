"""Реализация CoachPort на OpenAI. Свободный ответ текстом, не по схеме."""
from __future__ import annotations

from openai import AsyncOpenAI

from app.domain.spend import Usage

SYSTEM = """Ты — помощник по дневнику питания. Отвечай на русском, коротко и по делу.

Правила:
- Опирайся ТОЛЬКО на переданные цифры. Не выдумывай данных, которых нет.
- Не считай калорийность блюд — это делает приложение. Твоё дело объяснять цифры.
- Не ставь диагнозов и не назначай лечения. Ты не врач.
- Если данных мало для вывода, так и скажи, а не сочиняй.
- Без нравоучений и оценок вроде «плохо» или «ты переел». Факты и следствия.
- 2–5 предложений. Списком — только если пунктов правда несколько."""


class OpenAiCoach:
    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    async def ask(self, question: str, facts: str) -> tuple[str, Usage]:
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Мои данные:\n{facts}\n\nВопрос: {question}"},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        u = resp.usage
        usage = Usage(
            model=self._model,
            # Токены может не прийти (стрим, сбой) — считаем нулём, но НЕ падаем:
            # потерять ответ из-за отсутствия счётчика хуже, чем недосчитать цент.
            input_tokens=int(getattr(u, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(u, "completion_tokens", 0) or 0),
        )
        return text or "Не нашёл, что ответить по этим данным.", usage
