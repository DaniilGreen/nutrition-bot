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


# ---------- по итогам ревью 15.10.2026 ----------

def _single(name: str, key: str, grams: float, d: tuple) -> ParsedMessage:
    return ParsedMessage(
        intent=Intent.BATCH, confidence=0.9, batch_name=name,
        items=[_ing(name, key, grams, *d)],
    )


async def test_big_batch_density_is_not_capped_like_a_portion() -> None:
    """Регрессия ревью: потолки ПОРЦИИ (5000 ккал, 600 г белка на позицию) резали партию —
    3 кг курицы давали 112 ккал / 20 г белка на 100 г вместо 165 / 31."""
    assistant, users, _ = _build(StubNlu(_single("курица партия", "chicken_batch", 3000,
                                                 (165, 31, 3.6, 0))))
    user = await _new_user(users)

    saved = await assistant.process(user, "рецепт курица партия: 3 кг курицы")

    d = saved.product.density
    assert abs(d.kcal_per_100g - 165) < 1 and abs(d.protein_per_100g - 31) < 0.1


async def test_batch_with_two_ingredients_is_exact_at_scale() -> None:
    assistant, users, _ = _build(StubNlu(_recipe(chicken_g=2500, mayo_g=200)))
    user = await _new_user(users)

    saved = await assistant.process(user, "рецепт курица с мазиком: …")

    d = saved.product.density
    assert abs(d.kcal_per_100g - (2500 * 1.65 + 200 * 6.8) / 2700 * 100) < 1
    assert abs(d.protein_per_100g - (2500 * 0.31 + 200 * 0.01) / 2700 * 100) < 0.1


async def test_ingredient_gate_reconciles_and_spares_alcohol() -> None:
    """INV-11 на уровне плотности: «400 ккал при 10 г белка» сверяется Атвотером до 40;
    водка (INV-14) Атвотером не трогается."""
    weird = _single("смесь", "weird_mix", 100, (400, 10, 0, 0))
    vodka = _single("водка", "vodka", 500, (235, 0, 0, 0.1))
    assistant, users, _ = _build(StubNlu(weird, vodka))
    user = await _new_user(users)

    a = await assistant.process(user, "рецепт смесь: …")
    b = await assistant.process(user, "рецепт водка: …")

    assert abs(a.product.density.kcal_per_100g - 40) < 1
    assert abs(b.product.density.kcal_per_100g - 235) < 1


async def test_branded_beer_keeps_its_ethanol() -> None:
    """INV-14 по словам человека: «пиво Heineken» резолвится в продукт этикетки «Heineken»,
    в имени которого алкоголя нет, — этанол всё равно не уходит в Атвотер."""
    beer_parse = _ing("пиво Heineken", "heineken", 500, 42, 0.5, 0, 3.2)
    stew = ParsedMessage(intent=Intent.BATCH, confidence=0.9, batch_name="свинина в пиве",
                         items=[_ing("свинина", "pork", 1000, 250, 17, 20, 0), beer_parse])
    assistant, users, products = _build(StubNlu(stew))
    await products.create(Product(canonical_key="heineken", display_name="Heineken",
                                  density=Density(42, 0.5, 0, 3.2), source="label",
                                  is_estimate=False))
    user = await _new_user(users)

    saved = await assistant.process(user, "рецепт свинина в пиве: …")

    assert abs(saved.product.density.kcal_per_100g - (1000 * 2.5 + 500 * 0.42) / 1500 * 100) < 1


