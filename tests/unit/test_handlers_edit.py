"""Дымовой тест хендлер-слоя: правка карточки и навигация РЕДАКТИРУЮТ сообщение, а не шлют заново.

Ревьюер верно заметил: 578 строк рефактора проверены только live-прогоном. Этот тест закрывает
самое хрупкое место — ветвление on_item_del / on_day_nav, где легко случайно вернуть delete+resend
(прыгающая карточка + вторая карточка при двойном тапе).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TgUser

from app.application.results import MealLogged
from app.bot import handlers
from tests.e2e.test_editing import _chicken_and_rice
from tests.e2e.test_meal_flow import _build, _new_user
from tests.fakes import StubNlu


def _callback(data: str) -> CallbackQuery:
    """Callback с реальным Message (для isinstance) и мок-ботом (ловит .answer)."""
    msg = Message.model_construct(
        message_id=10, date=None, chat=Chat.model_construct(id=1, type="private")
    )
    cb = CallbackQuery.model_construct(
        id="1",
        from_user=TgUser.model_construct(id=1, is_bot=False, first_name="T"),
        chat_instance="ci",
        data=data,
        message=msg,
    )
    return cb.as_(AsyncMock())


async def _logged_two_item_meal() -> tuple[object, object, MealLogged]:
    assistant, users, _ = _build(StubNlu(_chicken_and_rice()))
    user = await _new_user(users)
    result = await assistant.process(user, "курица и рис")
    assert isinstance(result, MealLogged)
    return assistant, user, result


async def test_item_del_edits_in_place_not_resend(monkeypatch: pytest.MonkeyPatch) -> None:
    assistant, user, meal = await _logged_two_item_meal()
    edit, send, card = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(handlers, "_rich_edit", edit)
    monkeypatch.setattr(handlers, "_rich", send)
    monkeypatch.setattr(handlers, "_send_meal_card", card)

    item_id = meal.meal.items[0].id
    await handlers.on_item_del(_callback(f"item_del:{meal.meal.id}:{item_id}"), user, assistant)

    edit.assert_awaited_once()  # правим НА МЕСТЕ
    send.assert_not_awaited()  # не шлём новую карточку (иначе прыжок + дубль при двойном тапе)
    card.assert_not_awaited()


async def test_day_nav_edits_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    assistant, user, _ = await _logged_two_item_meal()
    edit, send = AsyncMock(), AsyncMock()
    monkeypatch.setattr(handlers, "_rich_edit", edit)
    monkeypatch.setattr(handlers, "_rich", send)

    await handlers.on_day_nav(_callback("day_nav:1"), user, assistant)

    edit.assert_awaited_once()
    send.assert_not_awaited()


async def test_day_del_removes_meal_and_edits_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    assistant, user, meal = await _logged_two_item_meal()
    edit = AsyncMock()
    monkeypatch.setattr(handlers, "_rich_edit", edit)

    await handlers.on_day_del(_callback(f"day_del:0:{meal.meal.id}"), user, assistant)

    edit.assert_awaited_once()  # тот же день перерисован на месте
    today = await assistant.today(user)
    assert today.meals_count == 0  # приём действительно удалён
