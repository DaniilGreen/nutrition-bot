"""Учёт расхода на вызовы модели вне ассистента (AI-PRINCIPLES §2).

До 08.09.2026 в burnfat считался ТОЛЬКО ассистент — самая мелкая статья. Разбор
еды (пять вызовов на приём с оценкой плотности) и распознавание голоса не
учитывались вовсе, поэтому счётчик «потрачено $0.03 из $5» показывал малую часть
правды, а лимит ничего не ограничивал.

Своя сессия, а не сессия запроса: деньги потрачены, даже если запись приёма потом
откатится. Учёт, привязанный к транзакции ввода, теряется ровно там, где он нужнее
всего — на сбоях.

ponytail: одна короткая транзакция на вызов модели. Потолок — пять транзакций на
приём с оценкой; на двух пользователях это ничто. Станет заметно — копить в памяти
и писать пачкой.
"""
from __future__ import annotations

import logging

from app.domain.spend import Usage

log = logging.getLogger(__name__)


class SpendMeter:
    def __init__(self, session_factory: object, purpose: str) -> None:
        self._sessions = session_factory
        self._purpose = purpose

    async def note(self, *, model: str, input_tokens: int, output_tokens: int) -> None:
        await self._write(Usage(model, input_tokens, output_tokens))

    async def note_audio(self, *, model: str, seconds: float) -> None:
        """Whisper тарифицируется за МИНУТЫ, не за токены (AI-PRINCIPLES §2).
        Попытка посчитать его токенами даёт мусор в обе стороны."""
        from app.domain.spend import PRICES

        per_minute = PRICES.get(model, (0.006, 0.0))[0]
        await self._write(Usage(model, 0, 0), cost_usd=per_minute * seconds / 60.0)

    async def _write(self, usage: Usage, cost_usd: float | None = None) -> None:
        """Сбой учёта не должен ронять запись: недосчитать цент дешевле,
        чем потерять приём."""
        try:
            from app.infrastructure.db.repositories import SqlSpendRepo

            async with self._sessions() as session:
                await SqlSpendRepo(session).add(
                    user_id=None,          # бюджет общий; привязка к человеку тут не нужна
                    model=usage.model,
                    purpose=self._purpose,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cost_usd=usage.cost_usd if cost_usd is None else cost_usd,
                )
                await session.commit()
        except Exception:  # noqa: BLE001
            log.warning("не удалось записать расход (%s)", self._purpose, exc_info=True)
