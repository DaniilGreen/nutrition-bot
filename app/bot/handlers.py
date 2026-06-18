"""Хендлеры aiogram. Тонкий слой: ввод -> Assistant -> форматтер.

UX: постоянное reply-меню, командное меню, «печатает…».
Отмена: контекстная (inline на свежей записи) — без вопроса; меню «Отменить» — выбор.
Каталог: /products → правка КБЖУ продукта, который AI оценил неверно.
Надёжность: глобальный error-handler ловит падения (в т.ч. AI) и отвечает юзеру.
"""
from __future__ import annotations

import logging
from contextlib import suppress
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message

from app.application.assistant import Assistant
from app.application.entities import User
from app.application.ports import UserRepo
from app.application.results import ClarificationNeeded, MealLogged, Result
from app.bot.formatters import format_result
from app.bot.keyboards import (
    BTN_GOAL,
    BTN_HISTORY,
    BTN_TODAY,
    BTN_UNDO,
    BTN_WEEK,
    main_menu_keyboard,
    meal_actions_keyboard,
    portion_keyboard,
    products_fix_keyboard,
    undo_picker_keyboard,
)
from app.bot.states import MealStates
from app.domain.goals import Goal
from app.domain.value_objects import Density

router = Router()
logger = logging.getLogger(__name__)

START_TEXT = (
    "👋 Привет! Я считаю КБЖУ по твоим сообщениям.\n\n"
    "Просто напиши или наговори, что съел:\n"
    "• «курица 250 грамм»\n"
    "• «два банана»\n"
    "• «фо бо средняя порция»\n\n"
    "Вес тела: «вес 86.8».\n"
    "Кнопки меню — снизу 👇"
)


# --- надёжность: единый перехват ошибок ---------------------------------------


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    logger.exception("Unhandled error in handler", exc_info=event.exception)
    target = event.update.message
    if target is None and event.update.callback_query is not None:
        maybe = event.update.callback_query.message
        target = maybe if isinstance(maybe, Message) else None
    if target is not None:
        with suppress(Exception):
            await target.answer("⚠️ Что-то пошло не так. Попробуй ещё раз через пару секунд.")
    return True


# --- команды ------------------------------------------------------------------


@router.message(CommandStart())
async def on_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(START_TEXT, reply_markup=main_menu_keyboard())


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    await message.answer(START_TEXT, reply_markup=main_menu_keyboard())


@router.message(Command("goal"))
async def on_goal(
    message: Message, command: CommandObject, app_user: User, users: UserRepo
) -> None:
    arg = (command.args or "").strip().replace(",", ".")
    try:
        kcal = float(arg)
    except ValueError:
        await message.answer("Формат: <code>/goal 1800</code> — дневная цель по калориям.")
        return

    assert app_user.id is not None
    goal = Goal(
        daily_kcal=kcal,
        protein_g=round(kcal * 0.30 / 4),
        fat_g=round(kcal * 0.25 / 9),
        carb_g=round(kcal * 0.45 / 4),
    )
    await users.set_goal(app_user.id, goal)
    await message.answer(f"🎯 Цель обновлена: {kcal:g} ккал/день.")


@router.message(Command("today"))
async def cmd_today(message: Message, app_user: User, assistant: Assistant) -> None:
    await _show_today(message, app_user, assistant)


@router.message(Command("week"))
async def cmd_week(message: Message, app_user: User, assistant: Assistant) -> None:
    await _show_week(message, app_user, assistant)


@router.message(Command("history"))
async def cmd_history(message: Message, app_user: User, assistant: Assistant) -> None:
    await _show_history(message, app_user, assistant)


@router.message(Command("undo"))
async def cmd_undo(message: Message, app_user: User, assistant: Assistant) -> None:
    await _show_undo_picker(message, app_user, assistant)


@router.message(Command("products"))
async def cmd_products(message: Message, app_user: User, assistant: Assistant) -> None:
    products = await assistant.fixable_products(limit=10)
    if not products:
        await message.answer("Пока нечего править — нет продуктов с AI-оценкой.")
        return
    await message.answer(
        "Продукты с AI-оценкой КБЖУ. Что поправить?",
        reply_markup=products_fix_keyboard(products),
    )


