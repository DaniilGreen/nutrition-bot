"""Сборка реального Assistant для eval-прогона: OpenAiNlu(gpt-4.1) + in-memory
репозитории + seed.CURATED. Это прод-композиция БЕЗ БД (как container.assistant_for,
но репозитории — фейки из tests.fakes). Нужен OPENAI_API_KEY (живой прогон).
"""
from __future__ import annotations

from app.application.assistant import Assistant
from app.application.entities import Product, User
from app.application.meal_service import MealService
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.clock import utcnow
from app.config import get_settings
from app.domain.value_objects import Density
from app.infrastructure.products.seed import CURATED, validate_curated
from tests.fakes import (
    InMemoryMealRepo,
    InMemoryProductRepo,
    InMemoryUserRepo,
    InMemoryWeightRepo,
)


async def build_eval_assistant(vision: object | None = None) -> tuple[Assistant, User]:
    from openai import AsyncOpenAI

    from app.infrastructure.nlu.openai_nlu import OpenAiNlu

    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY не задан — eval-прогон бьёт реальный OpenAI")

    # sample_k=1: eval мерит ТОЧНОСТЬ (within/MAPE), а не abstention — один вызов на позицию.
    # Сэмплинг/воздержание (D-15) проверяется юнит/e2e-тестами; k=5 в eval лишь ×5 тратит TPM и
    # уводит метрики от замороженного k=1-baseline. Прод остаётся на settings.sample_k (=5).
    nlu = OpenAiNlu(AsyncOpenAI(api_key=settings.openai_api_key), settings.openai_model, sample_k=1)
    users = InMemoryUserRepo()
    products = InMemoryProductRepo()
    meals = InMemoryMealRepo()
    weights = InMemoryWeightRepo()

    validate_curated()
    for r in CURATED:  # тот же курируемый каталог, что в проде (RAG/пины работают как в бою)
        await products.create(
            Product(
                canonical_key=r["key"],
                display_name=r["name"],
                density=Density(r["kcal"], r["p"], r["f"], r["c"]),
                category=r.get("cat"),
                portion_sizes=r.get("sizes"),
                default_portion_g=(r["sizes"]["medium"] if r.get("sizes") else None),
                source="curated",
                is_estimate=False,
            )
        )

    meal_service = MealService(products, meals, utcnow)
    assistant = Assistant(
        nlu,
        products,
        meal_service,
        WeightService(weights, utcnow),
        StatsService(meals, users, settings.default_goal(), utcnow),
        vision=vision,
    )
    user = await users.get_or_create(telegram_id=1, name="eval", locale="ru", timezone="UTC")
    return assistant, user
