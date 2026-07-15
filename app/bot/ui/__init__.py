"""Презентационный слой бота: единый стиль «Дашборд» на Rich Messages (Bot API 10.1).

`gather()` собирает дневной контекст (остаток, неделя, вес, добор), `screens.*` рендерят экраны,
`kit`/`blocks`/`topup` — примитивы и общие блоки. Физика вёрстки и продуктовые решения — в
докстрингах модулей и в SPEC (D-22…D-30).
"""
from __future__ import annotations

from app.bot.ui import screens
from app.bot.ui.context import DayContext, gather

__all__ = ["DayContext", "gather", "screens"]
