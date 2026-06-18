"""FSM-состояния бота (aiogram)."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class MealStates(StatesGroup):
    awaiting_portion = State()  # уточнение размера порции
    awaiting_density = State()  # ввод исправленных КБЖУ продукта
