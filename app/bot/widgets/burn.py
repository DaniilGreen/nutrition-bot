"""«🔥 Реальный расход» виджетом (FR-50, D-65): та же сводка состава тела, что в «Итогах» аппки.

Только показ: расход по InBody с погрешностью, формула для сравнения, сверка дневника, цель по
расходу. Цель применяют в аппке (MA-INV-52) — кнопка «📱 Открыть в приложении» на «Итоги».
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from aiogram import Bot

from app.application.body_scans import BodyScanService
from app.application.entities import User
from app.bot.keyboards import open_in_app
from app.bot.ui.widgets import burn_data
from app.bot.widgets import template
from app.bot.widgets.transport import SendWidget
from app.domain.prefs import day_start
from app.domain.timeframe import local_date

logger = logging.getLogger(__name__)
CAPTION = "🔥 Реальный расход"
CALLBACK = "burn"  # кнопка под виджетом «⚖️ Вес»


async def data(user: User, scans: BodyScanService) -> str:
    today = local_date(datetime.now(UTC), user.timezone, day_start(user.ui_prefs))
    return burn_data(await scans.overview(user), today)


async def show(bot: Bot, chat_id: int, user: User, scans: BodyScanService) -> bool:
    """Отправить виджет. False — не вышло: вызывающий шлёт `text()` (W-INV-2)."""
    try:
        await bot(SendWidget(
            chat_id=chat_id, html=template("burn"), chat_data=await data(user, scans),
            reply_markup=open_in_app("body"),
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("widget: расход не ушёл (%s) — фолбэк на текст", exc)
        return False
    return True


def text(raw: str) -> str:
    """Те же строки текстом — без флага и при сбое виджета."""
    d = json.loads(raw)
    lines = [CAPTION]
    if d.get("st") == "ok":
        lines.append(f"{d['t']} ± {d['er']} ккал/день — по дневнику и замерам InBody")
    if d.get("fb"):
        tail = f" · ешь в среднем {d['in']}" if "in" in d else ""
        lines.append(f"По формуле {d['fb']} ккал/день{tail}")
    if d.get("pp"):
        lines.append(f"Цель по расходу: {d['pp']} ккал — применить в «Итогах» приложения.")
    if d.get("nt"):
        lines.append(d["nt"])
    if d.get("dt"):
        lines.append(f"{d['dt']}: {d['dx']}")
    return "\n".join(lines)
