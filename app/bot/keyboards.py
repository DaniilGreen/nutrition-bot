"""Клавиатуры: постоянное reply-меню, inline-кнопки порций и действий с записью."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

PORTION_CALLBACK_PREFIX = "portion"
MEAL_UNDO_PREFIX = "meal_undo"

# Тексты кнопок постоянного меню (они же приходят как обычные сообщения).
BTN_TODAY = "📊 Сегодня"
BTN_WEEK = "📈 Неделя"
BTN_HISTORY = "🗒 История"
BTN_GOAL = "🎯 Цель"
BTN_UNDO = "↩️ Отменить"

MENU_BUTTONS = (BTN_TODAY, BTN_WEEK, BTN_HISTORY, BTN_GOAL, BTN_UNDO)


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    for text in MENU_BUTTONS:
        builder.button(text=text)
    builder.adjust(3, 2)
    return builder.as_markup(resize_keyboard=True, is_persistent=True)


def portion_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Маленькая", callback_data=f"{PORTION_CALLBACK_PREFIX}:small")
    builder.button(text="Средняя", callback_data=f"{PORTION_CALLBACK_PREFIX}:medium")
    builder.button(text="Большая", callback_data=f"{PORTION_CALLBACK_PREFIX}:large")
    builder.adjust(3)
    return builder.as_markup()


def meal_actions_keyboard(meal_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="↩️ Отменить", callback_data=f"{MEAL_UNDO_PREFIX}:{meal_id}")
    return builder.as_markup()