async def test_edit_inherits_estimate_from_ingredients() -> None:
    assistant, users, products = _build(StubNlu(_recipe()))
    user = await _new_user(users)
    saved = await assistant.process(user, "рецепт курица с мазиком: …")
    curated = await products.create(Product(
        canonical_key="rice", display_name="рис отварной", density=Density(130, 2.7, 0.3, 28),
        source="curated", is_estimate=False,
    ))

    rid = saved.product.id
    mixed = await assistant.edit_recipe(
        rid, [SetPick(grams=500, src=0), SetPick(grams=500, product_id=curated.id)], yield_g=None
    )
    # Фейк отдаёт тот же объект продукта — снимаем значение сразу, до следующей правки.
    mixed_estimate = mixed.is_estimate
    clean = await assistant.edit_recipe(rid, [SetPick(grams=500, src=1)], yield_g=None)

    assert mixed_estimate is True, "оценочная курица в составе — рецепт с «≈»"
    assert clean.is_estimate is False, "только выверенный рис — «≈» снимается"


async def test_plain_product_is_never_edited_as_a_recipe() -> None:
    """M4: правка по чужому id не трогает обычный продукт."""
    assistant, users, products = _build(StubNlu(_recipe()))
    user = await _new_user(users)
    await assistant.process(user, "рецепт курица с мазиком: …")
    plain = await products.get_by_key("chicken_baked")
    mayo = await products.get_by_key("mayo_home")

    assert await assistant.edit_recipe(plain.id, [SetPick(grams=100, product_id=mayo.id)],
                                       yield_g=None) is None
    after = await products.get_by_id(plain.id)
    assert after.source != "recipe" and after.density.kcal_per_100g == 165


async def test_recipe_merged_with_its_own_ingredient_does_not_inflate() -> None:
    """Слили ингредиент в сам рецепт — ссылка на себя обнуляется, плотность не растёт
    от сохранения к сохранению."""
    assistant, users, products = _build(StubNlu(_recipe()))
    user = await _new_user(users)
    saved = await assistant.process(user, "рецепт курица с мазиком: …")
    rid = saved.product.id
    chicken = await products.get_by_key("chicken_baked")
    await assistant.merge_products(rid, [chicken.id])

    picks = [SetPick(grams=1100, src=0), SetPick(grams=200, src=1)]
    first = await assistant.edit_recipe(rid, picks, yield_g=None)
    second = await assistant.edit_recipe(rid, picks, yield_g=None)

    assert (await assistant.recipe_of(rid)).items[0].product_id is None
    assert abs(first.density.kcal_per_100g - second.density.kcal_per_100g) < 0.01


async def test_portion_weight_follows_the_new_yield() -> None:
    """Партию делили на 4 — после правки порция = новый вес / 4, а не старые 500 г."""
    plov = ParsedMessage(intent=Intent.BATCH, confidence=0.9, batch_name="плов", batch_portions=4,
                         items=[_ing("рис", "rice_raw", 1000, 130, 2.7, 0.3, 28),
                                _ing("свинина", "pork", 1000, 250, 17, 20, 0)])
    assistant, users, _ = _build(StubNlu(plov))
    user = await _new_user(users)
    saved = await assistant.process(user, "рецепт плов: … на 4 порции")
    assert saved.product.unit_weight_g == 500

    updated = await assistant.edit_recipe(
        saved.product.id, [SetPick(grams=1000, src=0), SetPick(grams=1000, src=1)], yield_g=1600
    )

    assert updated.unit_weight_g == 400


async def test_impossible_yield_is_refused_not_clamped() -> None:
    from app.application.meal_service import RecipeRejected

    assistant, users, _ = _build(StubNlu(_recipe()))
    user = await _new_user(users)
    saved = await assistant.process(user, "рецепт курица с мазиком: …")
    picks = [SetPick(grams=1100, src=0), SetPick(grams=200, src=1)]

    for bad_yield in (100, 60000):
        try:
            await assistant.edit_recipe(saved.product.id, picks, yield_g=bad_yield)
        except RecipeRejected:
            continue
        raise AssertionError(f"вес готового {bad_yield} г прошёл молча")


