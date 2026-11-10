"""Общая карточка-виджет (D-88): фото блюда, бланк InBody, /products.

Только показ и правка данных: кнопки карточки жмутся через `k:<callback_data>` (D-82) и попадают в
те же обработчики, что кнопки под текстовой карточкой. Данные собирает `ui.widgets.card_data`.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from app.bot.widgets import template
from app.bot.widgets.transport import EditMessageWidgetData, SendWidget

logger = logging.getLogger(__name__)


async def show(
    bot: Bot, chat_id: int, data: str | None, reply_markup: InlineKeyboardMarkup | None = None,
) -> int | None:
    """Отправить карточку. None — данные не влезли или виджет не ушёл: вызывающий шлёт текст с
    прежними кнопками (W-INV-2). Иначе — id сообщения."""
    if data is None:
        return None
    try:
        msg = await bot(SendWidget(chat_id=chat_id, html=template("card"), chat_data=data,
                                   allow_queries=True, reply_markup=reply_markup))
    except Exception as exc:  # noqa: BLE001
        logger.warning("widget: карточка не ушла (%s) — фолбэк на текст", exc)
        return None
    return msg.message_id


async def edit(bot: Bot, chat_id: int, message_id: int, data: str | None) -> bool:
    """Поправить данные карточки на месте. False — не вышло (сообщения нет, данные не влезли)."""
    if data is None:
        return False
    try:
        await bot(EditMessageWidgetData(chat_id=chat_id, message_id=message_id, chat_data=data))
    except Exception as exc:  # noqa: BLE001
        if "not modified" not in str(exc).lower():
            logger.warning("widget: карточка не правится: %s", exc)
            return False
    return True


def is_widget(message: object) -> bool:
    """Сообщение — виджет: у настоящего Telegram кладёт `widget`, у нажатия из виджета (`k:`) его
    ставит мост (`middlewares._tap`)."""
    return "widget" in (getattr(message, "model_extra", None) or {})
