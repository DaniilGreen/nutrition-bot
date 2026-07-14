"""ВРЕМЕННОЕ демо: сравнение подач бара в Rich Messages (Bot API 10.1), HTML-режим.

Эмодзи-квадраты отвергнуты: они двойной ширины → бар переносится на вторую строку и «съезжает».
Здесь — 4 подачи ОДНИХ И ТЕХ ЖЕ данных, чтобы выбрать глазами:

  A. Типографика — только цифры и %, без бара (ничего не может переехать).
  B. Текстовый бар — █/░: одинарная ширина, в 2 раза уже эмодзи, в ячейке не переносится.
  C. Бар с подсветкой <mark> (RichTextMarked) — цвет БЕЗ квадратов.
  D. Картинка (наш PNG) — шлётся отдельно после сообщения.

Плюс показано, где таблица объективно хороша: История и Продукты (списки чисел).
Удалить после решения.
"""
from __future__ import annotations

from datetime import date
from html import escape
from zoneinfo import ZoneInfo

from app.application.entities import Product
from app.application.results import DailyStats, DayView
from app.domain.goals import Goal, in_goal

_WD = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_BAR_W = 10


def _n(v: float) -> str:
    return str(round(v))


def _e(s: str) -> str:
    return escape(s.strip(), quote=False)


def _pct(cons: float, goal: float) -> int:
    return round(100 * cons / goal) if goal else 0


def _filled(cons: float, goal: float) -> int:
    ratio = max(0.0, min(1.0, cons / goal)) if goal else 0.0
    return round(_BAR_W * ratio)


def _rows(st: DailyStats) -> list[tuple[str, float, float]]:
    c, g = st.remaining.consumed, st.remaining.goal
    return [
        ("Ккал", c.kcal, g.daily_kcal),
        ("Белок", c.protein, g.protein_g),
        ("Жир", c.fat, g.fat_g),
        ("Углеводы", c.carb, g.carb_g),
    ]


def _variant_a(st: DailyStats) -> str:
    """Только цифры: максимально чисто, переносить нечего."""
    out = ["<h2>A. Только цифры</h2>", "<table>",
           "<tr><th></th><th>Съедено / Цель</th><th>%</th></tr>"]
    for lab, cons, goal in _rows(st):
        out.append(
            f"<tr><td><b>{lab}</b></td><td>{_n(cons)} / {_n(goal)}</td>"
            f"<td><b>{_pct(cons, goal)}%</b></td></tr>"
        )
    out.append("</table>")
    return "\n".join(out)


def _variant_b(st: DailyStats) -> str:
    """Текстовый бар █/░ — одинарная ширина, не переносится."""
    out = ["<h2>B. Текстовый бар (█░)</h2>", "<table>"]
    for lab, cons, goal in _rows(st):
        f = _filled(cons, goal)
        bar = "█" * f + "░" * (_BAR_W - f)
        out.append(
            f"<tr><td><b>{lab}</b></td><td>{bar}</td><td>{_pct(cons, goal)}%</td></tr>"
        )
    out.append("</table>")
    return "\n".join(out)


def _variant_c(st: DailyStats) -> str:
    """Бар с подсветкой <mark> — цвет без квадратов."""
    out = ["<h2>C. Бар с подсветкой (mark)</h2>", "<table>"]
    for lab, cons, goal in _rows(st):
        f = _filled(cons, goal)
        bar = f"<mark>{'█' * f}</mark>{'░' * (_BAR_W - f)}" if f else "░" * _BAR_W
        out.append(
            f"<tr><td><b>{lab}</b></td><td>{bar}</td><td>{_pct(cons, goal)}%</td></tr>"
        )
    out.append("</table>")
    return "\n".join(out)


def _week(days: list[tuple[date, float]], goal: Goal) -> str:
    out = ["<h2>📈 Неделя</h2>", "<table>",
           "<tr><th>День</th><th>Ккал</th><th></th></tr>"]
    for d, kcal in days:
        if kcal <= 0:
            mark = "—"
        else:
            mark = "в цели" if in_goal(kcal, goal.daily_kcal) else "мимо"
        out.append(
            f"<tr><td>{_WD[d.weekday()]} {d.day}.{d.month:02d}</td>"
            f"<td>{_n(kcal)}</td><td>{mark}</td></tr>"
        )
    out.append("</table>")
    return "\n".join(out)


def _history(dv: DayView) -> str:
    tz = ZoneInfo(dv.timezone)
    out = ["<h2>🗒 История — сегодня</h2>", "<table>",
           "<tr><th>Время</th><th>Продукт</th><th>Ккал</th></tr>"]
    rows = 0
    for m in dv.meals:
        when = m.eaten_at.astimezone(tz).strftime("%H:%M")
        for it in m.items:
            out.append(
                f"<tr><td>{when}</td><td>{_e(it.name_snapshot)}</td>"
                f"<td>{_n(it.macros.kcal)}</td></tr>"
            )
            rows += 1
            if rows >= 8:
                out.append("</table>")
                return "\n".join(out)
    out.append("</table>")
    return "\n".join(out) if rows else "<h2>🗒 История — сегодня</h2><p>Пусто.</p>"


def _products(products: list[Product]) -> str:
    if not products:
        return ""
    out = ["<h2>🛠 Продукты (прикинул сам)</h2>", "<table>",
           "<tr><th>Продукт</th><th>Ккал/100 г</th></tr>"]
    for p in products[:6]:
        out.append(
            f"<tr><td>{_e(p.display_name)}</td><td>{_n(p.density.kcal_per_100g)}</td></tr>"
        )
    out.append("</table>")
    return "\n".join(out)


def _eaten(st: DailyStats) -> str:
    if not st.meals:
        return ""
    tz = ZoneInfo(st.timezone)
    items: list[str] = []
    for m in st.meals:
        when = m.eaten_at.astimezone(tz).strftime("%H:%M")
        for it in m.items:
            items.append(f"<li>{when} · {_e(it.name_snapshot)} — {_n(it.macros.kcal)} ккал</li>")
            if len(items) >= 10:
                break
    body = "<ul>" + "".join(items) + "</ul>"
    return (
        f"<details><summary>🍽 Съедено сегодня ({st.meals_count} приёмов)</summary>"
        f"{body}</details>"
    )


def build_variants_html(
    st: DailyStats,
    week_days: list[tuple[date, float]],
    goal: Goal,
    dv: DayView,
    products: list[Product],
) -> str:
    """4 подачи одних данных + разделы, где таблица объективно уместна."""
    parts = [
        "<h1>🧪 Варианты дизайна бара</h1>",
        "<p><i>Квадраты-эмодзи убрал: они двойной ширины, поэтому бар переносился и «съезжал». "
        "Ниже — три подачи одних и тех же цифр, плюс четвёртая (картинка) отдельным сообщением. "
        "Скажи, какая нравится.</i></p>",
        _variant_a(st),
        _variant_b(st),
        _variant_c(st),
        "<hr>",
        "<p><b>А вот где таблица хороша без вопросов</b> — списки чисел:</p>",
        _week(week_days, goal),
        _history(dv),
        _products(products),
        _eaten(st),
        "<hr>",
        "<p><i>D — картинкой (наш PNG) — идёт следующим сообщением.</i></p>",
    ]
    return "\n".join(p for p in parts if p)
