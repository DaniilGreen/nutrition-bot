"""ВРЕМЕННОЕ демо: три полных дизайна интерфейса на Rich Messages (Bot API 10.1). Без картинок.

Дизайны отличаются НЕ покраской, а тем, на какой вопрос отвечает экран:
  A «Дашборд» — «покажи все цифры»  → всё видно сразу, четыре бара, ноль кликов.
  B «Тренер»  — «что мне делать»    → вердикт сверху, бары только ккал+белок, детали под кат.
  C «Лента»   — «как прошёл день»   → хронология приёмов, накопительный итог, белок по приёмам.

Каждый покрывает ВСЕ разделы: Сегодня · Приём · Неделя · День · Продукты · Вес · Цель · Итог дня ·
Месяц. Выбрать надо один — и раскатать на прод, удалив этот пакет.
"""
from __future__ import annotations

from collections.abc import Callable

from app.bot.demo import a, b, c
from app.bot.demo.data import DemoData, collect

Design = Callable[[DemoData], list[tuple[str, str]]]

DESIGNS: dict[str, tuple[str, Design]] = {
    "a": ("A · Дашборд", a.build),
    "b": ("B · Тренер", b.build),
    "c": ("C · Лента", c.build),
}

__all__ = ["DESIGNS", "DemoData", "Design", "collect"]
