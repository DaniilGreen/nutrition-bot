"""E2E FR-27: фото этикетки → КБЖУ/100г → вес порции → лог с ТОЧНОЙ плотностью (source=label)."""
from __future__ import annotations

from datetime import UTC, datetime

from app.application.assistant import Assistant
from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.meal_service import MealService
from app.application.results import LabelRead, MealLogged, Unknown
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.domain.goals import Goal
from tests.fakes import (
    InMemoryMealRepo,
    InMemoryProductRepo,
    InMemoryUserRepo,
    InMemoryWeightRepo,
    StubNlu,
    StubVision,
)

FIXED_NOW = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)


def _build(item: ParsedItem | None) -> tuple[Assistant, InMemoryUserRepo, InMemoryProductRepo]:
    users, products = InMemoryUserRepo(), InMemoryProductRepo()
    meals, weights = InMemoryMealRepo(), InMemoryWeightRepo()

    def clock() -> datetime:
        return FIXED_NOW

    ms = MealService(products, meals, clock)
    # StubNlu в label-флоу не вызывается, но конструктору Assistant он нужен
    nlu = StubNlu(ParsedMessage(intent=Intent.MEAL, items=[], confidence=1.0))
    assistant = Assistant(
        nlu, products, ms, WeightService(weights, clock),
        StatsService(meals, users, Goal(2000, 150, 65, 250), clock), vision=StubVision(item),
    )
    return assistant, users, products


def _label() -> ParsedItem:
    # Атвотер-консистентно: 4*3 + 9*2 + 4*12 = 78 ≈ 80
    return ParsedItem(
        name="Йогурт греческий", canonical_key="", unit=Unit.GRAM, grams=None,
        kcal_per_100g=80, protein_per_100g=3, fat_per_100g=2, carb_per_100g=12,
        unit_weight_g=250, confidence=1.0,
    )


async def _user(users: InMemoryUserRepo):
    return await users.get_or_create(telegram_id=1, name="t", locale="ru", timezone="UTC")


async def test_label_read_then_grams_logs_exact() -> None:
    assistant, users, products = _build(_label())
    user = await _user(users)

    read = await assistant.read_label(user, b"fake-image")
    assert isinstance(read, LabelRead)
    item = read.draft.items[read.item_index]
    assert item.from_label is True and item.unit_weight_g == 250

    item.grams = 100  # юзер ввёл вес порции
    item.unit = Unit.GRAM
    final = await assistant.continue_meal(user, read.draft, raw_input="этикетка")

    assert isinstance(final, MealLogged)
    mi = final.meal.items[0]
    assert round(mi.macros.kcal) == 80  # 80/100 * 100, точные цифры с этикетки
    assert mi.is_estimate is False  # этикетка = точно, НЕ «≈»
    pinned = [p for p in await products.list_all() if p.source == "label"]
    assert len(pinned) == 1 and pinned[0].display_name == "Йогурт греческий"


async def test_label_not_recognized_returns_unknown() -> None:
    assistant, users, _ = _build(None)  # vision не нашёл таблицу
    user = await _user(users)
    assert isinstance(await assistant.read_label(user, b"x"), Unknown)
