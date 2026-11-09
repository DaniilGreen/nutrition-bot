"""HTML-виджеты Telegram (Bot API 10.4, FR-49): шаблоны `<имя>.html`, транспорт, живой «Сегодня».

Шаблон — статический файл рядом с этим модулем; данные сообщения собирает `app/bot/ui/widgets.py`.
"""
from __future__ import annotations

from functools import cache
from pathlib import Path

_DIR = Path(__file__).parent


@cache
def template(name: str) -> str:
    """HTML шаблона. Бюджет (≤ 4096 байт после zlib-9) стережёт тест W-INV-3, не рантайм."""
    return (_DIR / f"{name}.html").read_text(encoding="utf-8")
