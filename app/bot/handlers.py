"""Хендлеры aiogram. Тонкий слой: парсинг -> Assistant -> форматтер.

M2: гибридные уточнения порции через inline-кнопки + FSM (черновик в памяти).
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.application.assistant import Assistant
from app.application.entities import User
from app.application.ports import UserRepo
from app.application.results import ClarificationNeeded, Result
from app.bot.formatters import format_result
from app.bot.keyboards import portion_keyboard
from app.bot.states import MealStates
from app.domain.goals import Goal

router = Router()

START_TEXT = (
    "👋 Привет! Я считаю КБЖУ по твоим сообщениям.\n\n"
    "Просто напиши, что съел:\n"
    "• «курица 250 грамм»\n"
    "• «два банана»\n"
    "• «фо бо средняя порция»\n\n"
    "Вес тела: «вес 86.8».\n"
    "Статистика: «сколько осталось».\n\n"
    "Цель по калориям: /goal 1800"
)


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(START_TEXT)


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    await message.answer(START_TEXT)


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
    # Дефолтная разбивка 30/25/45 (Б/Ж/У). Тонкая настройка макросов — позже.
    goal = Goal(
        daily_kcal=kcal,
        protein_g=round(kcal * 0.30 / 4),
        fat_g=round(kcal * 0.25 / 9),
        carb_g=round(kcal * 0.45 / 4),
    )
    await users.set_goal(app_user.id, goal)
    await message.answer(f"🎯 Цель обновлена: {kcal:g} ккал/день.")


@router.message(F.voice)
async def on_voice(message: Message) -> None:
    await message.answer(
        "🎤 Голосовые будут в M3. Пока напиши текстом — например «фо бо средняя порция»."
    )


@router.message(F.text)
async def on_text(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    # Новый текст сбрасывает незавершённое уточнение.
    await state.clear()
    result = await assistant.process(app_user, message.text or "", input_type="text")
    await _reply_message(message, result, state, raw=message.text or "")


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
        await callback.message.edit_text(format_result(result))
    await callback.answer()


async def _reply_message(
    message: Message, result: Result, state: FSMContext, *, raw: str
) -> None:
    if isinstance(result, ClarificationNeeded):
        await state.set_state(MealStates.awaiting_portion)
        await state.update_data(draft=result.draft, item_index=result.item_index, raw=raw)
        await message.answer(format_result(result), reply_markup=portion_keyboard())
    else:
        await message.answer(format_result(result))
