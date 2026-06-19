"""E2E для Волны 2b: правка КБЖУ продукта — escape-хатч модели «БД фиксирует».

Проверяет: исправление влияет на будущие записи, но НЕ трогает прошлые снапшоты.
"""
from __future__ import annotations

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.results import MealLogged
from app.domain.value_objects import Density
from tests.e2e.test_meal_flow import _build, _new_user
from tests.fakes import StubNlu


def _mystery(kcal: float) -> ParsedMessage:
    return ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="загадочный соус",
                canonical_key="mystery_sauce",
                unit=Unit.GRAM,
                grams=100,
                kcal_per_100g=kcal,
                protein_per_100g=5,  # макросы согласованы с 300 ккал (Атвотер ≈305), чтобы
                fat_per_100g=25,  # авто-сверка INV-10 не трогала оценку — тест про правку каталога
                carb_per_100g=15,
                confidence=0.9,
            )
        ],
    )


async def test_fix_product_density_affects_future_not_past() -> None:
    # AI впервые оценил продукт в 300 ккал/100 г (и так бы и осталось — это оценка).
    assistant, users, _ = _build(StubNlu(_mystery(300), _mystery(300)))
    user = await _new_user(users)

    first = await assistant.process(user, "соус 100 г")
    assert isinstance(first, MealLogged)
    assert first.meal.totals.kcal == 300.0

    fixable = await assistant.fixable_products()
    assert len(fixable) == 1
    product_id = fixable[0].id
    assert product_id is not None

    # Владелец правит на верные 150 ккал/100 г.
    fixed = await assistant.fix_product_density(product_id, Density(150, 5, 5, 5))
    assert fixed is not None
    assert fixed.is_estimate is False

    # Новая запись считается уже по 150.
    second = await assistant.process(user, "соус 100 г")
    assert isinstance(second, MealLogged)
    assert second.meal.totals.kcal == 150.0

    # Прошлый снапшот не изменился (история стабильна).
    assert first.meal.totals.kcal == 300.0

    # После правки продукт больше не «оценка» — из списка правок ушёл.
    assert await assistant.fixable_products() == []
