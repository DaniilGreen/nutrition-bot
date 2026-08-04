"""E2E кнопки «Повторить»: выпил вторую бутылку — записать её теми же цифрами, без ИИ.

Кейс владельца: снял пиво с этикетки, потом выпил ещё. Пересчитывать нечего — вес и КБЖУ уже
зафиксированы снапшотом (INV-3), поэтому повтор обязан совпасть с оригиналом ДО ГРАММА. Если
когда-нибудь повтор пойдёт в LLM за новой оценкой, эти тесты покраснеют — так и задумано.
"""
from __future__ import annotations

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.results import MealLogged
from tests.e2e.test_meal_flow import _build, _new_user
from tests.fakes import StubNlu


def _beer() -> ParsedMessage:
    """Бутылка пива с этикетки: вес явный, плотность известна."""
    return ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="пиво нефильтрованное светлое",
                canonical_key="beer_unfiltered_light",
                unit=Unit.GRAM,
                grams=500,
                kcal_per_100g=42,
                protein_per_100g=0.5,
                fat_per_100g=0,
                carb_per_100g=3.8,
                confidence=0.9,
            )
        ],
    )


async def test_repeat_copies_grams_and_kcal_exactly() -> None:
    assistant, users, _ = _build(StubNlu(_beer()))
    user = await _new_user(users)
    first = await assistant.process(user, "бутылка пива нефильтрованного светлого")
    assert isinstance(first, MealLogged)

    again = await assistant.repeat_meal(user, first.meal.id)

    assert again is not None
    src, copy = first.meal.items[0], again.meal.items[0]
    assert (copy.grams, copy.macros) == (src.grams, src.macros)  # до грамма, без переоценки
    assert copy.name_snapshot == src.name_snapshot
    assert again.meal.id != first.meal.id  # ВТОРАЯ запись, а не правка первой


async def test_repeat_doubles_the_day_total() -> None:
    """Смысл кнопки: день вырос ровно на один приём, а не «переписался»."""
    assistant, users, _ = _build(StubNlu(_beer()))
    user = await _new_user(users)
    first = await assistant.process(user, "бутылка пива")
    assert isinstance(first, MealLogged)
    one = (await assistant.today(user)).remaining.consumed.kcal

    await assistant.repeat_meal(user, first.meal.id)

    day = await assistant.today(user)
    assert day.remaining.consumed.kcal == one * 2
    assert day.meals_count == 2


async def test_repeat_of_deleted_meal_returns_none() -> None:
    """Кнопка живёт в чате вечно — по удалённой записи повтор обязан молчать, а не падать."""
    assistant, users, _ = _build(StubNlu(_beer()))
    user = await _new_user(users)
    first = await assistant.process(user, "бутылка пива")
    assert isinstance(first, MealLogged)
    await assistant.delete_meal(user, first.meal.id)

    assert await assistant.repeat_meal(user, first.meal.id) is None
