"""«📈 Неделя» виджетом (FR-49.5, каталог #3): столбики дней, вкладки Калории / Белок, тап по дню —
его БЖУ. Только показ: данные — те же `week_view` и КБЖУ по дням, что у rich-экрана (MA-INV-1)."""
from __future__ import annotations

import logging

from aiogram import Bot

from app.bot.keyboards import open_in_app
from app.bot.ui.context import DayContext
from app.bot.ui.screens import week_view
from app.bot.ui.widgets import LIMIT, size, week_data
from app.bot.widgets import template
from app.bot.widgets.transport import SendWidget

logger = logging.getLogger(__name__)


async def show(bot: Bot, chat_id: int, ctx: DayContext) -> bool:
    """Отправить неделю виджетом. False — показывать нечего (экран «записей нет» — текстом),
    не влезло или не вышло: вызывающий шлёт прежний rich-экран (W-INV-2)."""
    try:
        v = week_view(ctx)
        if isinstance(v, str):
            return False
        today = ctx.days[-1][0]
        data = week_data(v, dict(ctx.macros7), today)
        if size(data) > LIMIT:
            return False
        await bot(SendWidget(
            chat_id=chat_id, html=template("week"), chat_data=data,
            caption="📈 Неделя",  # W-INV-2, D-60: цифры — в виджете
            reply_markup=open_in_app("tot-week"),
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("widget: неделя не ушла (%s) — фолбэк на rich", exc)
        return False
    return True
