"""Командное меню Telegram (кнопка «/») и кнопка запуска мини-аппа."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo

from app.assets import asset_version

log = logging.getLogger(__name__)

_COMMANDS = [
    BotCommand(command="start", description="Старт и меню"),
    BotCommand(command="today", description="Сегодня"),
    BotCommand(command="week", description="Среднее за неделю"),
    BotCommand(command="month", description="Отчёт за 30 дней"),
    BotCommand(command="streak", description="Календарь дней в цели"),
    BotCommand(command="card", description="Карточка дня"),
    BotCommand(command="history", description="История"),
    BotCommand(command="ask", description="Спросить ассистента про свои данные"),
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


async def setup_miniapp_button(bot: Bot, url: str) -> None:
    """Кнопка запуска аппки с отпечатком статики в адресе.

    Одного `Cache-Control: no-store` на странице мало: если клиент уже держит
    точку входа в кэше, он за ней просто не сходит — 05.09.2026 так и вышло,
    новый фронт лежал на сервере, а на телефоне остался старый. Адрес с новым
    отпечатком совпасть с закэшированным не может, поэтому каждая выкатка
    гарантированно открывается свежей.

    Сбой не роняет бота: не выставилась кнопка — это неудобство, а не потеря
    записей, и ради него незачем оставлять людей без учёта КБЖУ.
    """
    if not url:
        return
    target = f"{url.rstrip('/')}/?v={asset_version()}"
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Mini-App", web_app=WebAppInfo(url=target))
        )
    except Exception:
        log.warning("не удалось выставить кнопку мини-аппа (%s)", target, exc_info=True)