async def test_stale_position_is_refused() -> None:
    """Состав переписали с другого устройства — позиция №0 уже другая: отказ, а не подмена."""
    assistant, users, _ = _build(StubNlu(_recipe()))
    user = await _new_user(users)
    saved = await assistant.process(user, "рецепт курица с мазиком: …")

    stale = await assistant.edit_recipe(
        saved.product.id, [SetPick(grams=500, src=0, name="майонез домашний")], yield_g=None
    )

    assert stale is None


async def test_manual_density_fix_keeps_the_recipe() -> None:
    """Форма «Править» (переименование/плотность) не превращает рецепт в обычный продукт."""
    assistant, users, _ = _build(StubNlu(_recipe()))
    user = await _new_user(users)
    saved = await assistant.process(user, "рецепт курица с мазиком: …")

    fixed = await assistant.fix_product_density(saved.product.id, Density(190, 24, 9, 1))

    assert fixed.source == "recipe"
    assert await assistant.recipe_of(saved.product.id) is not None


# ---------- второй проход ревью: тесты, которые убивают выжившие мутации M6/M14/M16 ----------

async def test_gate_applies_to_an_atwater_inconsistent_catalog_density() -> None:
    """M6: правка руками (FR-15) не сверяет Атвотер, поэтому в каталоге МОЖЕТ лежать «400 ккал
    при 10 г белка». Рецепт обязан сверить её сам — до 40, как это делает превью."""
    assistant, users, products = _build(StubNlu())
    rec = await products.create(Product(
        canonical_key="rec_k6", display_name="смесь k6", density=Density(100, 5, 5, 5),
        source="recipe", is_estimate=True,
    ))
    odd = await products.create(Product(
        canonical_key="odd_k6", display_name="паста k6", density=Density(130, 5, 1, 25),
        source="llm", is_estimate=True,
    ))
    await assistant.fix_product_density(odd.id, Density(400, 10, 0, 0))

    updated = await assistant.edit_recipe(
        rec.id, [SetPick(grams=500, product_id=odd.id)], yield_g=None
    )

    assert abs(updated.density.kcal_per_100g - 40) < 1


async def test_branded_wine_keeps_its_ethanol() -> None:
    """M14: «вино Château Margaux» резолвится в продукт этикетки «Château Margaux», где слова
    про алкоголь нет. 85 ккал при Атвотере 10.8 — за допуском, так что без флага этанол ушёл
    бы. (Heineken 42 ккал в допуске и этого не видит.)"""
    wine = _ing("вино Château Margaux", "chateau_margaux", 500, 85, 0.1, 0, 2.6)
    stew = ParsedMessage(intent=Intent.BATCH, confidence=0.9, batch_name="говядина по-бургундски",
                         items=[_ing("говядина", "beef_k14", 1000, 250, 26, 15, 0), wine])
    assistant, users, products = _build(StubNlu(stew))
    await products.create(Product(canonical_key="chateau_margaux", display_name="Château Margaux",
                                  density=Density(85, 0.1, 0, 2.6), source="label",
                                  is_estimate=False))
    user = await _new_user(users)

    saved = await assistant.process(user, "рецепт говядина по-бургундски: …")

    assert abs(saved.product.density.kcal_per_100g - (1000 * 2.5 + 500 * 0.85) / 1500 * 100) < 1
    assert (await assistant.recipe_of(saved.product.id)).items[1].alcohol is True


