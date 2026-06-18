"""FSM-состояния бота (aiogram). Для MVP — единственное состояние уточнения порции."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class MealStates(StatesGroup):
    awaiting_portion = State()
