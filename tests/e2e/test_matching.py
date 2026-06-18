"""E2E на сопоставление продуктов: защита от слияния разных продуктов + RAG-матч.

Воспроизводит реальный баг: LLM вернула canonical_key="unknown" для разных
продуктов → все наследовали плотность первого (сок). Плюс RAG: имя совпало с
каталогом → берём выверенную строку, а не выдуманные LLM числа.
"""
from __future__ import annotations

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.entities import Product
from app.application.results import MealLogged
from app.domain.value_objects import Density
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
    assert result.meal.items[0].macros.protein == 75.0
    assert len(await products.all_keys()) == 2


async def test_clean_key_same_food_still_merges_and_pins() -> None:
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


async def test_rag_prefers_catalog_over_llm_estimate() -> None:
    # В каталоге уже есть выверенный «Борщ»; LLM дала другой ключ и абсурдные 999 ккал.
    assistant, users, products = _build(StubNlu(_item("Борщ", "krasniy_borsch_x", 999, 80)))
    user = await _new_user(users)
    await products.create(
        Product(
            canonical_key="borscht",
            display_name="Борщ",
            density=Density(50, 2.5, 2.5, 5),
            source="curated",
            is_estimate=False,
        )
    )

    result = await assistant.process(user, "борщ 100 г")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal == 50.0  # из каталога, не выдуманные 999
    assert await products.all_keys() == ["borscht"]  # новый продукт не создан
