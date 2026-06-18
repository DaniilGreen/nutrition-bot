"""E2E для Волны 2a: удаление отдельной позиции записи + пересчёт тоталов."""
from __future__ import annotations

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.results import MealLogged
from tests.e2e.test_meal_flow import _build, _new_user
from tests.fakes import StubNlu


def _chicken_and_rice() -> ParsedMessage:
    return ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.95,
        items=[
            ParsedItem(
                name="курица",
                canonical_key="chicken",
                unit=Unit.GRAM,
                grams=200,
                kcal_per_100g=165,
                protein_per_100g=31,
                fat_per_100g=3.6,
                carb_per_100g=0,
                confidence=0.95,
            ),
            ParsedItem(
                name="рис",
                canonical_key="rice",
                unit=Unit.GRAM,
                grams=150,
                kcal_per_100g=130,
                protein_per_100g=2.7,
                fat_per_100g=0.3,
                carb_per_100g=28,
                confidence=0.95,
            ),
        ],
    )


async def test_remove_one_item_recomputes_totals() -> None:
    assistant, users, _ = _build(StubNlu(_chicken_and_rice()))
    user = await _new_user(users)

    logged = await assistant.process(user, "курица 200 и рис 150")
    assert isinstance(logged, MealLogged)
    assert len(logged.meal.items) == 2
    assert logged.meal.totals.kcal == 525.0  # 165*2 + 130*1.5

    item_ids = [i.id for i in logged.meal.items]
    updated = await assistant.remove_item(user, logged.meal.id, item_ids[0])  # убрать курицу

    assert isinstance(updated, MealLogged)
    assert len(updated.meal.items) == 1
    assert updated.meal.items[0].name_snapshot == "рис"
    assert updated.meal.totals.kcal == 195.0  # 130*1.5


async def test_remove_last_item_deletes_meal() -> None:
    assistant, users, _ = _build(StubNlu(_chicken_and_rice()))
    user = await _new_user(users)

    logged = await assistant.process(user, "курица и рис")
    assert isinstance(logged, MealLogged)
    item_ids = [i.id for i in logged.meal.items]

    await assistant.remove_item(user, logged.meal.id, item_ids[0])
    gone = await assistant.remove_item(user, logged.meal.id, item_ids[1])

    assert gone is None
    today = await assistant.today(user)
    assert today.meals_count == 0
