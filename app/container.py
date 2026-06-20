"""Composition root: собирает зависимости и выдаёт Assistant на сессию.

Единственное место, где «склеены» конкретные реализации портов. Сервисы и домен
про это не знают.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.assistant import Assistant
from app.application.meal_service import MealService
from app.application.ports import NluPort, SttPort
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.clock import Clock, utcnow
from app.config import Settings
from app.infrastructure.db.base import Database
from app.infrastructure.db.repositories import (
    SqlBugRepo,
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

        return OpenAiNlu(
            AsyncOpenAI(api_key=settings.openai_api_key),
            settings.openai_model,
            sample_k=settings.sample_k,
        )

    raise RuntimeError(f"Неизвестный LLM_PROVIDER: {settings.llm_provider}")


def build_stt(settings: Settings) -> SttPort | None:
    """STT (голос) доступен только при наличии OpenAI-ключа; иначе None."""
    if not settings.openai_api_key:
        return None
    from openai import AsyncOpenAI

    from app.infrastructure.stt.whisper_client import OpenAiStt

    return OpenAiStt(AsyncOpenAI(api_key=settings.openai_api_key), settings.openai_stt_model)


@dataclass
class Container:
    settings: Settings
    db: Database
    nlu: NluPort
    stt: SttPort | None = None
    clock: Clock = utcnow

    @classmethod
    def create(cls, settings: Settings) -> Container:
        return cls(
            settings=settings,
            db=Database(settings.database_url, echo=settings.db_echo),
            nlu=build_nlu(settings),
            stt=build_stt(settings),
        )

    def assistant_for(self, session: AsyncSession) -> tuple[Assistant, SqlUserRepo]:
        users = SqlUserRepo(session)
        products = SqlProductRepo(session)
        meals = SqlMealRepo(session)
        weights = SqlWeightRepo(session)

        bugs = SqlBugRepo(session)

        meal_service = MealService(products, meals, self.clock)
        weight_service = WeightService(weights, self.clock)
        stats_service = StatsService(meals, users, self.settings.default_goal(), self.clock)
        assistant = Assistant(
            self.nlu, products, meal_service, weight_service, stats_service, self.stt, bugs
        )
        return assistant, users
