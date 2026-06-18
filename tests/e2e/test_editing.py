"""E2E для Волны 2a: удаление отдельной позиции записи + пересчёт тоталов."""
from __future__ import annotations

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.meal_service import _name_matches
from app.application.results import CorrectionFailed, ItemRemoved, MealLogged
from tests.e2e.test_meal_flow import _build, _new_user
from tests.fakes import StubNlu


def _delete(name: str, key: str) -> ParsedMessage:
    return ParsedMessage(
        intent=Intent.CORRECTION,
        correction_op="delete",
        confidence=0.9,
        items=[
            ParsedItem(
                name=name,
                canonical_key=key,
                unit=Unit.UNKNOWN,
                kcal_per_100g=0,
                protein_per_100g=0,
                fat_per_100g=0,
                carb_per_100g=0,
                confidence=0.9,
            )
        ],
    )


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


# --- FR-14: коррекции на естественном языке («удали X») ---


async def test_delete_item_by_name() -> None:
    assistant, users, _ = _build(StubNlu(_chicken_and_rice(), _delete("рис", "rice")))
    user = await _new_user(users)

    await assistant.process(user, "курица 200 и рис 150")
    removed = await assistant.process(user, "удали рис")

    assert isinstance(removed, ItemRemoved)
    assert [i.name_snapshot for i in removed.items] == ["рис"]
    today = await assistant.today(user)
    assert today.remaining.consumed.kcal == 330.0  # осталась только курица 165*2


async def test_delete_item_not_found() -> None:
    assistant, users, _ = _build(StubNlu(_chicken_and_rice(), _delete("гречка", "buckwheat")))
    user = await _new_user(users)

    await assistant.process(user, "курица и рис")
    result = await assistant.process(user, "удали гречку")

    assert isinstance(result, CorrectionFailed)
    assert result.reason == "not_found"


async def test_correction_replace_is_unsupported() -> None:
    replace = _delete("рис", "rice")
    replace.correction_op = "replace"
    assistant, users, _ = _build(StubNlu(_chicken_and_rice(), replace))
    user = await _new_user(users)

    await assistant.process(user, "курица и рис")
    result = await assistant.process(user, "замени рис на гречку")

    assert isinstance(result, CorrectionFailed)
    assert result.reason == "unsupported"


def test_name_matches_substring_and_fuzzy() -> None:
    assert _name_matches("рис", "рис отварной")  # подстрока
    assert _name_matches("творог", "творог 5%")  # подстрока
    assert not _name_matches("рис", "гречка")  # разное
    assert not _name_matches("", "рис")  # пустой запрос
