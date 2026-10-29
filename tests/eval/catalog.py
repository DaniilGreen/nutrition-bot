"""Сборка реального Assistant для eval-прогона: разбор и фото — той же сборкой, что в проде
(`container.build_nlu` / `build_vision`), репозитории — in-memory фейки + seed.CURATED.
Провайдер и модель — флагами прогона, не правкой кода (NFR-6). Живой прогон: нужен ключ
провайдера (OPENAI_API_KEY или ANTHROPIC_API_KEY).
"""
from __future__ import annotations

from app.application.assistant import Assistant
from app.application.entities import Product, User
from app.application.meal_service import MealService
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.clock import utcnow
from app.config import Settings, get_settings
from app.domain.value_objects import Density
from app.infrastructure.products.seed import CURATED, validate_curated
from tests.fakes import (
    InMemoryMealRepo,
    InMemoryProductRepo,
    InMemoryUserRepo,
    InMemoryWeightRepo,
)


def eval_settings(
    provider: str, *, nlu_model: str = "", vision_model: str = "", effort: str = "",
    thinking_off: bool = False,
) -> Settings:
    """Настройки прогона поверх прод-настроек. Модель чужого провайдера — ошибка сразу,
    до первого платного вызова (та же проверка, что на старте прода)."""
    update: dict = {
        "llm_provider": provider, "use_fake_nlu": False, "claude_thinking_off": thinking_off,
        # sample_k=1: eval мерит ТОЧНОСТЬ (within/MAPE), а не abstention — один вызов на позицию.
        # Сэмплинг/воздержание (D-15) проверяется юнит/e2e-тестами; k>1 в eval лишь множит
        # траты и уводит метрики от замороженного k=1-baseline. Прод — на settings.sample_k.
        "sample_k": 1,
    }
    if nlu_model:
        update["nlu_model"] = nlu_model
    if vision_model:
        update["vision_model"] = vision_model
    if effort:
        update["nlu_effort"] = update["vision_effort"] = effort
    settings = get_settings().model_copy(update=update)
    for route in ("nlu", "vision"):
        settings.model_for(route)
    return settings


async def build_eval_assistant(
    settings: Settings, meter: object | None = None, *, with_vision: bool = False,
) -> tuple[Assistant, User]:
    """Assistant прод-сборки. `meter` (EvalMeter) считает деньги и время каждого вызова модели
    и держит потолок прогона."""
    from app.container import build_nlu, build_vision

    nlu = build_nlu(settings, meter)
    vision = build_vision(settings, meter) if with_vision else None
    if with_vision and vision is None:
        raise RuntimeError(f"нет ключа {settings.llm_provider} — фото не прочитать")
    if meter is not None:
        meter.timed(nlu, "_parse_once")
        if vision is not None:
            meter.timed(vision, "_parse")
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
