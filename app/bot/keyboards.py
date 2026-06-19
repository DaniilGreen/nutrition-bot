"""Клавиатуры: reply-меню + inline для порций, действий, отмены, правки продукта, профиля."""
from __future__ import annotations

from collections.abc import Sequence
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from app.application.entities import Meal, Product

PORTION_CALLBACK_PREFIX = "portion"
VARIANT_CALLBACK_PREFIX = "variant"
MEAL_UNDO_PREFIX = "meal_undo"
UNDO_ITEM_PREFIX = "undo_item"
ITEM_DEL_PREFIX = "item_del"
PROD_FIX_PREFIX = "prod_fix"

# Тексты кнопок постоянного меню (они же приходят как обычные сообщения).
BTN_TODAY = "📊 Сегодня"
BTN_WEEK = "📈 Неделя"
BTN_HISTORY = "🗒 История"
BTN_WEIGHT = "⚖️ Вес"
BTN_GOAL = "🎯 Цель"
BTN_PROFILE = "⚙️ Профиль"
BTN_UNDO = "↩️ Отменить"

MENU_BUTTONS = (BTN_TODAY, BTN_WEEK, BTN_HISTORY, BTN_WEIGHT, BTN_GOAL, BTN_PROFILE, BTN_UNDO)


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    for text in MENU_BUTTONS:
        builder.button(text=text)
    builder.adjust(3, 3, 1)
    return builder.as_markup(resize_keyboard=True, is_persistent=True)


def portion_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Маленькая", callback_data=f"{PORTION_CALLBACK_PREFIX}:small")
    builder.button(text="Средняя", callback_data=f"{PORTION_CALLBACK_PREFIX}:medium")
    builder.button(text="Большая", callback_data=f"{PORTION_CALLBACK_PREFIX}:large")
    builder.adjust(3)
    return builder.as_markup()


def variant_keyboard(options: Sequence[Product]) -> InlineKeyboardMarkup:
    """FR-20: выбор варианта неоднозначного generic-имени (кнопка на продукт семейства)."""
    builder = InlineKeyboardBuilder()
    for product in options:
        if product.id is not None:
            builder.button(
                text=f"{_short(product.display_name)} ({product.density.kcal_per_100g:g})",
                callback_data=f"{VARIANT_CALLBACK_PREFIX}:{product.id}",
            )
    builder.adjust(1)
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
    """Меню «Отменить» вне контекста свежей записи — по ОТДЕЛЬНЫМ позициям (не приём целиком)."""
    tz = ZoneInfo(tz_name)
    builder = InlineKeyboardBuilder()
    for meal in meals:
        when = meal.eaten_at.astimezone(tz).strftime("%H:%M")
        for item in meal.items:
            if item.id is not None:
                builder.button(
                    text=f"🗑 {when} · {_short(item.name_snapshot, 22)} · {item.macros.kcal:g}",
                    callback_data=f"{UNDO_ITEM_PREFIX}:{meal.id}:{item.id}",
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


def sex_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="♂️ Мужской", callback_data="prof_sex:male")
    builder.button(text="♀️ Женский", callback_data="prof_sex:female")
    builder.adjust(2)
    return builder.as_markup()


def activity_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🛋 Низкая", callback_data="prof_act:low")
    builder.button(text="🚶 Средняя", callback_data="prof_act:medium")
    builder.button(text="🏃 Высокая", callback_data="prof_act:high")
    builder.adjust(3)
    return builder.as_markup()


def goal_type_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔻 Похудение", callback_data="prof_goal:cut")
    builder.button(text="⚖️ Поддержание", callback_data="prof_goal:maintain")
    builder.button(text="🔺 Набор", callback_data="prof_goal:bulk")
    builder.adjust(1)
    return builder.as_markup()


def _short(text: str, limit: int = 22) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
