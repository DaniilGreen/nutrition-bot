"""E2E для Волны 2b: правка КБЖУ продукта — escape-хатч модели «БД фиксирует».

Проверяет: исправление влияет на будущие записи, но НЕ трогает прошлые снапшоты.
"""
from __future__ import annotations

import dataclasses

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.entities import Product
from app.application.results import MealLogged
from app.domain.value_objects import Density
from tests.e2e.test_meal_flow import _build, _new_user
from tests.fakes import InMemoryProductRepo, StubNlu


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


def _named(name: str, key: str) -> ParsedMessage:
    """Тот же разбор, но с заданным именем — StubNlu не смотрит на текст юзера."""
    msg = _mystery(300)
    return dataclasses.replace(
        msg, items=[dataclasses.replace(msg.items[0], name=name, canonical_key=key)]
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

    # ...но найтись по имени обязан: выверенный продукт тоже правится, если в него внесли
    # опечатку. Иначе ошибку не откатить (FR-15.1).
    assert len(await assistant.fixable_products(query="соус")) == 1


async def _seed(products: InMemoryProductRepo, *names: tuple[str, str]) -> None:
    """Кладём продукты в каталог напрямую: тест про поиск, а не про разбор реплики."""
    for name, key in names:
        await products.create(
            Product(canonical_key=key, display_name=name, density=Density(300, 5, 25, 15))
        )


async def test_search_finds_product_beyond_recent_limit() -> None:
    """FR-15.1 / баг #26: продукт старше 10 последних оценок недостижим без поиска."""
    assistant, _, products = _build(StubNlu(_mystery(300)))
    # тофу первым, следом 11 других → он уезжает за границу десятки
    await _seed(
        products,
        ("тофу молодой", "tofu_silken"),
        *[(f"продукт{i}", f"product_{i}") for i in range(11)],
    )

    recent = await assistant.fixable_products(limit=10)
    assert len(recent) == 10
    assert all("тофу" not in p.display_name for p in recent)  # вот он и «пропал»

    found = await assistant.fixable_products(query="тофу")
    assert [p.display_name for p in found] == ["тофу молодой"]


async def test_search_is_case_insensitive_and_partial() -> None:
    """Ищем по части названия и без оглядки на регистр — юзер пишет как придётся."""
    assistant, _, products = _build(StubNlu(_mystery(300)))
    await _seed(products, ("тофу молодой", "tofu_silken"), ("курица гриль", "chicken_grilled"))

    for q in ("тофу", "ТОФУ", "молод", "tofu"):  # ищем и по canonical_key
        assert [p.display_name for p in await assistant.fixable_products(query=q)] == [
            "тофу молодой"
        ], q
    assert await assistant.fixable_products(query="лосось") == []