# --- кнопки постоянного меню (приходят как обычный текст) ----------------------


@router.message(F.text == BTN_TODAY)
async def btn_today(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    await state.clear()
    await _show_today(message, app_user, assistant)


@router.message(F.text == BTN_WEEK)
async def btn_week(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    await state.clear()
    await _show_week(message, app_user, assistant)


@router.message(F.text == BTN_HISTORY)
async def btn_history(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    await state.clear()
    await _show_history(message, app_user, assistant)


@router.message(F.text == BTN_GOAL)
async def btn_goal(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    await state.clear()
    goal = await assistant.current_goal(app_user)
    await message.answer(
        f"🎯 Текущая цель: <b>{goal.daily_kcal:g} ккал/день</b>\n"
        f"Б{goal.protein_g:g} Ж{goal.fat_g:g} У{goal.carb_g:g}\n\n"
        f"Изменить: <code>/goal 1800</code>"
    )


@router.message(F.text == BTN_UNDO)
async def btn_undo(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    await state.clear()
    await _show_undo_picker(message, app_user, assistant)


# --- ввод исправленных КБЖУ продукта (FSM) ------------------------------------


@router.message(MealStates.awaiting_density, F.text)
async def on_density_input(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    nums = _parse_floats(message.text or "")
    if len(nums) != 4:
        await message.answer(
            "Нужно 4 числа через пробел: ккал белки жиры углеводы. "
            "Напр.: <code>165 31 3.6 0</code>\nИли нажми кнопку меню, чтобы выйти."
        )
        return
    data = await state.get_data()
    product_id = int(data.get("product_id", 0))
    await state.clear()
    product = await assistant.fix_product_density(
        product_id, Density(nums[0], nums[1], nums[2], nums[3])
    )
    if product is None:
        await message.answer("Продукт не найден.")
        return
    await message.answer(
        f"✅ Обновил «{escape(product.display_name, quote=False)}»: "
        f"{nums[0]:g} ккал · Б{nums[1]:g} Ж{nums[2]:g} У{nums[3]:g} на 100 г.\n"
        f"Новые записи будут считаться по этим цифрам."
    )


# --- голос и свободный текст ---------------------------------------------------


@router.message(F.voice)
async def on_voice(
    message: Message, bot: Bot, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    await state.clear()
    if message.voice is None:
        return

    await _send_typing(message)
    buffer = await bot.download(message.voice)
    audio = buffer.read() if buffer is not None else b""
    transcript = await assistant.transcribe(audio, filename="voice.ogg")

    if not transcript:
        await message.answer(
            "🎤 Не смог распознать голос. Для голосовых нужен OpenAI-ключ — либо напиши текстом."
        )
        return

    await message.answer(f"🎤 Распознал: «{escape(transcript, quote=False)}»")
    await _send_typing(message)
    result = await assistant.process(app_user, transcript, input_type="voice")
    await _reply_message(message, result, state, raw=transcript)


@router.message(F.text)
async def on_text(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    await state.clear()
    await _send_typing(message)
    result = await assistant.process(app_user, message.text or "", input_type="text")
    await _reply_message(message, result, state, raw=message.text or "")


# --- callbacks ----------------------------------------------------------------


@router.callback_query(F.data.startswith("portion:"))
async def on_portion(
    callback: CallbackQuery, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    data = await state.get_data()
    draft = data.get("draft")
    if draft is None or not isinstance(callback.message, Message):
        await callback.answer("Запись устарела — отправь приём пищи заново.")
        return

    item_index = int(data.get("item_index", 0))
    raw = str(data.get("raw", ""))
    hint = (callback.data or "portion:medium").split(":", 1)[1]
    draft.items[item_index].portion_hint = hint

    result = await assistant.continue_meal(app_user, draft, raw_input=raw)
    if isinstance(result, ClarificationNeeded):
        await state.update_data(draft=result.draft, item_index=result.item_index)
        await callback.message.edit_text(format_result(result), reply_markup=portion_keyboard())
    else:
        await state.clear()
        markup = meal_actions_keyboard(result.meal) if isinstance(result, MealLogged) else None
        await callback.message.edit_text(format_result(result), reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("meal_undo:"))
async def on_meal_undo(callback: CallbackQuery, app_user: User, assistant: Assistant) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    meal_id = int((callback.data or "meal_undo:0").split(":", 1)[1])
    result = await assistant.delete_meal(app_user, meal_id)
    if result.meal is None:
        await callback.answer("Уже удалено.")
        with suppress(Exception):
            await callback.message.edit_text("🗑 Запись уже удалена.")
        return
    await callback.message.edit_text("🗑 Запись отменена.")
    await callback.answer()


@router.callback_query(F.data.startswith("item_del:"))
async def on_item_del(callback: CallbackQuery, app_user: User, assistant: Assistant) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    parts = (callback.data or "item_del:0:0").split(":")
    meal_id, item_id = int(parts[1]), int(parts[2])
    result = await assistant.remove_item(app_user, meal_id, item_id)
    if result is None:
        await callback.message.edit_text("🗑 Запись отменена (позиций не осталось).")
    else:
        await callback.message.edit_text(
            format_result(result), reply_markup=meal_actions_keyboard(result.meal)
        )
    await callback.answer()


@router.callback_query(F.data.startswith("prod_fix:"))
async def on_prod_fix(
    callback: CallbackQuery, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    product_id = int((callback.data or "prod_fix:0").split(":", 1)[1])
    product = await assistant.get_product(product_id)
    if product is None:
        await callback.answer("Продукт не найден.")
        return
    await state.set_state(MealStates.awaiting_density)
    await state.update_data(product_id=product_id)
    d = product.density
    await callback.message.answer(
        f"✏️ <b>{escape(product.display_name, quote=False)}</b>\n"
        f"Сейчас на 100 г: {d.kcal_per_100g:g} ккал · "
        f"Б{d.protein_per_100g:g} Ж{d.fat_per_100g:g} У{d.carb_per_100g:g}\n\n"
        f"Пришли 4 числа через пробел — ккал, белки, жиры, углеводы на 100 г.\n"
        f"Напр.: <code>165 31 3.6 0</code>"
    )
    await callback.answer()


# --- helpers ------------------------------------------------------------------


async def _show_today(message: Message, app_user: User, assistant: Assistant) -> None:
    await message.answer(format_result(await assistant.today(app_user)))


async def _show_week(message: Message, app_user: User, assistant: Assistant) -> None:
    await message.answer(format_result(await assistant.weekly(app_user)))


async def _show_history(message: Message, app_user: User, assistant: Assistant) -> None:
    await message.answer(format_result(await assistant.history(app_user)))


async def _show_undo_picker(message: Message, app_user: User, assistant: Assistant) -> None:
    history = await assistant.history(app_user, limit=5)
    if not history.meals:
        await message.answer("Нечего отменять.")
        return
    await message.answer(
        "Что отменить?",
        reply_markup=undo_picker_keyboard(history.meals, history.timezone),
    )


async def _send_typing(message: Message) -> None:
    with suppress(Exception):  # индикатор «печатает…» — не критично, если не отправится
        await message.bot.send_chat_action(message.chat.id, "typing")


def _parse_floats(text: str) -> list[float]:
    out: list[float] = []
    for token in text.replace(",", ".").split():
        try:
            out.append(float(token))
        except ValueError:
            return []
    return out


async def _reply_message(
    message: Message, result: Result, state: FSMContext, *, raw: str
) -> None:
    if isinstance(result, ClarificationNeeded):
        await state.set_state(MealStates.awaiting_portion)
        await state.update_data(draft=result.draft, item_index=result.item_index, raw=raw)
        await message.answer(format_result(result), reply_markup=portion_keyboard())
    elif isinstance(result, MealLogged):
        await message.answer(format_result(result), reply_markup=meal_actions_keyboard(result.meal))
    else:
        await message.answer(format_result(result))
