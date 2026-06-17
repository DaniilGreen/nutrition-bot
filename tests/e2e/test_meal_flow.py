"""E2E на уровне Assistant: фейковые репозитории + StubNlu, без БД и без LLM.

Главный тест — `test_density_pinned_on_first_sight`: доказывает инвариант
«LLM наполняет, БД фиксирует» (повторный приём той же еды даёт те же цифры,
даже если LLM выдал другую плотность).
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.application.assistant import Assistant
from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.meal_service import MealService
from app.application.results import DailyStats, MealLogged, WeightLogged
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.domain.goals import Goal
from tests.fakes import (
    InMemoryMealRepo,
    InMemoryProductRepo,
    InMemoryUserRepo,
    InMemoryWeightRepo,
    StubNlu,
)

FIXED_NOW = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)


def _build(nlu: StubNlu) -> tuple[Assistant, InMemoryUserRepo, InMemoryProductRepo]:
    users = InMemoryUserRepo()
    products = InMemoryProductRepo()
    meals = InMemoryMealRepo()
    weights = InMemoryWeightRepo()

    def clock() -> datetime:
        return FIXED_NOW

    meal_service = MealService(products, meals, clock)
    weight_service = WeightService(weights, clock)
    stats = StatsService(meals, users, Goal(2000, 150, 65, 250), clock)
    assistant = Assistant(nlu, products, meal_service, weight_service, stats)
    return assistant, users, products


def _chicken(kcal: float) -> ParsedMessage:
    return ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.95,
        items=[
            ParsedItem(
                name="курица",
                canonical_key="chicken",
                unit=Unit.GRAM,
                grams=250,
                kcal_per_100g=kcal,
                protein_per_100g=31,
                fat_per_100g=3.6,
                carb_per_100g=0,
                confidence=0.95,
            )
        ],
    )


async def _new_user(users: InMemoryUserRepo):
    return await users.get_or_create(telegram_id=1, name="Test", locale="ru", timezone="UTC")


async def test_meal_logged_and_computed_app_side() -> None:
    assistant, users, _ = _build(StubNlu(_chicken(165)))
    user = await _new_user(users)

    result = await assistant.process(user, "курица 250 грамм")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal == 412.5
    assert result.meal.totals.protein == 77.5
    assert result.remaining.kcal_left == 2000 - 412.5


async def test_density_pinned_on_first_sight() -> None:
    # LLM возвращает 165, затем 999 — второй раз плотность берётся из БД (165).
    assistant, users, products = _build(StubNlu(_chicken(165), _chicken(999)))
    user = await _new_user(users)

    r1 = await assistant.process(user, "курица 250 грамм")
    r2 = await assistant.process(user, "курица 250 грамм")

    assert isinstance(r1, MealLogged)
    assert isinstance(r2, MealLogged)
    assert r1.meal.totals.kcal == r2.meal.totals.kcal == 412.5
    assert await products.all_keys() == ["chicken"]


async def test_weight_flow() -> None:
    assistant, users, _ = _build(
        StubNlu(ParsedMessage(intent=Intent.WEIGHT, weight_kg=86.8, confidence=0.9))
    )
    user = await _new_user(users)

    result = await assistant.process(user, "вес 86.8")

    assert isinstance(result, WeightLogged)
    assert result.log.weight_kg == 86.8
    assert result.delta_kg is None


async def test_query_after_meal() -> None:
    assistant, users, _ = _build(
        StubNlu(_chicken(165), ParsedMessage(intent=Intent.QUERY, confidence=0.9))
    )
    user = await _new_user(users)

    await assistant.process(user, "курица 250 грамм")
    result = await assistant.process(user, "сколько осталось")

    assert isinstance(result, DailyStats)
    assert result.meals_count == 1
    assert result.remaining.consumed.kcal == 412.5
