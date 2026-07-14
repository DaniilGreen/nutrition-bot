"""ВРЕМЕННОЕ демо: как разделы выглядят на Rich Messages (Bot API 10.1) — нативными таблицами.

Кнопка «🧪 Демо» шлёт ОДНО сообщение: Сегодня / Неделя / Месяц / История / Продукты — таблицами,
которые верстает КЛИЕНТ (значит, бары и колонки не разъезжаются на Android — то, из-за чего мы
ушли в PNG). Цель — оценить дизайн до миграции поверхностей. Удалить после решения.
"""
from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from app.application.entities import Product
from app.application.results import DailyStats, DayView, MonthReport
from app.domain.goals import Goal, in_goal

_WD = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_BAR_W = 10


def _n(v: float) -> str:
    return str(round(v))


def _esc(s: str) -> str:
    """Пайп сломал бы разметку таблицы."""
    return s.replace("|", "/").strip()


def _bar(cons: float, goal: float) -> str:
    ratio = max(0.0, min(1.0, cons / goal)) if goal else 0.0
    filled = round(_BAR_W * ratio)
    return "▰" * filled + "▱" * (_BAR_W - filled)


def _today(st: DailyStats) -> str:
    c, g = st.remaining.consumed, st.remaining.goal
    rows = (
        ("Ккал", c.kcal, g.daily_kcal),
        ("Белок", c.protein, g.protein_g),
        ("Жир", c.fat, g.fat_g),
        ("Углеводы", c.carb, g.carb_g),
    )
    out = ["## 📊 Сегодня", "", "| | Съедено / Цель | Прогресс | % |", "|---|---|---|---|"]
    for lab, cons, goal in rows:
        pct = round(100 * cons / goal) if goal else 0
        out.append(f"| **{lab}** | {_n(cons)} / {_n(goal)} | {_bar(cons, goal)} | {pct}% |")
    return "\n".join(out)


def _eaten(st: DailyStats) -> str:
    if not st.meals:
        return ""
    tz = ZoneInfo(st.timezone)
    lines: list[str] = []
    for m in st.meals:
        when = m.eaten_at.astimezone(tz).strftime("%H:%M")
        for it in m.items:
            lines.append(f"- {when} · {_esc(it.name_snapshot)} — {_n(it.macros.kcal)} ккал")
            if len(lines) >= 8:
                break
    return "**Съедено сегодня:**\n" + "\n".join(lines)


def _week(days: list[tuple[date, float]], goal: Goal) -> str:
    out = ["## 📈 Неделя", "", "| День | Ккал | В цели |", "|---|---|---|"]
    for d, kcal in days:
        mark = "—" if kcal <= 0 else ("✅" if in_goal(kcal, goal.daily_kcal) else "❌")
        out.append(f"| {_WD[d.weekday()]} {d.day}.{d.month:02d} | {_n(kcal)} | {mark} |")
    return "\n".join(out)


def _month(rep: MonthReport) -> str:
    a = rep.avg_macros
    if rep.weight_delta is None:
        wd = "—"
    else:
        wd = f"{'+' if rep.weight_delta > 0 else ''}{rep.weight_delta:g} кг"
    return "\n".join([
        f"## 📅 Месяц ({rep.total_days} дней)",
        "",
        "| Метрика | Значение |",
        "|---|---|",
        f"| Ср. ккал/день | {_n(a.kcal)} |",
        f"| Дней в цели | {rep.days_in_goal} / {rep.days_logged} |",
        f"| Δ веса | {wd} |",
        f"| Ср. Б/Ж/У | {_n(a.protein)} / {_n(a.fat)} / {_n(a.carb)} г |",
    ])


def _history(dv: DayView) -> str:
    tz = ZoneInfo(dv.timezone)
    out = ["## 🗒 История — сегодня", "", "| Время | Продукт | Ккал |", "|---|---|---|"]
    rows = 0
    for m in dv.meals:
        when = m.eaten_at.astimezone(tz).strftime("%H:%M")
        for it in m.items:
            out.append(f"| {when} | {_esc(it.name_snapshot)} | {_n(it.macros.kcal)} |")
            rows += 1
            if rows >= 8:
                return "\n".join(out)
    return "\n".join(out) if rows else "## 🗒 История — сегодня\n\nЗа сегодня пусто."


def _products(products: list[Product]) -> str:
    if not products:
        return ""
    out = ["## 🛠 Продукты (где я прикинул сам)", "", "| Продукт | Ккал/100 г | |", "|---|---|---|"]
    for p in products[:6]:
        out.append(f"| {_esc(p.display_name)} | {_n(p.density.kcal_per_100g)} | ≈ |")
    return "\n".join(out)


def build_demo_markdown(
    st: DailyStats,
    week_days: list[tuple[date, float]],
    goal: Goal,
    rep: MonthReport,
    dv: DayView,
    products: list[Product],
) -> str:
    """Одно сообщение со ВСЕМИ разделами в нативных таблицах — для оценки дизайна."""
    parts = [
        "# 🧪 Демо: гибрид — таблицы + инфографика",
        "_Цифры — **нативными таблицами** (их верстает клиент, колонки и бары не разъезжаются). "
        "Графика — **нашими картинками альбомом ниже**: то, что таблицей не нарисуешь._",
        _today(st),
        _eaten(st),
        _week(week_days, goal),
        _month(rep),
        _history(dv),
        _products(products),
        "---",
        "<details><summary>🧪 Сворачиваемый блок — тапни, чтобы раскрыть</summary>\n\n"
        "Если раскрылось — длинные списки (съеденное, история) прячем сюда, и лимит подписи "
        "1024 больше не нужен.\n\n</details>",
        "⬇️ _Ниже — альбом графиков: неделя, дни в цели, вес, кольцо Б/Ж/У._",
    ]
    return "\n\n".join(p for p in parts if p)
