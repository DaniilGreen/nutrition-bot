"""Клавиатуры: reply-меню, inline-кнопки порций, действий, выбора отмены и правки продукта."""
from __future__ import annotations

from collections.abc import Sequence
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from app.application.entities import Meal, Product

PORTION_CALLBACK_PREFIX = "portion"
MEAL_UNDO_PREFIX = "meal_undo"
ITEM_DEL_PREFIX = "item_del"
PROD_FIX_PREFIX = "prod_fix"

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


def meal_actions_keyboard(meal: Meal) -> InlineKeyboardMarkup:
    """Под свежей записью: удалить отдельную позицию (если их >1) и отменить всё.

    Это контекстная отмена «без вопроса» — кнопки знают конкретную запись.
    """
    builder = InlineKeyboardBuilder()
    if len(meal.items) > 1:
        for item in meal.items:
            if item.id is not None:
                builder.button(
                    text=f"🗑 {_short(item.name_snapshot)}",
                    callback_data=f"{ITEM_DEL_PREFIX}:{meal.id}:{item.id}",
                )
    builder.button(text="↩️ Отменить всё", callback_data=f"{MEAL_UNDO_PREFIX}:{meal.id}")
    builder.adjust(1)
    return builder.as_markup()


def undo_picker_keyboard(meals: Sequence[Meal], tz_name: str) -> InlineKeyboardMarkup:
    """Меню «Отменить» вне контекста свежей записи — выбор, что именно отменить."""
    tz = ZoneInfo(tz_name)
    builder = InlineKeyboardBuilder()
    for meal in meals:
        when = meal.eaten_at.astimezone(tz).strftime("%H:%M")
        names = _short(", ".join(i.name_snapshot for i in meal.items) or "запись", 24)
        builder.button(
            text=f"🗑 {when} · {meal.totals.kcal:g} ккал · {names}",
            callback_data=f"{MEAL_UNDO_PREFIX}:{meal.id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def products_fix_keyboard(products: Sequence[Product]) -> InlineKeyboardMarkup:
    """Список продуктов с AI-оценкой — выбрать, какому исправить КБЖУ."""
    builder = InlineKeyboardBuilder()
    for product in products:
        if product.id is not None:
            builder.button(
                text=f"✏️ {_short(product.display_name)} ({product.density.kcal_per_100g:g} ккал)",
                callback_data=f"{PROD_FIX_PREFIX}:{product.id}",
            )
    builder.adjust(1)
    return builder.as_markup()


def _short(text: str, limit: int = 22) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
