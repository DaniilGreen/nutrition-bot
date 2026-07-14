"""ВРЕМЕННОЕ демо: гибрид «таблица = графика» на Rich Messages (Bot API 10.1).

Идея: не «таблицы отдельно, картинки отдельно», а ГРАФИЧНАЯ таблица — цветные блоки-эмодзи
внутри ячеек. Клиент верстает таблицу → блоки выравниваются (то, из-за чего мы ушли в PNG),
и при этом получаем цвет в нашей палитре: Ккал-василёк 🟦, Белок 🟥, Жир 🟨, Углеводы 🟩.

Нативно получаются: бары КБЖУ, кольцо Б/Ж/У (→ стековый бар долей), хитмап «дни в цели»
(→ таблица 7×N), бары недели. Картинкой остаётся только линия веса — её эмодзи не нарисуешь.

Вставить СВОЙ png прямо в ячейку нельзя: в Bot API 10.1 медиа в rich-разметке только по
публичному HTTP(S)-URL, а наши графики рендерятся в память. Появится в 10.2 (media/file_id).
Удалить после решения.
"""
from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from app.application.entities import Product
from app.application.results import DailyStats, DayView, MonthReport
from app.domain.goals import IN_GOAL_HIGH, IN_GOAL_LOW, Goal, in_goal

_WD = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
# палитра как в PNG: ккал-василёк, белок-красный, жир-янтарь, углеводы-тил
_KCAL, _PROT, _FAT, _CARB, _EMPTY = "🟦", "🟥", "🟨", "🟩", "⬜"
_OVER, _UNDER = "🟥", "🟨"  # перебор / недобор — как в хитмапе


def _n(v: float) -> str:
    return str(round(v))


def _esc(s: str) -> str:
    """Пайп сломал бы разметку таблицы."""
    return s.replace("|", "/").strip()


def _bar(cons: float, goal: float, fill: str, width: int = 8) -> str:
    """Цветной бар: заполненная часть — цветом макроса, остаток — пустыми блоками."""
    ratio = max(0.0, min(1.0, cons / goal)) if goal else 0.0
    n = round(width * ratio)
    return fill * n + _EMPTY * (width - n)


def _today(st: DailyStats) -> str:
    c, g = st.remaining.consumed, st.remaining.goal
    rows = (
        ("Ккал", c.kcal, g.daily_kcal, _KCAL),
        ("Белок", c.protein, g.protein_g, _PROT),
        ("Жир", c.fat, g.fat_g, _FAT),
        ("Углеводы", c.carb, g.carb_g, _CARB),
    )
    out = ["## 📊 Сегодня", "", "| | Прогресс | Съедено / Цель |", "|---|---|---|"]
    for lab, cons, goal, fill in rows:
        pct = round(100 * cons / goal) if goal else 0
        out.append(f"| **{lab}** | {_bar(cons, goal, fill)} {pct}% | {_n(cons)} / {_n(goal)} |")
    return "\n".join(out)


def _split(st: DailyStats) -> str:
    """Кольцо Б/Ж/У → стековый цветной бар: доли по вкладу в калории."""
    c = st.remaining.consumed
    pk, fk, ck = 4 * c.protein, 9 * c.fat, 4 * c.carb
    total = pk + fk + ck
    if total <= 0:
        return ""
    width = 10
    np_ = round(width * pk / total)
    nf = round(width * fk / total)
    nc = max(0, width - np_ - nf)
    bar = _PROT * np_ + _FAT * nf + _CARB * nc
    return (
        f"**Б/Ж/У по калориям:** {bar}\n"
        f"🟥 Белок {_n(c.protein)} г · 🟨 Жир {_n(c.fat)} г · 🟩 Углеводы {_n(c.carb)} г"
    )


def _eaten(st: DailyStats) -> str:
    if not st.meals:
        return ""
    tz = ZoneInfo(st.timezone)
    lines: list[str] = []
    for m in st.meals:
        when = m.eaten_at.astimezone(tz).strftime("%H:%M")
        for it in m.items:
            lines.append(f"- {when} · {_esc(it.name_snapshot)} — {_n(it.macros.kcal)} ккал")
            if len(lines) >= 10:
                break
    body = "\n".join(lines)
    return (
        f"<details><summary>🍽 Съедено сегодня ({st.meals_count} приёмов) — раскрыть</summary>\n\n"
        f"{body}\n\n</details>"
    )


