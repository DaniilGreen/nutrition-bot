"""E2E D-19: LLM-driven уточнения — clarify_options → кнопки/текст → переоценка плотности."""
from __future__ import annotations

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.results import ClarificationNeeded, MealLogged, VariantClarificationNeeded
from tests.e2e.test_meal_flow import _build, _new_user
from tests.fakes import StubNlu


def _meal(
    name: str,
    key: str,
    kcal: float,
    *,
    clarify: list[str] | None = None,
    unit: Unit = Unit.GRAM,
    grams: float | None = 200,
    portion_hint: str | None = None,
) -> ParsedMessage:
    return ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name=name,
                canonical_key=key,
                unit=unit,
                grams=grams,
                portion_hint=portion_hint,
                kcal_per_100g=kcal,
                protein_per_100g=0,
                fat_per_100g=0,
                carb_per_100g=round(kcal / 4, 1),  # Атвотер-консистентно (vet не переписывает ккал)
                clarify_options=clarify,
                confidence=0.9,
            )
        ],
    )


async def test_clarify_asked_when_options_present() -> None:
    # «шаверма» с clarify_options от LLM → вопрос ровно с этими вариантами (строки)
    opts = ["шаверма с курицей", "шаверма с говядиной", "вег"]
    stub = StubNlu(_meal("шаверма", "shawarma", 210, clarify=opts))
    assistant, users, _ = _build(stub)
    user = await _new_user(users)

    result = await assistant.process(user, "шаверма 200")

    assert isinstance(result, VariantClarificationNeeded)
    assert result.options == opts


async def test_clarify_button_replaces_name_and_reestimates() -> None:
    # тап «шаверма с говядиной» → имя заменяется целиком, плотность ПЕРЕоценивается (230)
    stub = StubNlu(
        _meal("шаверма", "shawarma", 210, clarify=["шаверма с курицей", "шаверма с говядиной"]),
        _meal("шаверма с говядиной", "shawarma_beef", 230),  # переоценка под уточнённое имя
    )
    assistant, users, _ = _build(stub)
    user = await _new_user(users)

    asked = await assistant.process(user, "шаверма 200")
    assert isinstance(asked, VariantClarificationNeeded)

    final = await assistant.resolve_clarification(
        user, asked.draft, asked.item_index, asked.options[1], raw_input="шаверма 200"
    )
    assert isinstance(final, MealLogged)
    assert final.meal.items[0].name_snapshot == "шаверма с говядиной"
    assert final.meal.totals.kcal == 460.0  # 230/100 * 200, не generic 210


async def test_clarify_freetext_appends_and_reestimates() -> None:
    # свой вариант текстом «с бараниной» → имя = «шаверма с бараниной», переоценка (250)
    stub = StubNlu(
        _meal("шаверма", "shawarma", 210, clarify=["шаверма с курицей"]),
        _meal("шаверма с бараниной", "shawarma_lamb", 250),
    )
    assistant, users, _ = _build(stub)
    user = await _new_user(users)

    asked = await assistant.process(user, "шаверма 200")
    new_name = f"{asked.draft.items[asked.item_index].name} с бараниной"
    final = await assistant.resolve_clarification(
        user, asked.draft, asked.item_index, new_name, raw_input="шаверма 200"
    )

    assert isinstance(final, MealLogged)
    assert final.meal.items[0].name_snapshot == "шаверма с бараниной"
    assert final.meal.totals.kcal == 500.0  # 250/100 * 200


async def test_no_clarify_logs_directly() -> None:
    # нет clarify_options → не дёргаем вопросом, пишем сразу
    stub = StubNlu(_meal("огурец", "cucumber", 15))
    assistant, users, _ = _build(stub)
    user = await _new_user(users)

    result = await assistant.process(user, "огурец 200")
    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal == 30.0  # 15/100 * 200


async def test_clarify_then_portion_two_questions() -> None:
    # ≤2 вопроса: сперва СОСТАВ (clarify), потом РАЗМЕР (portion). Состав влияет и на то, и на то.
    stub = StubNlu(
        _meal("рыба", "fish", 100, clarify=["треска", "лосось"], unit=Unit.PORTION, grams=200),
        _meal("лосось", "salmon", 200),  # переоценка плотности; unit/размер берём из исходной
    )
    assistant, users, _ = _build(stub)
    user = await _new_user(users)

    asked = await assistant.process(user, "рыба")
    assert isinstance(asked, VariantClarificationNeeded)  # сперва — что за рыба

    after = await assistant.resolve_clarification(
        user, asked.draft, asked.item_index, "лосось", raw_input="рыба"
    )
    assert isinstance(after, ClarificationNeeded)  # затем — размер порции


async def test_no_quantity_asks_size_and_scales() -> None:
    # «овсянка» без количества → unit=portion/null/типичная → вопрос размера → масштаб (FR-21/D-7)
    oat = ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="овсянка",
                canonical_key="oatmeal_x",
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
