"""Хендлеры aiogram. Тонкий слой: парсинг команды -> Assistant -> форматтер."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from app.application.assistant import Assistant
from app.application.entities import User
from app.application.ports import UserRepo
from app.bot.formatters import format_result
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
    # Дефолтная разбивка 30/25/45 (Б/Ж/У). Тонкая настройка макросов — в M2.
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
async def on_text(message: Message, app_user: User, assistant: Assistant) -> None:
    result = await assistant.process(app_user, message.text or "", input_type="text")
    await message.answer(format_result(result))
