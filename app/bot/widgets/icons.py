"""Иконки виджетов (D-81): тот же набор правил, что у категорий финансового бота.

Контур 24×24, обводка 2, скруглённые концы и стыки, рисунок от края до края (19–21 единица) —
один мелкий значок выбивается из строки заметнее, чем кажется. Цвет — `currentColor`.

В шаблоне — метка `{{ic:имя}}`; `widgets.template` заменяет её инлайн-`<svg>` и добавляет в стили
правило `.ic`. Подпутей в одном `d` хватает: стиль у всех частей значка один.
"""
from __future__ import annotations

import re

ICONS: dict[str, str] = {  # целая сетка: дробные координаты удваивали вес шаблона (бюджет 4 КБ)
    "trash": "M3 6H21M9 6V3h6v3M5 6l1.5 16h11L19 6M10 11v6m4-6v6",
    "back": "M9 3L3 9l6 6M3 9h12a6.5 6.5 0 0 1 0 13H9",
    "redo": "M3 12a9 9 0 0 1 17-4M21 3v5h-5m5 4a9 9 0 0 1-17 4M3 21v-5h5",
    "ok": "M3 13l6 6L21 5",
    "x": "M5 5l14 14M19 5L5 19",
    "pin": "M8 3h8m-6 0v6l-4 5h12l-4-5V3m-2 11v8",
    "fire": "M12 22c-4 0-8-3-8-7.5S9 8 9 2c4 2.5 5 6.5 4 9 2-1 3-3 3-5 3 2.5 4 5.5 4 8.5"
            "S16 22 12 22z",
    "snow": "M12 2v20M3 7l18 10M21 7L3 17",
    "hist": "M3 12a9 9 0 1 0 3-6.7M3 3v5h5m4-1v5l3.5 2.5",
    "goal": "M12 2.5a9.5 9.5 0 1 0 0 19 9.5 9.5 0 1 0 0-19zm0 5"
            "a4.5 4.5 0 1 0 0 9 4.5 4.5 0 1 0 0-9z",
    "plate": "M2 2v5.5a3.5 3.5 0 0 0 7 0V2M5.5 2v5.5m0 4V22M20.5 22V2c-2.5 2.5-3 7-2.2 10h2.2",
    "moon": "M21 14.5A9 9 0 1 1 9.5 3a7 7 0 0 0 11.5 11.5z",
    "week": "M2 22h20M5 22v-9m5 9V6m5 16v-12m5 12V3",
    "scale": "M5 2.5h14a2.5 2.5 0 0 1 2.5 2.5v14a2.5 2.5 0 0 1-2.5 2.5H5A2.5 2.5 0 0 1 2.5 19V5"
             "A2.5 2.5 0 0 1 5 2.5zM7 10a5 5 0 0 1 10 0m-5 0l2-3",
    "bolt": "M13 2L4 14h7l-1 8 9-12h-7z",
    "down": "M12 3v18m-7-7l7 7 7-7",
    "up": "M12 21V3M5 10l7-7 7 7",
}

CSS = (".ic{width:1.1em;height:1.1em;vertical-align:-.2em;fill:none;stroke:currentColor;"
       "stroke-width:2;stroke-linecap:round;stroke-linejoin:round}")
_MARK = re.compile(r"\{\{ic:([a-z]+)\}\}")


def inline(html: str) -> str:
    """Метки `{{ic:имя}}` → инлайн-SVG; правило `.ic` — в первый `<style>`. Нет меток — как было.
    Неизвестное имя — KeyError на сборке: опечатка не должна уехать пустым местом."""
    if not _MARK.search(html):
        return html
    svg = '<svg class=ic viewBox="0 0 24 24"><path d="{}"/></svg>'
    html = _MARK.sub(lambda m: svg.format(ICONS[m.group(1)]), html)
    return html.replace("</style>", CSS + "</style>", 1)
