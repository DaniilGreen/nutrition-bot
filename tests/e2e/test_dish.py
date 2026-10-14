"""E2E на блюдо своей сборки (FR-39): кнопка под записью и фраза для котла.

Оба входа сходятся в одном пине. Главное, что тут доказывается, — история не движется
(INV-3): кнопка добавляет продукт в каталог, а не переписывает съеденное.
"""
from __future__ import annotations

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.results import MealLogged
from tests.e2e.test_meal_flow import _build, _new_user
from tests.fakes import StubNlu


def _item(
    name: str, key: str, grams: float, kcal: float,
    protein: float = 1.0, fat: float = 1.0, carb: float = 1.0,
) -> ParsedItem:
    """Б/Ж/У должны сходиться с ккал по Атвотеру, иначе gate (INV-10) пересчитает ккал
    из макросов, и тест будет мерить не то, что думает."""
    return ParsedItem(
        name=name, canonical_key=key, unit=Unit.GRAM, grams=grams,
        kcal_per_100g=kcal, protein_per_100g=protein, fat_per_100g=fat, carb_per_100g=carb,
        confidence=0.9,
    )


def _meal(*items: ParsedItem) -> ParsedMessage:
    return ParsedMessage(intent=Intent.MEAL, confidence=0.9, items=list(items))


async def test_button_turns_a_logged_meal_into_a_dish() -> None:
    """AC-63/AC-64: свёрток из трёх компонентов → продукт с плотностью и весом порции."""
    assistant, users, products = _build(
        StubNlu(_meal(
            _item("курица", "chicken_cooked", 140, 165, 31),
            _item("лаваш", "lavash", 50, 274, 8),
            _item("майонез", "mayo", 20, 680, 0.5),
        ))
    )
    user = await _new_user(users)
    logged = await assistant.process(user, "курица 140, лаваш 50, майонез 20")
    assert isinstance(logged, MealLogged)
    assert logged.meal.id is not None

    saved = await assistant.remember_dish(user, logged.meal.id, "свёрток")

    assert saved is not None
    total_g = 140 + 50 + 20
    assert saved.product.unit_weight_g == total_g  # дальше «свёрток» считается штукой (INV-7)
    # плотность = КБЖУ записи / вес записи; сверяем с тем, что реально записано
    expected = logged.meal.totals.kcal / total_g * 100
    assert abs(saved.product.density.kcal_per_100g - expected) < 0.5
    assert saved.product.source == "manual"


async def test_saving_a_dish_does_not_move_history() -> None:
    """AC-68/INV-3: снапшот съеденного после сохранения блюда не изменился ни на грамм."""
    assistant, users, products = _build(
        StubNlu(_meal(
            _item("курица", "chicken_cooked", 140, 165, 31),
            _item("лаваш", "lavash", 50, 274, 8),
        ))
    )
    user = await _new_user(users)
    logged = await assistant.process(user, "курица 140, лаваш 50")
    assert isinstance(logged, MealLogged)
    before = [(i.name_snapshot, i.grams, i.macros.kcal) for i in logged.meal.items]
    before_total = logged.meal.totals.kcal

    assert logged.meal.id is not None
    await assistant.remember_dish(user, logged.meal.id, "свёрток")

    after_meal = await assistant.get_meal(user, logged.meal.id)
    assert after_meal is not None
    assert [(i.name_snapshot, i.grams, i.macros.kcal) for i in after_meal.items] == before
    assert after_meal.totals.kcal == before_total


async def test_dish_without_weights_is_refused() -> None:
    """AC-65: позиция по своим цифрам без граммов — плотности не существует, блюдо не заводим."""
    stated = ParsedItem(
        name="шаурма", canonical_key="shawarma", unit=Unit.UNKNOWN,
        total_kcal=520, total_protein=45, total_fat=18, total_carb=35, confidence=0.9,
    )
    assistant, users, _ = _build(StubNlu(_meal(stated)))
    user = await _new_user(users)
    logged = await assistant.process(user, "шаурма 520 ккал 45/18/35")
    assert isinstance(logged, MealLogged)
    assert logged.meal.id is not None

    assert await assistant.remember_dish(user, logged.meal.id, "шаурма") is None


async def test_batch_phrase_divides_the_pot_and_logs_no_meal() -> None:
    """AC-69: «на 8 порций» → вес порции = котёл/8, и приём пищи при этом НЕ записан."""
    batch = ParsedMessage(
        intent=Intent.BATCH, confidence=0.9, batch_name="свёрток", batch_portions=8,
        items=[
            _item("курица", "chicken_cooked", 1100, 165, 31),
            _item("соус", "sauce", 300, 500, 1),
            _item("лаваш", "lavash", 400, 274, 8),
        ],
    )
    assistant, users, products = _build(StubNlu(batch))
    user = await _new_user(users)

    result = await assistant.process(user, "заготовка свёрток: ... на 8 порций")

    assert type(result).__name__ == "DishSaved"
    assert result.product.unit_weight_g == (1100 + 300 + 400) / 8  # 225 г
    day = await assistant.today(user)
    assert day.meals_count == 0, "заготовка — это не съеденное, день трогать нельзя"


async def test_batch_named_yield_beats_sum_of_inputs() -> None:
    """AC-71: вода выкипела — делим на названный выход, а не на сумму вложенного."""
    batch = ParsedMessage(
        intent=Intent.BATCH, confidence=0.9, batch_name="суп", batch_yield_g=1800,
        items=[
            _item("крупа", "grain", 300, 350, 7, 1, 77),
            _item("вода", "water", 2000, 0, 0, 0, 0)  # вода — ровно ноль, без дефолтов,
        ],
    )
    assistant, users, _ = _build(StubNlu(batch))
    user = await _new_user(users)

    result = await assistant.process(user, "заготовка суп: 300 г крупы, 2 л воды, вышло 1800 г")

    assert type(result).__name__ == "DishSaved"
    # 300 г крупы × 350/100 = 1050 ккал на 1800 г готового ≈ 58 ккал/100 г
    assert abs(result.product.density.kcal_per_100g - 1050 / 1800 * 100) < 1


async def test_cooking_the_same_dish_again_updates_it() -> None:
    """AC-66: имя уже в каталоге → обновляем плотность, дубль не заводим."""
    first = ParsedMessage(
        intent=Intent.BATCH, confidence=0.9, batch_name="соус", batch_portions=2,
        items=[_item("майонез", "mayo", 100, 680, 0.5)],
    )
    second = ParsedMessage(
        intent=Intent.BATCH, confidence=0.9, batch_name="соус", batch_portions=2,
        items=[_item("майонез", "mayo", 100, 680, 0.5), _item("вода", "water", 100, 0, 0)],
    )
    assistant, users, products = _build(StubNlu(first, second))
    user = await _new_user(users)

    await assistant.process(user, "заготовка соус: 100 г майонеза, на 2 порции")
    result = await assistant.process(user, "заготовка соус: 100 г майонеза, 100 г воды, на 2")

    assert type(result).__name__ == "DishSaved"
    keys = await products.all_keys()
    assert "соус_2" not in keys, "дубль заводить нельзя — обновляем существующее блюдо"
    assert result.product.density.kcal_per_100g < 680  # разбавили водой — плотность упала
