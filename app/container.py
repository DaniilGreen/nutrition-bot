"""Composition root: собирает зависимости и выдаёт Assistant на сессию.

Единственное место, где «склеены» конкретные реализации портов. Сервисы и домен
про это не знают.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.assistant import Assistant
from app.application.body_scans import BodyScanService
from app.application.coach import Coach, CoachPort
from app.application.meal_service import MealService
from app.application.ports import NluPort, SttPort, VisionPort
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.clock import Clock, utcnow
from app.config import Settings
from app.infrastructure.db.base import Database
from app.infrastructure.db.repositories import (
    SqlBodyScanRepo,
    SqlBugRepo,
    SqlMealRepo,
    SqlProductRepo,
    SqlSpendRepo,
    SqlUserRepo,
    SqlWeightRepo,
)


def build_nlu(settings: Settings, meter: object | None = None) -> NluPort:
    if settings.use_fake_nlu:
        from app.infrastructure.nlu.fake_nlu import FakeNlu

        return FakeNlu()

    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY не задан (или включите USE_FAKE_NLU=true)")
        from openai import AsyncOpenAI

        from app.infrastructure.nlu.openai_nlu import OpenAiNlu

        return OpenAiNlu(
            # max_retries=0: у разбора свой цикл повторов, вложенные повторы SDK превращали
            # одну попытку в 3 × 5 запросов (AI-PRINCIPLES §1).
            AsyncOpenAI(api_key=settings.openai_api_key, max_retries=0),
            settings.openai_model,
            sample_k=settings.sample_k,
            # AI-PRINCIPLES §2: разбор — основная статья трат, и до 08.09.2026
            # она не считалась вовсе.
            meter=meter,
        )

    raise RuntimeError(f"Неизвестный LLM_PROVIDER: {settings.llm_provider}")


def build_stt(settings: Settings, meter: object | None = None) -> SttPort | None:
    """STT (голос) доступен только при наличии OpenAI-ключа; иначе None."""
    if not settings.openai_api_key:
        return None
    from openai import AsyncOpenAI

    from app.infrastructure.stt.whisper_client import OpenAiStt

    return OpenAiStt(
        AsyncOpenAI(
            api_key=settings.openai_api_key, max_retries=settings.openai_max_retries,
            timeout=settings.openai_timeout_s,
        ),
        settings.openai_stt_model,
        meter,
    )


def build_vision(settings: Settings, meter: object | None = None) -> VisionPort | None:
    """Vision (фото этикеток, FR-27) доступен только при наличии OpenAI-ключа; иначе None."""
    if not settings.openai_api_key:
        return None
    from openai import AsyncOpenAI

    from app.infrastructure.vision.openai_vision import OpenAiVision

    return OpenAiVision(
        AsyncOpenAI(
            api_key=settings.openai_api_key, max_retries=settings.openai_max_retries,
            timeout=settings.openai_timeout_s,
        ),
        settings.vision_model,
        meter,
    )


def build_coach_port(settings: Settings, meter: object | None = None) -> CoachPort | None:
    """Ассистент вкладки AI доступен только при наличии ключа; иначе None —
    молча притворяться работающим хуже, чем честно сказать «недоступен»."""
    if not settings.openai_api_key:
        return None
    from openai import AsyncOpenAI

    from app.infrastructure.nlu.openai_coach import OpenAiCoach

    return OpenAiCoach(
        AsyncOpenAI(
            api_key=settings.openai_api_key, max_retries=settings.openai_max_retries,
            timeout=settings.openai_timeout_s,
        ),
        settings.coach_model,
        meter,
    )


@dataclass
class Container:
    settings: Settings
    db: Database
    nlu: NluPort
    stt: SttPort | None = None
    vision: VisionPort | None = None
    coach_port: CoachPort | None = None
    clock: Clock = utcnow

    @classmethod
    def create(cls, settings: Settings) -> Container:
        # БД собираем первой: счётчику трат нужна фабрика сессий, а он нужен
        # разбору и голосу (AI-PRINCIPLES §2).
        db = Database(settings.database_url, echo=settings.db_echo)
        from app.infrastructure.spend_meter import SpendMeter

        return cls(
            settings=settings,
            db=db,
            nlu=build_nlu(settings, SpendMeter(db.session_factory, "nlu")),
            stt=build_stt(settings, SpendMeter(db.session_factory, "stt")),
            vision=build_vision(settings, SpendMeter(db.session_factory, "vision")),
            coach_port=build_coach_port(settings, SpendMeter(db.session_factory, "coach")),
        )

    def spend_for(self, session: AsyncSession) -> SqlSpendRepo:
        """Учёт трат отдельным входом: экрану настроек нужен только счётчик,
        собирать ради него весь Assistant незачем."""
        return SqlSpendRepo(session)

    def coach_for(self, session: AsyncSession) -> Coach | None:
        """Ассистент вкладки AI. None — если ключа нет."""
        if self.coach_port is None:
            return None
        users = SqlUserRepo(session)
        meals = SqlMealRepo(session)
        stats = StatsService(meals, users, self.settings.default_goal(), self.clock)
        return Coach(
            self.coach_port,
            SqlSpendRepo(session),
            stats,
            self.settings.ai_month_limit_usd,
            meals,
            SqlWeightRepo(session),
            SqlProductRepo(session),
            SqlBodyScanRepo(session),
        )

    def body_scans_for(self, session: AsyncSession) -> BodyScanService:
        """Замеры состава (§6.24) и недельный баланс с полом по обмену (§6.23)."""
        users = SqlUserRepo(session)
        stats = StatsService(SqlMealRepo(session), users, self.settings.default_goal(), self.clock)
        return BodyScanService(
            SqlBodyScanRepo(session), users, SqlWeightRepo(session), stats, self.clock
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
            self.nlu, products, meal_service, weight_service, stats_service,
            self.stt, bugs, self.vision,
            SqlSpendRepo(session), self.settings.ai_month_limit_usd,
        )
        return assistant, users
