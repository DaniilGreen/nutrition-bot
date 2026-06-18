"""FSM-состояния бота (aiogram)."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class MealStates(StatesGroup):
    awaiting_portion = State()  # уточнение размера порции
    awaiting_variant = State()  # выбор варианта generic-имени (FR-20)
    awaiting_density = State()  # ввод исправленных КБЖУ продукта


class ProfileStates(StatesGroup):
    sex = State()       # выбор пола
    body = State()      # ввод «вес рост возраст»
    activity = State()  # выбор активности
    goal = State()      # выбор цели (похудение/поддержание/набор)
