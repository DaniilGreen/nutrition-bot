"""Вечерний итог виджетом с «✅ Всё записал» (FR-49.6, каталог #15, D-57).

Итог — те же цифры и слова, что у rich-итога FR-18 (`screens.evening`); кнопка закрывает день:
`e:c:<YYYY-MM-DD>:<ключ>` → `close_day` (таблица `closed_days`) — сигнал полноты дня (D-57).
Итог шлётся только в день с записями, а такой день в серии уже засчитан, — поэтому серию кнопка
сейчас не меняет (D-58, OQ-W8). Повтор безвреден: закрытый день закрыт.

Граница доверия (W-INV-4): дата строго по формату, не в будущем и не старше недели — итог шлётся
в 23:59, а тапнуть его могут и утром.

ponytail: какие данные ушли в сообщение — в памяти процесса (одно сообщение в день на человека),
чтобы после закрытия поправить его на «закрыт». После рестарта правка сообщения пропускается:
виджет сам рисует «закрыт» из ответа, а повторный тап по кнопке после переоткрытия безвреден.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from app.application.assistant import Assistant
from app.application.entities import User
from app.bot.ui.context import DayContext
from app.bot.ui.widgets import LIMIT, evening_data, size
from app.bot.widgets import live, template
from app.bot.widgets.transport import EditMessageWidgetData, SendWidget
from app.domain.prefs import day_start
from app.domain.timeframe import local_date

logger = logging.getLogger(__name__)

_CLOSE = re.compile(r"e:c:([0-9]{4}-[0-9]{2}-[0-9]{2}):([0-9a-z]{4,10})")
BACK_DAYS = 7
_SENT: dict[tuple[int, int], str] = {}  # (кто, сообщение) → chat_data при отправке


async def show(
    bot: Bot, user: User, assistant: Assistant, ctx: DayContext, day: date
) -> bool:
    """Отправить вечерний итог виджетом. False — не вышло: вызывающий шлёт rich-итог."""
    try:
        data = evening_data(ctx, day, await assistant.streak(user))
        msg = await bot(SendWidget(
            chat_id=user.telegram_id, html=template("evening"), chat_data=data,
            caption="🌙 Итог дня", allow_queries=True,  # уведомление; цифры — в виджете (D-60)
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("widget: вечерний итог не ушёл (%s) — фолбэк на rich", exc)
        return False
    if len(_SENT) > 50:  # ponytail: двое людей, одно сообщение в день — чистим целиком
        _SENT.clear()
    _SENT[(user.telegram_id, msg.message_id)] = data
    return True


async def on_close(
    uid: int, mid: int, data: str, user: User, assistant: Assistant
) -> live.Outcome:
    """`e:c:<день>:<ключ>` — «✅ Всё записал» (D-57)."""
    m = _CLOSE.fullmatch(data)
    if m is None:
        return live.Outcome({"err": "bad"})
    try:
        day = date.fromisoformat(m.group(1))
    except ValueError:
        return live.Outcome({"err": "bad"})
    today = local_date(datetime.now(UTC), user.timezone, day_start(user.ui_prefs))
    if not today - timedelta(days=BACK_DAYS) <= day <= today:
        return live.Outcome({"err": "bad"})
    await assistant.close_day(user, day)  # False — уже закрыт: ответ тот же
    streak = await assistant.streak(user)
    reply: dict[str, Any] = {"ok": 1}
    if streak.of > 0:  # как `evening_data`: нет истории — нет пилюли, а не «0 из 0»
        reply["s"] = [streak.days, int(streak.frozen), streak.covered, streak.of]
    sent = _SENT.get((uid, mid))

    async def after(bot: Bot) -> None:
        if sent is None:
            return
        obj = json.loads(sent)
        obj["c"] = 1
        if "s" in reply:
            obj["s"] = reply["s"]
        fresh = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        while size(fresh) > LIMIT and obj.get("nt"):  # серия могла добавить байты к впритык
            obj["nt"].pop()
            fresh = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        try:
            await bot(EditMessageWidgetData(chat_id=uid, message_id=mid, chat_data=fresh))
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        _SENT[(uid, mid)] = fresh

    return live.Outcome(reply, after=after)


live.ROUTES["e"] = on_close
