"""E2E FR-20: детерминированный детект варианта (квалификатор в тексте) + резолв по истории."""
from __future__ import annotations

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.entities import Product
from app.application.results import ClarificationNeeded, MealLogged, VariantClarificationNeeded
from app.domain.value_objects import Density
from tests.e2e.test_meal_flow import _build, _new_user
from tests.fakes import InMemoryProductRepo, StubNlu

_CHICKEN = [
    ("chicken_breast_boiled", "Куриная грудка отварная", 130, 29, 1.8),
    ("chicken_breast_fried", "Куриная грудка жареная", 200, 30, 8),
    ("chicken_grilled", "Курица гриль", 200, 22, 12),
]
_TVOROG = [
    ("tvorog_5", "Творог 5%", 120, 16, 5),
    ("tvorog_9", "Творог 9%", 160, 16, 9),
]


async def _seed(products: InMemoryProductRepo, rows: list, category: str) -> None:
    for key, name, kcal, p, f in rows:
        await products.create(
            Product(
                canonical_key=key,
                display_name=name,
                category=category,
                density=Density(kcal, p, f, 0),
                source="curated",
                is_estimate=False,
            )
        )


def _item(name: str, key: str) -> ParsedMessage:
    # kcal_per_100g != 0 — чтобы не триггерить ретрай нулевой оценки
    return ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name=name,
                canonical_key=key,
                unit=Unit.GRAM,
                grams=200,
                kcal_per_100g=165,
                protein_per_100g=31,
                fat_per_100g=3.6,
                carb_per_100g=0,
                confidence=0.9,
            )
        ],
    )


async def test_generic_cold_start_asks() -> None:
    # «курица» без квалификатора и без истории → вопрос
    assistant, users, products = _build(StubNlu(_item("курица", "chicken")))
    await _seed(products, _CHICKEN, "chicken")
    user = await _new_user(users)

    result = await assistant.process(user, "курица 200")

    assert isinstance(result, VariantClarificationNeeded)
    assert {p.canonical_key for p in result.options} == {
        "chicken_breast_boiled",
        "chicken_breast_fried",
        "chicken_grilled",
    }


async def test_tvorog_generic_asks_even_if_llm_picked_concrete() -> None:
    # РЕГРЕССИЯ бага: LLM вернул конкретный tvorog_5 + ambiguous=False, но «творог» без
    # квалификатора → всё равно спросить (раньше молча логировался 5%).
    assistant, users, products = _build(StubNlu(_item("творог", "tvorog_5")))
    await _seed(products, _TVOROG, "tvorog")
    user = await _new_user(users)

    result = await assistant.process(user, "творог 150")

    assert isinstance(result, VariantClarificationNeeded)
    assert {p.canonical_key for p in result.options} == {"tvorog_5", "tvorog_9"}


async def test_qualifier_picks_variant_overriding_llm() -> None:
    # «творог 9%» — квалификатор «9%» → ровно tvorog_9, даже если LLM промахнулся ключом
    assistant, users, products = _build(StubNlu(_item("творог 9%", "tvorog_5")))
    await _seed(products, _TVOROG, "tvorog")
    user = await _new_user(users)

    result = await assistant.process(user, "творог 9% 200")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal == 320.0  # tvorog_9 (160/100*200), не tvorog_5 (240)


async def test_variant_pick_sticks() -> None:
    # после вопроса юзер выбрал вариант (эмуляция callback: key + variant_locked) →
    # continue_meal логирует, НЕ переспрашивает заново (регрессия «message is not modified»)
    assistant, users, products = _build(StubNlu(_item("творог", "tvorog_5")))
    await _seed(products, _TVOROG, "tvorog")
    user = await _new_user(users)

    result = await assistant.process(user, "творог 150")
    assert isinstance(result, VariantClarificationNeeded)

    draft = result.draft
    draft.items[result.item_index].canonical_key = "tvorog_9"
    draft.items[result.item_index].variant_locked = True
    final = await assistant.continue_meal(user, draft, raw_input="творог 150")

    assert isinstance(final, MealLogged)
    assert final.meal.totals.kcal == 320.0  # выбранный tvorog_9 (160/100*200), без повтора


async def test_repeat_generic_asks_again() -> None:
    # памяти нет: «творог» спрашивает КАЖДЫЙ раз, даже после прошлого выбора
    # (два свежих парса — StubNlu отдаёт один и тот же объект, а первый мы мутируем)
    assistant, users, products = _build(
        StubNlu(_item("творог", "tvorog_5"), _item("творог", "tvorog_5"))
    )
    await _seed(products, _TVOROG, "tvorog")
    user = await _new_user(users)

    first = await assistant.process(user, "творог 150")
    # эмулируем выбор tvorog_9 и запись
    first.draft.items[first.item_index].canonical_key = "tvorog_9"
    first.draft.items[first.item_index].variant_locked = True
    await assistant.continue_meal(user, first.draft, raw_input="творог 150")
    # снова «творог» → снова вопрос (память не ведём)
    second = await assistant.process(user, "творог 150")

    assert isinstance(first, VariantClarificationNeeded)
    assert isinstance(second, VariantClarificationNeeded)


async def test_no_quantity_asks_size_and_scales() -> None:
    # «овсянка» без количества → unit=portion/null/типичная → вопрос размера → масштаб (FR-21/D-7)
    oat = ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="овсянка",
                canonical_key="oatmeal_x",  # вне семейства — только вопрос размера, без варианта
                unit=Unit.PORTION,
                portion_hint=None,
                grams=250,  # типичная порция от LLM
                kcal_per_100g=90,
                protein_per_100g=3,
                fat_per_100g=2,
                carb_per_100g=15,
                confidence=0.9,
            )
        ],
    )
    assistant, users, _ = _build(StubNlu(oat))
    user = await _new_user(users)

    result = await assistant.process(user, "овсянка")
    assert isinstance(result, ClarificationNeeded)  # спросили размер, не угадали

    result.draft.items[0].portion_hint = "small"
    final = await assistant.continue_meal(user, result.draft, raw_input="овсянка")

    assert isinstance(final, MealLogged)
    assert final.meal.items[0].grams == 175  # типичная 250 × 0.7 (маленькая)
    assert final.meal.totals.kcal == 157.5  # 90/100 * 175


async def test_chicken_qualifier_picks_grilled() -> None:
    # «курица гриль» — квалификатор гриль → grilled (400), даже если LLM промахнулся ключом
    assistant, users, products = _build(StubNlu(_item("курица гриль", "chicken_breast_boiled")))
    await _seed(products, _CHICKEN, "chicken")
    user = await _new_user(users)

    result = await assistant.process(user, "курица гриль 200")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal == 400.0
