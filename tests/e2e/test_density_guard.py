"""E2E: пустую LLM-оценку (все макросы 0) не пиним + ретрай парса (баг «пангасиус 0 ккал»)."""
from __future__ import annotations

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.results import MealLogged
from tests.e2e.test_meal_flow import _build, _new_user
from tests.fakes import StubNlu


def _fish(kcal: float, p: float, f: float, c: float) -> ParsedMessage:
    return ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="пангасиус",
                canonical_key="pangasius",
                unit=Unit.GRAM,
                grams=400,
                kcal_per_100g=kcal,
                protein_per_100g=p,
                fat_per_100g=f,
                carb_per_100g=c,
                confidence=0.9,
            )
        ],
    )


async def test_blank_density_not_pinned() -> None:
    # LLM отдаёт пустую оценку (и на ретрае тоже) → продукт НЕ закрепляется в каталоге
    assistant, users, products = _build(StubNlu(_fish(0, 0, 0, 0)))
    user = await _new_user(users)

    await assistant.process(user, "пангасиус 400")

    assert await products.get_by_key("pangasius") is None  # каталог не отравлен
    assert "pangasius" not in await products.all_keys()


async def test_stated_totals_logged_as_is() -> None:
    # FR-22: пользователь сам назвал готовые КБЖУ (меню/этикетка) → пишем как есть, без каталога
    meal = ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="рис с гречей из кафе",
                canonical_key="cafe_dish",
                unit=Unit.PORTION,
                total_kcal=400,
                total_protein=30,
                total_fat=10,
                total_carb=50,
                confidence=0.9,
            )
        ],
    )
    assistant, users, products = _build(StubNlu(meal))
    user = await _new_user(users)

    result = await assistant.process(user, "рис с гречей из кафе 400 ккал 30/10/50")

    assert isinstance(result, MealLogged)
    it = result.meal.items[0]
    assert it.name_snapshot == "рис с гречей из кафе"
    assert (it.macros.kcal, it.macros.protein, it.macros.fat, it.macros.carb) == (400, 30, 10, 50)
    assert it.product_id is None  # в каталог не пиним
    assert await products.get_by_key("cafe_dish") is None


async def test_blank_real_food_fallback_not_zero() -> None:
    # INV-11.3: реальная еда с пустой оценкой (и на ретрае) → фолбэк ≈150/100г, не 0 в дневнике
    assistant, users, products = _build(StubNlu(_fish(0, 0, 0, 0)))
    user = await _new_user(users)

    result = await assistant.process(user, "пангасиус 400")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal > 0  # не молчаливый 0
    assert await products.get_by_key("pangasius") is None  # фолбэк в каталог не пиннится


async def test_water_blank_stays_zero() -> None:
    # вода — осознанно нулевая (FR-25): фолбэк НЕ применяется, остаётся 0
    meal = ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="вода", canonical_key="water", unit=Unit.ML, grams=500,
                kcal_per_100g=0, protein_per_100g=0, fat_per_100g=0, carb_per_100g=0,
                confidence=0.9,
            )
        ],
    )
    assistant, users, _ = _build(StubNlu(meal))
    user = await _new_user(users)

    result = await assistant.process(user, "вода 500")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal == 0


async def test_stated_totals_trusted_not_reconciled() -> None:
    # INV-11: явные тоталы пользователя доверяем как есть, НЕ перетираем Атвотером (только границы)
    meal = ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="поке", canonical_key="poke", unit=Unit.PORTION,
                total_kcal=350, total_protein=35, total_fat=17.5, total_carb=70,
                confidence=0.9,
            )
        ],
    )
    assistant, users, _ = _build(StubNlu(meal))
    user = await _new_user(users)

    result = await assistant.process(user, "поке 350 ккал 35/17.5/70")

    assert isinstance(result, MealLogged)
    assert result.meal.items[0].macros.kcal == 350  # сохранён ввод юзера, не пересчитан в 577


async def test_partial_stated_totals_preserved() -> None:
    # HIGH-1: «блюдо 400 ккал 30 белка» (частичный набор) → ккал юзера сохраняем, не зануляем
    meal = ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="блюдо из кафе", canonical_key="cafe_dish", unit=Unit.PORTION,
                total_kcal=400, total_protein=30, confidence=0.9,  # fat/carb = None
            )
        ],
    )
    assistant, users, _ = _build(StubNlu(meal))
    user = await _new_user(users)

    result = await assistant.process(user, "блюдо из кафе 400 ккал 30 белка")

    assert isinstance(result, MealLogged)
    it = result.meal.items[0]
    assert it.macros.kcal == 400 and it.macros.protein == 30  # не перетёрто Атвотером (120)


async def test_blank_coffee_with_milk_gets_fallback() -> None:
    # HIGH-3: «кофе с молоком без сахара» с пустой оценкой → фолбэк (не 0): есть «молок»
    meal = ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="кофе с молоком без сахара", canonical_key="coffee_milk", unit=Unit.ML,
                grams=200, kcal_per_100g=0, protein_per_100g=0, fat_per_100g=0, carb_per_100g=0,
                confidence=0.9,
            )
        ],
    )
    assistant, users, _ = _build(StubNlu(meal))
    user = await _new_user(users)

    result = await assistant.process(user, "кофе с молоком без сахара 200")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal > 0  # «молок» в имени → не зануляем


async def test_retry_recovers_blank_estimate() -> None:
    # первый парс пустой, ретрай даёт норму → пинится корректно (120/100 * 400 = 480)
    assistant, users, products = _build(StubNlu(_fish(0, 0, 0, 0), _fish(120, 20, 5, 0)))
    user = await _new_user(users)

    result = await assistant.process(user, "пангасиус 400")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal == 480.0
    pinned = await products.get_by_key("pangasius")
    assert pinned is not None and pinned.density.kcal_per_100g == 120
