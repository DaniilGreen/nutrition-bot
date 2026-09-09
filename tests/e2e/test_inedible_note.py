"""INV-7.1 (багрепорт #34): вычет несъедобного виден в записи.

Человек сказал «куриный окорок 400 грамм с костью», а в ленте увидел «260 г».
Вычет верен — кость не едят, — но молчание про него читается как поломка:
«он так и нормально не понял».
"""
from __future__ import annotations

import pytest

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.results import MealLogged
from tests.e2e.test_meal_flow import _build, _new_user
from tests.fakes import StubNlu


def _item(name: str, grams: float, kcal: float = 220.0) -> ParsedMessage:
    return ParsedMessage(
        intent=Intent.MEAL, confidence=0.9,
        items=[ParsedItem(
            name=name, canonical_key="chicken_leg", unit=Unit.GRAM, grams=grams,
            kcal_per_100g=kcal, protein_per_100g=22.0, fat_per_100g=14.0,
            carb_per_100g=0.0, confidence=0.9)],
    )


async def test_вычет_кости_объясняется_в_записи() -> None:
    """Тот самый случай. Модель права в цифре, приложение обязано сказать почему."""
    assistant, users, _ = _build(StubNlu(_item("куриный окорок с кожей", 260)))
    user = await _new_user(users)
    result = await assistant.process(user, "куриный окорок 400 грамм с костью и с кожей")
    assert isinstance(result, MealLogged)
    name = result.meal.items[0].name_snapshot
    assert "съедобное из 400 г с костью" in name, name
    # Цифру не трогаем: вычет верен, чинилась видимость.
    assert result.meal.items[0].grams == pytest.approx(260.0)


async def test_без_упоминания_несъедобного_приписки_нет() -> None:
    """Обычная запись не должна обрастать пояснениями — это был бы шум."""
    assistant, users, _ = _build(StubNlu(_item("куриный окорок", 400)))
    user = await _new_user(users)
    result = await assistant.process(user, "куриный окорок 400 грамм")
    assert "·" not in result.meal.items[0].name_snapshot


async def test_вес_не_назван_сверять_нечего() -> None:
    """«Две ножки с костью» — на весах ничего не было, приписка бессмысленна."""
    assistant, users, _ = _build(StubNlu(_item("куриная ножка", 120)))
    user = await _new_user(users)
    result = await assistant.process(user, "две куриные ножки с костью")
    assert "съедобное" not in result.meal.items[0].name_snapshot


async def test_каталог_приписками_не_засоряется() -> None:
    """INV-3: пояснение про ОДНУ запись. Имя продукта в каталоге остаётся чистым,
    иначе следующая запись того же продукта унаследует чужие 400 грамм."""
    assistant, users, products = _build(StubNlu(_item("куриный окорок с кожей", 260)))
    user = await _new_user(users)
    await assistant.process(user, "куриный окорок 400 грамм с костью")
    saved = await products.get_by_key("chicken_leg")
    assert saved is not None
    assert "съедобное" not in saved.display_name, saved.display_name
