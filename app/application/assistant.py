"""Фасад приложения: разобрать сообщение и направить в нужный use-case.

Презентационный слой (бот/веб) вызывает только Assistant.process и получает
Result, который форматирует. Это делает маршрутизацию тестируемой (e2e на фейках).
"""
from __future__ import annotations

from app.application.dto import Intent
from app.application.entities import User
from app.application.meal_service import MealService
from app.application.ports import NluPort, ProductRepo
from app.application.results import MealLogged, Result, Unknown, WeightLogged
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService


class Assistant:
    def __init__(
        self,
        nlu: NluPort,
        products: ProductRepo,
        meal_service: MealService,
        weight_service: WeightService,
        stats_service: StatsService,
    ) -> None:
        self._nlu = nlu
        self._products = products
        self._meals = meal_service
        self._weights = weight_service
        self._stats = stats_service

    async def process(self, user: User, text: str, *, input_type: str = "text") -> Result:
        known_keys = await self._products.all_keys()
        parsed = await self._nlu.parse(text, known_keys=known_keys)

        if parsed.intent is Intent.MEAL and parsed.items:
            meal = await self._meals.log(user, parsed, raw_input=text, input_type=input_type)
            stats = await self._stats.daily(user)
            return MealLogged(meal=meal, remaining=stats.remaining)

        if parsed.intent is Intent.WEIGHT and parsed.weight_kg is not None:
            log, delta = await self._weights.log(user, parsed.weight_kg)
            return WeightLogged(log=log, delta_kg=delta)

        if parsed.intent is Intent.QUERY:
            return await self._stats.daily(user)

        return Unknown(reason="не распознал приём пищи или вес")
