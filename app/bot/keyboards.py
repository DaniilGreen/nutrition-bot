"""Inline-клавиатуры."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

PORTION_CALLBACK_PREFIX = "portion"


def portion_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Маленькая", callback_data=f"{PORTION_CALLBACK_PREFIX}:small")
    builder.button(text="Средняя", callback_data=f"{PORTION_CALLBACK_PREFIX}:medium")
    builder.button(text="Большая", callback_data=f"{PORTION_CALLBACK_PREFIX}:large")
    builder.adjust(3)
    return builder.as_markup()
