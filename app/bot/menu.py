"""Командное меню Telegram (показывается по кнопке «/»)."""
from __future__ import annotations

from aiogram import Bot
from aiogram.types import BotCommand

_COMMANDS = [
    BotCommand(command="start", description="Старт и меню"),
    BotCommand(command="today", description="Сегодня"),
    BotCommand(command="week", description="За неделю"),
    BotCommand(command="history", description="История"),
    BotCommand(command="undo", description="Отменить запись"),
    BotCommand(command="goal", description="Цель по калориям"),
    BotCommand(command="products", description="Поправить КБЖУ продукта"),
    BotCommand(command="help", description="Помощь"),
]


async def setup_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(_COMMANDS)
