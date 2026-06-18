"""Фасад приложения: разобрать сообщение и направить в нужный use-case.

Презентационный слой (бот/веб) вызывает Assistant и получает Result, который
форматирует. Это делает маршрутизацию тестируемой (e2e на фейках).
"""
from __future__ import annotations

from app.application.dto import Intent, ParsedMessage, Unit
from app.application.entities import User
from app.application.meal_service import MealService
from app.application.ports import NluPort, ProductRepo, SttPort
from app.application.results import (
    ClarificationNeeded,
    DailyStats,
    History,
    MealDeleted,
    MealLogged,
    Result,
    Unknown,
    WeeklyStats,
    WeightLogged,
)
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.domain.goals import Goal


class Assistant:
    def __init__(
        self,
        nlu: NluPort,
        products: ProductRepo,
        meal_service: MealService,
        weight_service: WeightService,
        stats_service: StatsService,
        stt: SttPort | None = None,
    ) -> None:
        self._nlu = nlu
        self._products = products
        self._meals = meal_service
        self._weights = weight_service
        self._stats = stats_service
        self._stt = stt

    async def process(self, user: User, text: str, *, input_type: str = "text") -> Result:
        known_keys = await self._products.all_keys()
        parsed = await self._nlu.parse(text, known_keys=known_keys)

        if parsed.intent is Intent.MEAL and parsed.items:
            return await self._handle_meal(user, parsed, raw_input=text, input_type=input_type)

        if parsed.intent is Intent.WEIGHT and parsed.weight_kg is not None:
            log, delta = await self._weights.log(user, parsed.weight_kg)
            return WeightLogged(log=log, delta_kg=delta)

        if parsed.intent is Intent.QUERY:
            return await self._stats.daily(user)

        return Unknown(reason="не распознал приём пищи или вес")

    async def transcribe(self, audio: bytes, *, filename: str = "voice.ogg") -> str | None:
        """Голос → текст (M3). None, если STT не сконфигурирован (нет OpenAI-ключа)."""
        if self._stt is None:
            return None
        return await self._stt.transcribe(audio, filename=filename)

    async def continue_meal(
        self, user: User, draft: ParsedMessage, *, raw_input: str, input_type: str = "text"
    ) -> Result:
        """Продолжить запись приёма пищи после ответа на уточняющий вопрос."""
        return await self._handle_meal(user, draft, raw_input=raw_input, input_type=input_type)

    async def today(self, user: User) -> DailyStats:
        return await self._stats.daily(user)

    async def weekly(self, user: User) -> WeeklyStats:
        return await self._stats.weekly(user)

    async def current_goal(self, user: User) -> Goal:
        return await self._stats.current_goal(user)

    async def history(self, user: User, limit: int = 10) -> History:
        meals = await self._meals.recent(user, limit)
        return History(meals=meals, timezone=user.timezone)

    async def undo(self, user: User) -> MealDeleted:
        return MealDeleted(meal=await self._meals.delete_last(user))

    async def delete_meal(self, user: User, meal_id: int) -> MealDeleted:
        return MealDeleted(meal=await self._meals.delete(user, meal_id))

    async def _handle_meal(
        self, user: User, parsed: ParsedMessage, *, raw_input: str, input_type: str
    ) -> Result:
        idx = _first_unclarified_index(parsed)
        if idx is not None:
            return ClarificationNeeded(
                draft=parsed, item_index=idx, item_name=parsed.items[idx].name
            )

        meal = await self._meals.log(user, parsed, raw_input=raw_input, input_type=input_type)
        stats = await self._stats.daily(user)
        return MealLogged(meal=meal, remaining=stats.remaining)


def _first_unclarified_index(parsed: ParsedMessage) -> int | None:
    """Первый продукт с порцией без размера — для него и зададим вопрос."""
    for i, item in enumerate(parsed.items):
        if item.unit is Unit.PORTION and item.portion_hint is None:
            return i
    return None
