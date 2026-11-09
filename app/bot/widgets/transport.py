"""Методы Bot API 10.4 для виджетов — свои `TelegramMethod` (D-45).

Релиза aiogram с 10.4 нет (3.31.0 — последний на 10.11.2026), поэтому методы описаны здесь.
Вызов — как у любого метода aiogram: `await bot(SendWidget(...))`; сессия, ретраи и разбор
ошибок (TelegramBadRequest и т.д.) — те же. Поле `widget` в ответе aiogram не знает: модели
aiogram принимают лишние поля (`extra="allow"`), оно ляжет в `message.model_extra`.
Выйдет aiogram с 10.4 — этот модуль заменяется его методами.
"""
from __future__ import annotations

from aiogram.methods.base import TelegramMethod
from aiogram.types import InlineKeyboardMarkup, Message


class SendWidget(TelegramMethod[Message]):
    __returning__ = Message
    __api_method__ = "sendWidget"

    chat_id: int
    html: str
    chat_data: str | None = None
    caption: str | None = None
    allow_queries: bool | None = None
    disable_notification: bool | None = None
    reply_markup: InlineKeyboardMarkup | None = None


class EditMessageWidgetData(TelegramMethod[bool]):
    __returning__ = bool
    __api_method__ = "editMessageWidgetData"

    chat_id: int
    message_id: int
    chat_data: str


class AnswerWidgetQuery(TelegramMethod[bool]):
    __returning__ = bool
    __api_method__ = "answerWidgetQuery"

    widget_query_id: str
    data: str | None = None
