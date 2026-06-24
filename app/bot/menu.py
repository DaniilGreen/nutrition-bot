"""Командное меню Telegram (показывается по кнопке «/»)."""
from __future__ import annotations

from aiogram import Bot
from aiogram.types import BotCommand

_COMMANDS = [
    BotCommand(command="start", description="Старт и меню"),
    BotCommand(command="today", description="Сегодня"),
    BotCommand(command="week", description="За неделю (график)"),
    BotCommand(command="streak", description="Календарь дней в цели"),
    BotCommand(command="card", description="Карточка дня (картинка)"),
    BotCommand(command="history", description="История"),
    BotCommand(command="undo", description="Отменить запись"),
    BotCommand(command="goal", description="Цель по калориям"),
    BotCommand(command="profile", description="Настроить цель по профилю"),
    BotCommand(command="products", description="Поправить КБЖУ продукта"),
    BotCommand(command="timezone", description="Часовой пояс (по геолокации)"),
    BotCommand(command="bug", description="Сообщить о баге"),
    BotCommand(command="help", description="Помощь"),
]


async def setup_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(_COMMANDS)
