"""E2E FR-20: разрешение неоднозначных generic-имён по истории пользователя."""
from __future__ import annotations

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.entities import Product
from app.application.results import MealLogged, VariantClarificationNeeded
from app.domain.value_objects import Density
from tests.e2e.test_meal_flow import _build, _new_user
from tests.fakes import InMemoryProductRepo, StubNlu

# семейство «курица»: (key, name, kcal, protein, fat) на 100 г
_CHICKEN = [
    ("chicken_breast_boiled", "Куриная грудка отварная", 130, 29, 1.8),
    ("chicken_breast_fried", "Куриная грудка жареная", 200, 30, 8),
    ("chicken_grilled", "Курица гриль", 200, 22, 12),
]


async def _seed_chicken(products: InMemoryProductRepo) -> None:
    for key, name, kcal, p, f in _CHICKEN:
        await products.create(
            Product(
                canonical_key=key,
                display_name=name,
                category="chicken",
                density=Density(kcal, p, f, 0),
                source="curated",
                is_estimate=False,
            )
        )


def _item(name: str, key: str, *, ambiguous: bool) -> ParsedMessage:
    return ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name=name,
                canonical_key=key,
                unit=Unit.GRAM,
                grams=200,
                ambiguous=ambiguous,
                kcal_per_100g=165,  # LLM-оценка; для известного продукта игнорится (INV-2)
                protein_per_100g=31,
                fat_per_100g=3.6,
                carb_per_100g=0,
                confidence=0.9,
            )
        ],
    )


def _generic() -> ParsedMessage:
    return _item("курица", "chicken", ambiguous=True)


def _boiled() -> ParsedMessage:
    return _item("курица отварная", "chicken_breast_boiled", ambiguous=False)


def _grilled() -> ParsedMessage:
    return _item("курица гриль", "chicken_grilled", ambiguous=False)


async def test_variant_cold_start_asks() -> None:
    # generic «курица» без истории → вопрос с вариантами семейства
    assistant, users, products = _build(StubNlu(_generic()))
    await _seed_chicken(products)
    user = await _new_user(users)

    result = await assistant.process(user, "курица 200")

    assert isinstance(result, VariantClarificationNeeded)
    assert {p.canonical_key for p in result.options} == {
        "chicken_breast_boiled",
        "chicken_breast_fried",
        "chicken_grilled",
    }


async def test_variant_uses_history() -> None:
    # ел отварную дважды → generic «курица» молча резолвится в отварную (130/100 * 200 = 260)
    assistant, users, products = _build(StubNlu(_boiled(), _boiled(), _generic()))
    await _seed_chicken(products)
    user = await _new_user(users)

    await assistant.process(user, "курица отварная 200")
    await assistant.process(user, "курица отварная 200")
    result = await assistant.process(user, "курица 200")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal == 260.0  # отварная, не гриль (была бы 400)


async def test_specific_variant_not_overridden_by_history() -> None:
    # история отварной не перетирает явный «курица гриль» (200/100 * 200 = 400)
    assistant, users, products = _build(StubNlu(_boiled(), _grilled()))
    await _seed_chicken(products)
    user = await _new_user(users)

    await assistant.process(user, "курица отварная 200")
    result = await assistant.process(user, "курица гриль 200")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal == 400.0  # гриль, не отварная (была бы 260)
