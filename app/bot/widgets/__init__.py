"""HTML-виджеты Telegram (Bot API 10.4, FR-49): шаблоны `<имя>.html`, транспорт, живой «Сегодня».

Шаблон — статический файл рядом с этим модулем; данные сообщения собирает `app/bot/ui/widgets.py`.
"""
from __future__ import annotations

from functools import cache
from pathlib import Path

_DIR = Path(__file__).parent


# Шаблоны, собранные из общей части и вида панели (`<!--P-->` ← `<имя>.<вид>.html`, D-78).
PARTS = {"today": ("bars", "rings", "kcal")}


@cache
def template(name: str, part: str | None = None) -> str:
    """HTML шаблона. Бюджет (≤ 4096 байт после zlib-9) стережёт тест W-INV-3, не рантайм.

    У «Сегодня» три вида панели, как в аппке (`ui_prefs.hero`): вместе они не влезают в бюджет
    Telegram, поэтому шаблон собирается при отправке — общая часть + панель человека."""
    html = (_DIR / f"{name}.html").read_text(encoding="utf-8")
    if name in PARTS:
        kind = part if part in PARTS[name] else PARTS[name][0]
        html = html.replace("<!--P-->", (_DIR / f"{name}.{kind}.html").read_text(encoding="utf-8"))
    from app.bot.widgets.kit import build  # кит: отлив, движение, иконки (D-81, D-83, D-84)

    return build(html)
