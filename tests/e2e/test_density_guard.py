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


async def test_retry_recovers_blank_estimate() -> None:
    # первый парс пустой, ретрай даёт норму → пинится корректно (120/100 * 400 = 480)
    assistant, users, products = _build(StubNlu(_fish(0, 0, 0, 0), _fish(120, 20, 5, 0)))
    user = await _new_user(users)

    result = await assistant.process(user, "пангасиус 400")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal == 480.0
    pinned = await products.get_by_key("pangasius")
    assert pinned is not None and pinned.density.kcal_per_100g == 120