async def test_stored_self_reference_is_treated_as_snapshot() -> None:
    """M16: строка состава, указывающая на сам рецепт (старые/испорченные данные), — снимок:
    иначе каждое сохранение подмешивало бы новую плотность в саму себя."""
    from app.application.entities import Recipe, RecipeItem

    assistant, users, products = _build(StubNlu())
    chicken = await products.create(Product(
        canonical_key="chicken_k16", display_name="курица k16", density=Density(165, 31, 3.6, 0),
        source="llm", is_estimate=True,
    ))
    rec = await products.create(Product(
        canonical_key="rec_k16", display_name="курица с мазиком k16",
        density=Density(183.5, 23.7, 9.1, 0.9), source="recipe", is_estimate=True,
    ))
    await products.set_recipe(rec.id, Recipe(items=[
        RecipeItem("курица k16", 1000, chicken.density, chicken.id, True),
        RecipeItem("курица с мазиком k16", 500, Density(183.5, 23.7, 9.1, 0.9), rec.id, True),
    ]))

    assert (await assistant.recipe_of(rec.id)).items[1].product_id is None

    picks = [SetPick(grams=1000, src=0), SetPick(grams=500, src=1)]
    first = (await assistant.edit_recipe(rec.id, picks, yield_g=800)).density.kcal_per_100g
    second = (await assistant.edit_recipe(rec.id, picks, yield_g=800)).density.kcal_per_100g
    assert abs(first - second) < 0.01


async def test_mutual_recipes_are_refused() -> None:
    """A в B, а B в A — плотности раздувались бы друг через друга на каждом сохранении."""
    from app.application.entities import Recipe, RecipeItem

    assistant, users, products = _build(StubNlu())
    a = await products.create(Product(canonical_key="rec_a", display_name="соус",
                                      density=Density(300, 1, 30, 5), source="recipe"))
    b = await products.create(Product(canonical_key="rec_b", display_name="салат с соусом",
                                      density=Density(120, 3, 10, 5), source="recipe"))
    await products.set_recipe(b.id, Recipe(items=[RecipeItem("соус", 50, a.density, a.id)]))

    assert await assistant.edit_recipe(a.id, [SetPick(grams=100, product_id=b.id)],
                                       yield_g=None) is None


async def test_bot_says_why_a_recipe_is_refused() -> None:
    """Отказ рецепта доходит до человека своим текстом, а не подсказкой «вес 20–400 кг»."""
    from app.application.results import Unknown
    from app.bot.formatters import format_result

    bad = ParsedMessage(
        intent=Intent.BATCH, confidence=0.9, batch_name="курица с мазиком", batch_yield_g=100,
        items=[_ing(*CHICKEN[:2], 1100, *CHICKEN[2:]), _ing(*MAYO[:2], 200, *MAYO[2:])],
    )
    assistant, users, _ = _build(StubNlu(bad))
    user = await _new_user(users)

    result = await assistant.process(user, "рецепт курица с мазиком: … вышло 100 г")

    assert isinstance(result, Unknown) and result.say
    text = format_result(result)
    assert "вес готового" in text.lower() and "20–400" not in text


async def test_zero_yield_from_the_model_means_not_named() -> None:
    zero = ParsedMessage(
        intent=Intent.BATCH, confidence=0.9, batch_name="курица с мазиком", batch_yield_g=0,
        items=[_ing(*CHICKEN[:2], 1100, *CHICKEN[2:]), _ing(*MAYO[:2], 200, *MAYO[2:])],
    )
    assistant, users, _ = _build(StubNlu(zero))
    user = await _new_user(users)

    saved = await assistant.process(user, "рецепт курица с мазиком: …")

    assert abs(saved.product.density.kcal_per_100g - (1100 * 1.65 + 200 * 6.8) / 1300 * 100) < 1


async def test_near_bound_density_is_clamped_not_refused() -> None:
    """Топлёное масло у самой границы — шум округлений, жмём; явная опечатка — отказ."""
    ghee = ParsedMessage(intent=Intent.BATCH, confidence=0.9, batch_name="гхи", batch_yield_g=950,
                         items=[_ing("масло сливочное", "butter", 1000, 717, 0.9, 81, 0.1)])
    assistant, users, _ = _build(StubNlu(ghee))
    user = await _new_user(users)

    saved = await assistant.process(user, "рецепт гхи: 1 кг масла, вышло 950 г")

    assert saved.product.density.fat_per_100g <= 100
    assert abs(saved.product.density.kcal_per_100g - 717 / 0.95) < 30
