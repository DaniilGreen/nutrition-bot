"""Дымовой тест хендлер-слоя: правка карточки и навигация РЕДАКТИРУЮТ сообщение, а не шлют заново.

Ревьюер верно заметил: 578 строк рефактора проверены только live-прогоном. Этот тест закрывает
самое хрупкое место — ветвление on_item_del / on_day_nav, где легко случайно вернуть delete+resend
(прыгающая карточка + вторая карточка при двойном тапе).
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TgUser

from app.application.results import MealLogged
from app.bot import handlers
from tests.e2e.test_editing import _chicken_and_rice
from tests.e2e.test_meal_flow import _build, _new_user
from tests.e2e.test_variants import _meal
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


async def test_day_del_removes_one_item_and_edits_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    assistant, user, meal = await _logged_two_item_meal()
    edit = AsyncMock()
    monkeypatch.setattr(handlers, "_rich_edit", edit)

    item_id = meal.meal.items[0].id
    await handlers.on_day_del(_callback(f"day_del:0:{meal.meal.id}:{item_id}"), user, assistant)

    edit.assert_awaited_once()  # тот же день перерисован на месте
    today = await assistant.today(user)
    remaining = [it for m in today.meals for it in m.items]
    assert len(remaining) == 1  # удалена ОДНА позиция, вторая осталась


async def test_variant_answers_callback_before_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Баг-репорт #21: «кнопки подвисают и не отрабатывают».

    Причина была в порядке: `callback.answer()` стоял ПОСЛЕ переоценки плотности через LLM.
    Пока шёл запрос, спиннер на кнопке крутился, а через ~15 с коллбэк протухал — и answer()
    падал с «query is too old», выдавая «Что-то пошло не так» уже ПОСЛЕ успешной записи.
    """
    draft = _meal("молоко", "milk", 60, clarify=["молоко 1.5%", "молоко 2.5%"])
    assistant, users, _ = _build(StubNlu(_meal("молоко 2.5%", "milk_2_5", 52)))
    user = await _new_user(users)

    cb = _callback("variant:0")
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"draft": draft, "item_index": 0, "raw": "молоко"})
    monkeypatch.setattr(handlers, "_render_after_continue", AsyncMock())

    answered_before: list[bool] = []
    slow = assistant.resolve_clarification

    async def spy(*args: object, **kw: object) -> object:
        answered_before.append(cb.bot.await_count > 0)  # успели ответить до похода в LLM?
        return await slow(*args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(assistant, "resolve_clarification", spy)
    await handlers.on_variant(cb, user, assistant, AsyncMock(), state)

    assert answered_before == [True], "спиннер гасим ДО LLM, иначе коллбэк протухнет (#21)"


# Методы, которые ходят в LLM/STT — то есть занимают секунды, а не миллисекунды.
_SLOW = {"resolve_clarification", "continue_meal", "process", "process_voice", "read_label"}


def test_every_slow_callback_answers_telegram_first() -> None:
    """Баг #21 оказался КЛАССОМ: три хендлера уходили в LLM, не ответив Telegram.

    Проверяем все разом, а не три починенных: спиннер на кнопке крутится, пока коллбэк не
    отвечен, а через ~15 с он протухает — и ответ падает с «query is too old» уже ПОСЛЕ
    успешной записи. Новый хендлер с той же болью должен ронять этот тест, а не прод.
    """
    tree = ast.parse(Path(handlers.__file__).read_text(encoding="utf-8"))
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        if not any("callback_query" in ast.dump(d) for d in fn.decorator_list):
            continue

        answers: list[int] = []
        slow: list[int] = []
        for stmt in fn.body:
            # guard-блок «if черновик потерян: ответить и выйти» — его answer() не считается
            # ответом основного потока (именно на этом первая версия теста и слепла).
            if isinstance(stmt, ast.If) and stmt.body and isinstance(stmt.body[-1], ast.Return):
                continue
            for node in ast.walk(stmt):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                recv = node.func.value
                if (
                    node.func.attr == "answer"
                    and isinstance(recv, ast.Name)
                    and recv.id == "callback"
                ):
                    answers.append(node.lineno)
                elif node.func.attr in _SLOW:
                    slow.append(node.lineno)

        if slow and (not answers or min(answers) > min(slow)):
            offenders.append(fn.name)

    assert not offenders, f"коллбэк уходит в LLM, не ответив Telegram: {offenders}"
