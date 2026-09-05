"""Багрепорт #31: новая этикетка отменяет вопрос про вес предыдущей.

Человек прислал второе фото, не ответив про вес первого, и бот раз за разом
требовал ответить про первое. Тупик: выйти можно было только меню, а из чата это
выглядит как «бот заклинило».
"""
from __future__ import annotations

from unittest.mock import AsyncMock

from app.bot.handlers import _busy_with_flow
from app.bot.states import MealStates

LABEL = MealStates.awaiting_label_grams.state


class _State:
    """Минимальный FSMContext: только то, что читает гард."""

    def __init__(self, current: str | None) -> None:
        self.current = current
        self.cleared = False

    async def get_state(self) -> str | None:
        return self.current

    async def clear(self) -> None:
        self.cleared = True
        self.current = None


async def test_свободное_состояние_пропускает() -> None:
    msg = AsyncMock()
    assert await _busy_with_flow(msg, _State(None)) is False
    msg.answer.assert_not_awaited()


async def test_новая_этикетка_отменяет_вопрос_о_весе() -> None:
    msg, state = AsyncMock(), _State(LABEL)
    assert await _busy_with_flow(msg, state, supersedes=LABEL) is False
    assert state.cleared is True
    # Молча: человек прислал фото, а не спросил, что происходит.
    msg.answer.assert_not_awaited()


async def test_чужой_шаг_этикеткой_не_сносится() -> None:
    """Отменяем ровно тот шаг, который новое сообщение заменяет. Визард профиля
    или ввод плотности этикетка не заменяет — там черновик потерять нельзя."""
    msg, state = AsyncMock(), _State(MealStates.awaiting_density.state)
    assert await _busy_with_flow(msg, state, supersedes=LABEL) is True
    assert state.cleared is False
    msg.answer.assert_awaited_once()


async def test_без_supersedes_гард_прежний() -> None:
    """Голос ничего не заменяет: у него нет своего незавершённого шага."""
    msg, state = AsyncMock(), _State(LABEL)
    assert await _busy_with_flow(msg, state) is True
    assert state.cleared is False
