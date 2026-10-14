"""Правка состава рецепта (FR-39.7) и граница партии.

Главное: состав сохраняется; правка считает по ТЕКУЩИМ плотностям ингредиентов; съеденное
не двигается (INV-3); слияние дублей не оставляет состав ссылаться на удалённое.
"""
from __future__ import annotations

from app.application.dto import Intent, ParsedItem, ParsedMessage, SetPick, Unit
from app.application.entities import Product
from app.application.results import MealLogged
from app.domain.value_objects import Density
from tests.e2e.test_meal_flow import _build, _new_user
from tests.fakes import StubNlu


def _ing(
    name: str, key: str, grams: float, kcal: float, p: float, f: float, c: float
) -> ParsedItem:
    return ParsedItem(
        name=name, canonical_key=key, unit=Unit.GRAM, grams=grams,
        kcal_per_100g=kcal, protein_per_100g=p, fat_per_100g=f, carb_per_100g=c, confidence=0.9,
    )


CHICKEN = ("куриная грудка запечённая", "chicken_baked", 165, 31, 3.6, 0)
MAYO = ("майонез домашний", "mayo_home", 680, 1, 75, 1)


def _recipe(chicken_g: float = 1100, mayo_g: float = 200) -> ParsedMessage:
    return ParsedMessage(
        intent=Intent.BATCH, confidence=0.9, batch_name="курица с мазиком",
        items=[_ing(*CHICKEN[:2], chicken_g, *CHICKEN[2:]), _ing(*MAYO[:2], mayo_g, *MAYO[2:])],
    )


async def test_recipe_keeps_its_composition() -> None:
    """AC-80: состав с граммами и ссылками на ингредиенты сохраняется вместе с рецептом."""
    assistant, users, products = _build(StubNlu(_recipe()))
    user = await _new_user(users)
    saved = await assistant.process(user, "рецепт курица с мазиком: …")
    pid = saved.product.id

    recipe = await assistant.recipe_of(pid)

    assert recipe is not None
    assert [(i.name, i.grams) for i in recipe.items] == [
        ("куриная грудка запечённая", 1100), ("майонез домашний", 200),
    ]
    assert all(i.product_id for i in recipe.items)


async def test_big_batch_is_not_clamped_to_a_portion() -> None:
    """Регрессия: граница порции 2000 г резала 2.5 кг курицы до 2 кг — плотность врала."""
    assistant, users, _ = _build(StubNlu(_recipe(chicken_g=2500, mayo_g=0.0001)))
    user = await _new_user(users)
    saved = await assistant.process(user, "рецепт курица с мазиком: 2.5 кг курицы")

    recipe = await assistant.recipe_of(saved.product.id)

    assert recipe.items[0].grams == 2500, "партия обрезана границей порции"


async def test_edit_recomputes_density_and_history_stays() -> None:
    """AC-81: меньше майонеза → плотность ниже; съеденное раньше не изменилось (INV-3)."""
    eat = ParsedMessage(intent=Intent.MEAL, confidence=0.9, items=[ParsedItem(
        name="курица с мазиком", canonical_key="курица_с_мазиком", unit=Unit.GRAM, grams=150,
        confidence=0.9,
    )])
    assistant, users, products = _build(StubNlu(_recipe(), eat))
    user = await _new_user(users)
    saved = await assistant.process(user, "рецепт курица с мазиком: …")
    pid = saved.product.id
    before_density = saved.product.density.kcal_per_100g
    logged = await assistant.process(user, "курица с мазиком 150")
    assert isinstance(logged, MealLogged)
    eaten_kcal = logged.meal.items[0].macros.kcal

    updated = await assistant.edit_recipe(
        pid, [SetPick(grams=1100, src=0), SetPick(grams=120, src=1)], yield_g=None
    )

    expected = (1100 * 1.65 + 120 * 6.8) / 1220 * 100
    assert updated is not None and abs(updated.density.kcal_per_100g - expected) < 1
    assert updated.density.kcal_per_100g < before_density
    assert updated.source == "recipe"
    meal = await assistant.get_meal(user, logged.meal.id)
    assert meal.items[0].macros.kcal == eaten_kcal, "правка рецепта сдвинула историю"


