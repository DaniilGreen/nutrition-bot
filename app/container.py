"""Composition root: собирает зависимости и выдаёт Assistant на сессию.

Единственное место, где «склеены» конкретные реализации портов. Сервисы и домен
про это не знают.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.assistant import Assistant
from app.application.meal_service import MealService
from app.application.ports import NluPort
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.clock import Clock, utcnow
from app.config import Settings
from app.infrastructure.db.base import Database
from app.infrastructure.db.repositories import (
    SqlMealRepo,
    SqlProductRepo,
    SqlUserRepo,
    SqlWeightRepo,
)


def build_nlu(settings: Settings) -> NluPort:
    if settings.use_fake_nlu:
        from app.infrastructure.nlu.fake_nlu import FakeNlu

        return FakeNlu()

    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY не задан (или включите USE_FAKE_NLU=true)")
        from openai import AsyncOpenAI

        from app.infrastructure.nlu.openai_nlu import OpenAiNlu

        return OpenAiNlu(AsyncOpenAI(api_key=settings.openai_api_key), settings.openai_model)

    raise RuntimeError(f"Неизвестный LLM_PROVIDER: {settings.llm_provider}")


@dataclass
class Container:
    settings: Settings
    db: Database
    nlu: NluPort
    clock: Clock = utcnow

    @classmethod
    def create(cls, settings: Settings) -> Container:
        return cls(
            settings=settings,
            db=Database(settings.database_url, echo=settings.db_echo),
            nlu=build_nlu(settings),
        )

    def assistant_for(self, session: AsyncSession) -> tuple[Assistant, SqlUserRepo]:
        users = SqlUserRepo(session)
        products = SqlProductRepo(session)
        meals = SqlMealRepo(session)
        weights = SqlWeightRepo(session)

        meal_service = MealService(products, meals, self.clock)
        weight_service = WeightService(weights, self.clock)
        stats_service = StatsService(meals, users, self.settings.default_goal(), self.clock)
        assistant = Assistant(self.nlu, products, meal_service, weight_service, stats_service)
        return assistant, users