def _day_color(kcal: float, goal: float) -> str:
    if kcal <= 0:
        return _EMPTY
    if goal and kcal > goal * IN_GOAL_HIGH:
        return _OVER
    if in_goal(kcal, goal):
        return _CARB
    return _UNDER


def _week(days: list[tuple[date, float]], goal: Goal) -> str:
    """Бар на каждый день: длина ∝ ккал/цель, цвет = в цели / перебор / недобор."""
    out = ["## 📈 Неделя", "", "| День | Ккал | |", "|---|---|---|"]
    for d, kcal in days:
        color = _day_color(kcal, goal.daily_kcal)
        bar = _bar(kcal, goal.daily_kcal, color, width=6) if kcal > 0 else _EMPTY * 6
        out.append(f"| {_WD[d.weekday()]} {d.day}.{d.month:02d} | {_n(kcal)} | {bar} |")
    return "\n".join(out)


def _heatmap(days: list[tuple[date, float]], goal: Goal, streak: int) -> str:
    """Хитмап «дни в цели» — нативной таблицей 7×N из цветных блоков."""
    head = "## 📅 Дни в цели"
    if streak >= 2:
        head += f" · серия {streak} дн."
    out = [head, "", "| " + " | ".join(_WD) + " |", "|" + "---|" * 7]
    pad = days[0][0].weekday() if days else 0
    cells = [_EMPTY] * pad + [_day_color(k, goal.daily_kcal) for _, k in days]
    for i in range(0, len(cells), 7):
        row = cells[i : i + 7]
        row += [_EMPTY] * (7 - len(row))
        out.append("| " + " | ".join(row) + " |")
    out.append("")
    out.append(f"{_CARB} в цели · {_OVER} перебор · {_UNDER} недобор · {_EMPTY} нет записи")
    return "\n".join(out)


def _month(rep: MonthReport) -> str:
    a = rep.avg_macros
    if rep.weight_delta is None:
        wd = "—"
    else:
        wd = f"{'+' if rep.weight_delta > 0 else ''}{rep.weight_delta:g} кг"
    hit = _bar(rep.days_in_goal, max(1, rep.days_logged), _CARB, width=8)
    return "\n".join([
        f"## 🗓 Месяц ({rep.total_days} дней)",
        "",
        "| Метрика | Значение |",
        "|---|---|",
        f"| Ср. ккал/день | **{_n(a.kcal)}** |",
        f"| Дней в цели | {hit} {rep.days_in_goal}/{rep.days_logged} |",
        f"| Δ веса | **{wd}** |",
        f"| Ср. Б/Ж/У | 🟥 {_n(a.protein)} · 🟨 {_n(a.fat)} · 🟩 {_n(a.carb)} г |",
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
    out = ["## 🛠 Продукты (прикинул сам)", "", "| Продукт | Ккал/100 г | |", "|---|---|---|"]
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
    month_days: list[tuple[date, float]],
    streak: int,
) -> str:
    """Всё графикой ВНУТРИ таблиц: цветные бары, стековый Б/Ж/У, эмодзи-хитмап."""
    parts = [
        "# 🧪 Демо: графика внутри таблиц",
        f"_Бары и хитмап — цветными блоками прямо в ячейках (палитра как в PNG: "
        f"{_KCAL} ккал · {_PROT} белок · {_FAT} жир · {_CARB} углеводы). "
        f"Таблицу верстает клиент → ничего не разъезжается. Картинкой — только линия веса._",
        _today(st),
        _split(st),
        _eaten(st),
        _week(week_days, goal),
        _heatmap(month_days, goal, streak),
        _month(rep),
        _history(dv),
        _products(products),
        "---",
        f"_Коридор «в цели» — {int(IN_GOAL_LOW * 100)}–{int(IN_GOAL_HIGH * 100)}% дневной цели "
        f"(тот же критерий, что в `/streak` и `/month`)._",
    ]
    return "\n\n".join(p for p in parts if p)
