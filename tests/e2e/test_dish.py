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


async def test_named_dish_repeats_its_composition_without_llm() -> None:
    """AC-67/AC-68: имя блюда повторяет СОСТАВ, продукт в каталоге не заводится."""
    assistant, users, products = _build(
        StubNlu(_meal(
            _item("взвесь", "mix", 120, 200, 15, 13, 3),
            _item("лаваш", "lavash", 50, 274, 8, 1, 56),
        ))
    )
    user = await _new_user(users)
    logged = await assistant.process(user, "взвесь 120, лаваш 50")
    assert isinstance(logged, MealLogged)
    assert logged.meal.id is not None
    keys_before = await products.all_keys()

    assert await assistant.remember_dish(user, logged.meal.id, "свёрток") is not None
    # StubNlu исчерпан: если бот пойдёт в разбор — упадёт. Значит повтор идёт мимо LLM.
    again = await assistant.process(user, "свёрток")

    assert isinstance(again, MealLogged)
    assert [(i.name_snapshot, i.grams) for i in again.meal.items] == [
        (i.name_snapshot, i.grams) for i in logged.meal.items
    ]
    assert await products.all_keys() == keys_before, "сборка не должна плодить продукт"


async def test_template_survives_on_repeat() -> None:
    """Имя едет с повтором — шаблон жив, пока блюдо едят (FR-39.2)."""
    assistant, users, _ = _build(
        StubNlu(_meal(
            _item("взвесь", "mix", 120, 200, 15, 13, 3),
            _item("лаваш", "lavash", 50, 274, 8, 1, 56),
        ))
    )
    user = await _new_user(users)
    logged = await assistant.process(user, "взвесь 120, лаваш 50")
    assert isinstance(logged, MealLogged)
    assert logged.meal.id is not None
    await assistant.remember_dish(user, logged.meal.id, "свёрток")

    repeated = await assistant.process(user, "свёрток")

    assert isinstance(repeated, MealLogged)
    assert repeated.meal.template_name == "свёрток"


async def test_saving_a_dish_does_not_move_history() -> None:
    """AC-71/INV-3: снапшот съеденного после сохранения блюда не изменился ни на грамм."""
    assistant, users, _ = _build(
        StubNlu(_meal(
            _item("взвесь", "mix", 120, 200, 15, 13, 3),
            _item("лаваш", "lavash", 50, 274, 8, 1, 56),
        ))
    )
    user = await _new_user(users)
    logged = await assistant.process(user, "взвесь 120, лаваш 50")
    assert isinstance(logged, MealLogged)
    before = [(i.name_snapshot, i.grams, i.macros.kcal) for i in logged.meal.items]

    assert logged.meal.id is not None
    await assistant.remember_dish(user, logged.meal.id, "свёрток")

    after = await assistant.get_meal(user, logged.meal.id)
    assert after is not None
    assert [(i.name_snapshot, i.grams, i.macros.kcal) for i in after.items] == before


async def test_single_item_or_weightless_meal_is_not_a_dish() -> None:
    """AC-70: из одной позиции блюдо делать нечего, без веса — нечего повторять."""
    one = _meal(_item("взвесь", "mix", 120, 200, 15, 13, 3))
    assistant, users, _ = _build(StubNlu(one))
    user = await _new_user(users)
    logged = await assistant.process(user, "взвесь 120")
    assert isinstance(logged, MealLogged)
    assert logged.meal.id is not None

    assert await assistant.remember_dish(user, logged.meal.id, "свёрток") is None


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


async def _saved_wrap(extra_nlu: ParsedMessage | None = None):
    """Записанный свёрток с именем; StubNlu держит ещё один разбор на случай похода в LLM."""
    msgs = [_meal(
        _item("взвесь", "mix", 120, 200, 15, 13, 3),
        _item("лаваш", "lavash", 50, 274, 8, 1, 56),
    )]
    if extra_nlu is not None:
        msgs.append(extra_nlu)
    assistant, users, products = _build(StubNlu(*msgs))
    user = await _new_user(users)
    logged = await assistant.process(user, "взвесь 120, лаваш 50")
    assert isinstance(logged, MealLogged) and logged.meal.id is not None
    await assistant.remember_dish(user, logged.meal.id, "свёрток")
    return assistant, user, logged.meal


async def test_dish_name_inside_a_longer_message_is_not_swallowed() -> None:
    """«свёрток и кофе» — не своё блюдо целиком. Перехват выкинул бы кофе из дня молча."""
    coffee_and_wrap = _meal(
        _item("свёрток", "wrap", 170, 260, 20, 11, 17),
        _item("кофе", "coffee", 200, 2, 0.1, 0, 0.3),
    )
    assistant, user, _ = await _saved_wrap(coffee_and_wrap)

    result = await assistant.process(user, "свёрток и кофе")

    assert isinstance(result, MealLogged)
    names = [i.name_snapshot for i in result.meal.items]
    assert "кофе" in names, "кофе пропал — сообщение перехвачено как свёрток целиком"


async def test_count_repeats_the_dish_n_times() -> None:
    """«2 свёртка» — тот же состав ×2 одной записью, без LLM (StubNlu уже пуст)."""
    assistant, user, src = await _saved_wrap()

    result = await assistant.process(user, "2 свёртка")

    assert isinstance(result, MealLogged)
    assert [i.grams for i in result.meal.items] == [i.grams * 2 for i in src.items]
    assert abs(result.meal.totals.kcal - src.totals.kcal * 2) < 0.01
