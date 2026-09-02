"""E2E на уровне Assistant: фейковые репозитории + StubNlu, без БД и без LLM.

Главный тест — `test_density_pinned_on_first_sight`: доказывает инвариант
«LLM наполняет, БД фиксирует». Плюс M2: уточнение порции (clarification).
"""
from __future__ import annotations

from datetime import UTC, datetime

from app.application.assistant import Assistant
from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.meal_service import MealService
from app.application.results import (
    ClarificationNeeded,
    DailyStats,
    MealLogged,
    WeightLogged,
)
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

FIXED_NOW = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)


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


def _pho_bo() -> ParsedMessage:
    return ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.6,
        items=[
            ParsedItem(
                name="фо бо",
                canonical_key="pho_bo",
                unit=Unit.PORTION,
                portion_hint=None,
                kcal_per_100g=60,
                protein_per_100g=4,
                fat_per_100g=2,
                carb_per_100g=6,
                confidence=0.6,
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


async def test_day_view_today_and_yesterday() -> None:
    # вар. A: day_view(0)=сегодня с приёмом, day_view(1)=вчера пусто, навигация по days_ago
    assistant, users, _ = _build(StubNlu(_chicken(150)))
    user = await _new_user(users)
    await assistant.process(user, "курица 250")
    today = await assistant.day_view(user, days_ago=0)
    assert len(today.meals) == 1 and today.days_ago == 0
    yest = await assistant.day_view(user, days_ago=1)
    assert yest.meals == [] and yest.days_ago == 1


async def test_today_meals_shows_all_today() -> None:
    # багрепорт #12: пикер отмены показывает ВСЕ сегодняшние приёмы, не последние 5
    assistant, users, _ = _build(StubNlu(_chicken(150), _chicken(160), _chicken(170)))
    user = await _new_user(users)
    for _ in range(3):
        await assistant.process(user, "курица 250")
    today = await assistant.today_meals(user)
    assert len(today.meals) == 3


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


# --- M2: уточнение порции ---


async def test_portion_triggers_clarification() -> None:
    assistant, users, _ = _build(StubNlu(_pho_bo()))
    user = await _new_user(users)

    result = await assistant.process(user, "съел тарелку фо бо")

    assert isinstance(result, ClarificationNeeded)
    assert result.item_index == 0
    assert result.item_name == "фо бо"


async def test_meal_not_logged_until_clarified() -> None:
    assistant, users, _ = _build(
        StubNlu(_pho_bo(), ParsedMessage(intent=Intent.QUERY, confidence=0.9))
    )
    user = await _new_user(users)

    await assistant.process(user, "съел тарелку фо бо")
    stats = await assistant.process(user, "сколько осталось")

    assert isinstance(stats, DailyStats)
    assert stats.meals_count == 0  # без ответа на вопрос запись не создаётся


async def test_continue_after_portion_choice() -> None:
    assistant, users, _ = _build(StubNlu(_pho_bo()))
    user = await _new_user(users)

    r1 = await assistant.process(user, "съел тарелку фо бо")
    assert isinstance(r1, ClarificationNeeded)

    draft = r1.draft
    draft.items[0].portion_hint = "large"
    r2 = await assistant.continue_meal(user, draft, raw_input="съел тарелку фо бо")

    assert isinstance(r2, MealLogged)
    # large: дефолтная порция 300 г * 1.4 = 420 г; фо бо 60 ккал/100 г -> 252 ккал
    assert r2.meal.items[0].grams == 420
    assert r2.meal.totals.kcal == 252
