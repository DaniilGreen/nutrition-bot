"""E2E на сопоставление продуктов: защита от слияния разных продуктов в один.

Воспроизводит реальный баг: LLM вернула canonical_key="unknown" для разных
продуктов → все наследовали плотность первого (апельсиновый сок), и протеин
показывал 0.9 г белка/100 г вместо ~75.
"""
from __future__ import annotations

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.results import MealLogged
from tests.e2e.test_meal_flow import _build, _new_user
from tests.fakes import StubNlu


def _item(name: str, key: str, kcal: float, protein: float) -> ParsedMessage:
    return ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name=name,
                canonical_key=key,
                unit=Unit.GRAM,
                grams=100,
                kcal_per_100g=kcal,
                protein_per_100g=protein,
                fat_per_100g=1,
                carb_per_100g=1,
                confidence=0.9,
            )
        ],
    )


async def test_junk_unknown_key_does_not_merge_foods() -> None:
    assistant, users, products = _build(
        StubNlu(
            _item("апельсиновый сок", "unknown", 45, 0.9),
            _item("протеин whey", "unknown", 370, 75),
        )
    )
    user = await _new_user(users)

    await assistant.process(user, "сок 100 г")
    result = await assistant.process(user, "протеин 100 г")

    assert isinstance(result, MealLogged)
    # белок протеина (75 на 100 г), а не унаследованный от сока (0.9)
    assert result.meal.items[0].macros.protein == 75.0
    assert len(await products.all_keys()) == 2  # два разных продукта, не слились


async def test_clean_key_same_food_still_merges_and_pins() -> None:
    # Детерминизм не сломан: чистый ключ переиспользуется, плотность пиннится.
    assistant, users, products = _build(
        StubNlu(_item("курица", "chicken", 165, 31), _item("курица", "chicken", 999, 80))
    )
    user = await _new_user(users)

    r1 = await assistant.process(user, "курица 100 г")
    r2 = await assistant.process(user, "курица 100 г")

    assert isinstance(r1, MealLogged)
    assert isinstance(r2, MealLogged)
    assert r1.meal.totals.kcal == r2.meal.totals.kcal == 165.0
    assert await products.all_keys() == ["chicken"]
