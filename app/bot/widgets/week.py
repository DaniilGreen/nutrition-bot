"""«📈 Неделя» виджетом (FR-49.5, каталог #3): столбики дней, вкладки Калории / Белок, тап по дню —
его БЖУ, листание ‹ › по неделям (D-72). Только показ: данные — те же `week_view` / `week_balance`
и КБЖУ по дням, что у rich-экрана и «Итогов» аппки (MA-INV-1)."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import replace

from aiogram import Bot

from app.application.assistant import Assistant
from app.application.entities import User
from app.bot.keyboards import open_in_app
from app.bot.ui import gather
from app.bot.ui.context import DayContext
from app.bot.ui.screens import _week_calendar, week_view
from app.bot.ui.widgets import LIMIT, size, week_data
from app.bot.widgets import live, template
from app.bot.widgets.transport import SendWidget

logger = logging.getLogger(__name__)
MAX_WEEKS = 52
_NAV = re.compile(r"k:([0-9]{1,2})")


async def data_for(ctx: DayContext, assistant: Assistant, user: User, scans: object | None,
                   weeks_ago: int) -> str | None:
    """`chat_data` недели `weeks_ago` назад. None — показывать нечего (записей нет) или не влезло.

    Прошлая неделя — тот же `week_balance`, что листают «Итоги» аппки, и тот же `_week_calendar`,
    что рисует текущую: подставлены баланс той недели и её КБЖУ по дням."""
    today = ctx.days[-1][0]
    if weeks_ago == 0:
        v, macros = week_view(ctx), dict(ctx.macros7)
    else:
        bmr = await scans.bmr(user) if scans is not None else None  # type: ignore[attr-defined]
        w = await assistant.week_balance(user, weeks_ago, bmr_kcal=bmr)
        if w.verdict == "empty":
            return None
        rows, _ = await assistant.daily_series(user, (today - w.start).days + 1)
        macros = {d: m for d, m, _ in rows if w.start <= d <= w.end}
        v = _week_calendar(replace(ctx, week_bal=w, macros7=sorted(macros.items())), w)
        if w.verdict == "few_logs":  # баланса нет — герой не должен его выдумывать
            v = replace(v, hero="записей мало", over=False)
    if isinstance(v, str):
        return None
    data = week_data(v, macros, today, weeks_ago)
    return data if size(data) <= LIMIT else None


async def show(bot: Bot, chat_id: int, ctx: DayContext) -> bool:
    """Отправить неделю виджетом. False — показывать нечего (экран «записей нет» — текстом),
    не влезло или не вышло: вызывающий шлёт прежний rich-экран (W-INV-2)."""
    try:
        v = week_view(ctx)
        if isinstance(v, str):
            return False
        data = week_data(v, dict(ctx.macros7), ctx.days[-1][0])
        if size(data) > LIMIT:
            return False
        await bot(SendWidget(
            chat_id=chat_id, html=template("week"), chat_data=data,
            allow_queries=True, reply_markup=open_in_app("tot-week"),
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("widget: неделя не ушла (%s) — фолбэк на rich", exc)
        return False
    return True


async def on_week(uid: int, mid: int, data: str, user: User, assistant: Assistant) -> live.Outcome:
    """`k:<недель назад>` — листание (только чтение)."""
    m = _NAV.fullmatch(data)
    if m is None or int(m.group(1)) > MAX_WEEKS:
        return live.Outcome({"err": "bad"})
    scans = (live.EXTRA.get() or {}).get("scans")
    ctx = await gather(assistant, user, scans)
    text = await data_for(ctx, assistant, user, scans, int(m.group(1)))
    return live.Outcome(json.loads(text) if text else {"err": "empty"})


live.ROUTES["k"] = on_week