async def test_edit_uses_the_current_ingredient_density() -> None:
    """Поправил майонез через FR-15 — правка рецепта считает уже по исправленной плотности."""
    assistant, users, products = _build(StubNlu(_recipe()))
    user = await _new_user(users)
    saved = await assistant.process(user, "рецепт курица с мазиком: …")
    mayo = await products.get_by_key("mayo_home")
    await assistant.fix_product_density(mayo.id, Density(300, 1, 33, 1))

    updated = await assistant.edit_recipe(
        saved.product.id, [SetPick(grams=1100, src=0), SetPick(grams=200, src=1)], yield_g=None
    )

    expected = (1100 * 1.65 + 200 * 3.0) / 1300 * 100
    assert abs(updated.density.kcal_per_100g - expected) < 1


async def test_yield_divides_the_pot() -> None:
    assistant, users, _ = _build(StubNlu(_recipe()))
    user = await _new_user(users)
    saved = await assistant.process(user, "рецепт курица с мазиком: …")

    updated = await assistant.edit_recipe(
        saved.product.id, [SetPick(grams=1100, src=0), SetPick(grams=200, src=1)], yield_g=1000
    )

    assert abs(updated.density.kcal_per_100g - (1100 * 1.65 + 200 * 6.8) / 1000 * 100) < 1
    assert (await assistant.recipe_of(saved.product.id)).yield_g == 1000


async def test_merged_ingredient_points_to_the_winner() -> None:
    """AC-82: майонез слили в другой — состав ссылается на выжившего и считает его плотностью."""
    assistant, users, products = _build(StubNlu(_recipe()))
    user = await _new_user(users)
    saved = await assistant.process(user, "рецепт курица с мазиком: …")
    mayo = await products.get_by_key("mayo_home")
    winner = await products.create(Product(
        canonical_key="mayo", display_name="майонез", density=Density(600, 1, 66, 1),
        source="curated", is_estimate=False,
    ))
    await assistant.merge_products(winner.id, [mayo.id])

    recipe = await assistant.recipe_of(saved.product.id)

    assert recipe.items[1].product_id == winner.id
    assert recipe.items[1].density.kcal_per_100g == 600


async def test_recipe_without_composition_can_be_assembled() -> None:
    """AC-83: рецепт до 15.10 — состава нет; собираем из каталога по product_id."""
    assistant, users, products = _build(StubNlu())
    old = await products.create(Product(
        canonical_key="курица_с_мазиком", display_name="курица с мазиком",
        density=Density(183.5, 23.7, 9.1, 0.9), source="recipe", is_estimate=True,
    ))
    chicken = await products.create(Product(
        canonical_key="chicken_baked", display_name="куриная грудка запечённая",
        density=Density(165, 31, 3.6, 0), source="llm", is_estimate=True,
    ))
    assert await assistant.recipe_of(old.id) is None

    updated = await assistant.edit_recipe(
        old.id, [SetPick(grams=500, product_id=chicken.id)], yield_g=None
    )

    assert updated is not None and abs(updated.density.kcal_per_100g - 165) < 0.5
    assert len((await assistant.recipe_of(old.id)).items) == 1


async def test_edit_refuses_bad_input() -> None:
    assistant, users, products = _build(StubNlu(_recipe()))
    user = await _new_user(users)
    saved = await assistant.process(user, "рецепт курица с мазиком: …")
    pid = saved.product.id
    plain = await products.get_by_key("chicken_baked")

    async def edit(target: int, *picks: SetPick):
        return await assistant.edit_recipe(target, list(picks), yield_g=None)

    assert await edit(pid, SetPick(grams=100, src=9)) is None
    assert await edit(pid, SetPick(grams=100, product_id=pid)) is None
    assert await edit(pid, SetPick(grams=100, product_id=99999)) is None
    assert await edit(plain.id, SetPick(grams=100, src=0)) is None, "не рецепт так не правят"
    assert await edit(pid) is None
