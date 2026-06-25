"""Рендер дашбордов/графиков КБЖУ картинками (PNG) — пиксель-перфектно на любой платформе.

Текстовые бары в <code> на Android разъезжаются (кириллица не моноширинна), поэтому всё
визуальное (бары, графики, кольцо, хитмап) рисуем картинкой. Кириллица — DejaVu (fonts-dejavu-core).
Всё рисуется ЛОКАЛЬНО (Pillow), без OpenAI — рендер ~мс, ~десятки КБ.
"""
from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from app.application.results import MonthReport
from app.domain.goals import IN_GOAL_HIGH, Goal, Remaining, in_goal
from app.domain.value_objects import Macros

_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
# тёмная тема, палитра «Минимал»: Ккал-василёк (отдельно от макросов), Белок-красный,
# Жир-янтарь, Углеводы-тил. Ккал намеренно другого семейства — читается как «итог».
_COLORS = ((124, 156, 224), (235, 122, 120), (224, 169, 59), (95, 188, 176))
_OK, _OVER, _OK_DIM = (92, 184, 122), (232, 115, 111), (58, 96, 80)  # в цели / перебор / мало
_MUTED, _BG = (52, 57, 66), (28, 31, 38)  # пустой бар/клетка · тёмный фон карточки
_INK, _GREY = (237, 239, 243), (151, 157, 166)  # светлый текст · приглушённый
_WD = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_MON = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


@lru_cache(maxsize=16)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_FONT_BOLD if bold else _FONT, size)


def _png(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _delta_str(delta: float | None) -> str:
    if delta is None:
        return "—"
    return f"{'+' if delta > 0 else ''}{delta:g} кг"


def _draw_bars(d: ImageDraw.ImageDraw, rem: Remaining, x: int, y: int, w: int) -> int:
    """4 бара КБЖУ начиная с (x,y), ширина блока w. Возвращает Y под последним баром."""
    c, g = rem.consumed, rem.goal
    rows = [
        ("Ккал", c.kcal, g.daily_kcal),
        ("Белок", c.protein, g.protein_g),
        ("Жир", c.fat, g.fat_g),
        ("Углеводы", c.carb, g.carb_g),
    ]
    row_h, bh = 56, 22
    bar_x = x + 340
    bar_w = w - 340 - 66
    for i, (label, cons, goal) in enumerate(rows):
        cy = y + i * row_h + row_h // 2
        pct = round(100 * cons / goal) if goal else 0
        nums = f"{round(cons)}/{round(goal)}"
        d.text((x, cy), label, font=_font(26, True), fill=_INK, anchor="lm")
        d.text((x + 170, cy), nums, font=_font(20), fill=_GREY, anchor="lm")
        by = cy - bh // 2
        d.rounded_rectangle([bar_x, by, bar_x + bar_w, by + bh], radius=11, fill=_MUTED)
        ratio = max(0.0, min(1.0, cons / goal)) if goal else 0.0
        if ratio > 0:
            fw = max(bh, round(bar_w * ratio))
            d.rounded_rectangle([bar_x, by, bar_x + fw, by + bh], radius=11, fill=_COLORS[i])
        d.text((bar_x + bar_w + 12, cy), f"{pct}%", font=_font(20), fill=_INK, anchor="lm")
    return y + len(rows) * row_h


def render_dashboard(rem: Remaining) -> bytes:
    """Бары КБЖУ (после приёма / fallback). PNG-байты."""
    img = Image.new("RGB", (720, 252), _BG)
    _draw_bars(ImageDraw.Draw(img), rem, 22, 14, 698)
    return _png(img)


def _draw_ring(d: ImageDraw.ImageDraw, consumed: Macros, cx: int, cy: int, r: int) -> None:
    """Донат Б/Ж/У (доли по вкладу в калории), центр — съеденные ккал. Снизу — легенда Б/Ж/У."""
    pk, fk, ck = 4 * consumed.protein, 9 * consumed.fat, 4 * consumed.carb
    total = pk + fk + ck
    box = [cx - r, cy - r, cx + r, cy + r]
    if total <= 0:
        d.ellipse(box, fill=_MUTED)
    else:
        start = -90.0
        for val, col in ((pk, _COLORS[1]), (fk, _COLORS[2]), (ck, _COLORS[3])):
            end = start + 360 * val / total
            d.pieslice(box, start, end, fill=col)
            start = end
    hole = r - 34
    d.ellipse([cx - hole, cy - hole, cx + hole, cy + hole], fill=_BG)
    d.text((cx, cy - 12), f"{round(consumed.kcal)}", font=_font(30, True), fill=_INK, anchor="mm")
    d.text((cx, cy + 20), "ккал", font=_font(15), fill=_GREY, anchor="mm")
    # легенда Б/Ж/У под кольцом
    labels = (("Б", consumed.protein, _COLORS[1]), ("Ж", consumed.fat, _COLORS[2]),
              ("У", consumed.carb, _COLORS[3]))
    n = len(labels)
    for i, (lab, grams, col) in enumerate(labels):
        lx = cx - r + (2 * r) * (i + 0.5) / n
        ly = cy + r + 26
        d.text((lx, ly), f"{lab} {round(grams)}г", font=_font(18, True), fill=col, anchor="mm")


def render_today(rem: Remaining) -> bytes:
    """📊 Сегодня: бары КБЖУ + кольцо Б/Ж/У под ними (на всю ширину — крупно на телефоне)."""
    img = Image.new("RGB", (720, 530), _BG)
    d = ImageDraw.Draw(img)
    bottom = _draw_bars(d, rem, 22, 24, 698)
    _draw_ring(d, rem.consumed, 360, bottom + 132, 108)
    return _png(img)


def render_day_card(rem: Remaining, day: date) -> bytes:
    """Карточка дня для шеринга: дата · бары · кольцо Б/Ж/У · подвал."""
    img = Image.new("RGB", (720, 620), _BG)
    d = ImageDraw.Draw(img)
    title = f"{day.day} {_MON[day.month - 1]}"
    d.text((24, 22), title, font=_font(30, True), fill=_INK, anchor="lt")
    d.line([(24, 70), (696, 70)], fill=_MUTED, width=2)
    bottom = _draw_bars(d, rem, 24, 88, 696)
    _draw_ring(d, rem.consumed, 360, bottom + 120, 100)  # кольцо + легенда (≈ до y 560)
    d.text((24, 592), "@Nutrition_calorie_bot · учёт КБЖУ", font=_font(16), fill=_GREY, anchor="lt")
    return _png(img)


def render_goal_card(goal: Goal) -> bytes:
    """Карточка дневной цели: кольцо целевых Б/Ж/У, центр — целевые ккал."""
    img = Image.new("RGB", (560, 380), _BG)
    d = ImageDraw.Draw(img)
    d.text((280, 30), "Цель на день", font=_font(26, True), fill=_INK, anchor="mt")
    target = Macros(goal.daily_kcal, goal.protein_g, goal.fat_g, goal.carb_g)
    _draw_ring(d, target, 280, 210, 110)
    return _png(img)


def render_month_card(rep: MonthReport) -> bytes:
    """Отчёт за период: ср. ккал, дней в цели, Δ веса + кольцо средних Б/Ж/У в день."""
    img = Image.new("RGB", (720, 420), _BG)
    d = ImageDraw.Draw(img)
    head = f"Последние {rep.total_days} дней"
    d.text((24, 22), head, font=_font(26, True), fill=_INK, anchor="lt")
    d.line([(24, 66), (696, 66)], fill=_MUTED, width=2)
    metrics = (
        (f"{round(rep.avg_macros.kcal)}", "ср. ккал/день"),
        (f"{rep.days_in_goal}/{rep.days_logged}", "дней в цели"),
        (_delta_str(rep.weight_delta), "вес за период"),
    )
    for i, (val, lab) in enumerate(metrics):
        y = 110 + i * 88
        d.text((40, y), val, font=_font(34, True), fill=_INK, anchor="lt")
        d.text((40, y + 44), lab, font=_font(17), fill=_GREY, anchor="lt")
    _draw_ring(d, rep.avg_macros, 530, 230, 108)
    return _png(img)


def render_label_card(name: str, kcal: float, protein: float, fat: float, carb: float) -> bytes:
    """Карточка распознанной этикетки: имя + КБЖУ на 100 г (цветные точки макросов)."""
    img = Image.new("RGB", (640, 260), _BG)
    d = ImageDraw.Draw(img)
    d.text((24, 22), "Прочитал этикетку", font=_font(22, True), fill=_INK, anchor="lt")
    nm = name if len(name) <= 36 else name[:35] + "…"
    d.text((24, 58), nm, font=_font(19), fill=_GREY, anchor="lt")
    rows = (("Ккал", kcal, _COLORS[0]), ("Белок", protein, _COLORS[1]),
            ("Жир", fat, _COLORS[2]), ("Углеводы", carb, _COLORS[3]))
    for i, (lab, val, col) in enumerate(rows):
        y = 102 + i * 34
        d.ellipse([24, y + 3, 40, y + 19], fill=col)
        d.text((54, y), lab, font=_font(18, True), fill=_INK, anchor="lt")
        d.text((300, y), f"{round(val)}", font=_font(18), fill=_INK, anchor="lt")
    d.text((300, 102 + 4 * 34 + 4), "на 100 г", font=_font(14), fill=_GREY, anchor="lt")
    return _png(img)


def render_weekly_chart(days: list[tuple[date, float]], goal: float) -> bytes:
    """Столбики калорий за N дней + пунктир цели. Зелёный ≤ цели, красный > цели."""
    img = Image.new("RGB", (720, 380), _BG)
    d = ImageDraw.Draw(img)
    d.text((24, 18), "Калории по дням", font=_font(24, True), fill=_INK, anchor="lt")
    ml, mt, mb = 24, 70, 56
    plot_h, plot_w = 380 - mt - mb, 720 - ml - 24
    maxv = max([k for _, k in days] + [goal, 1.0]) * 1.12
    gy = mt + plot_h * (1 - goal / maxv)
    for gx in range(ml, 700, 14):  # пунктир цели
        d.line([(gx, gy), (gx + 7, gy)], fill=_GREY, width=2)
    d.text((696, gy - 6), f"цель {round(goal)}", font=_font(15), fill=_GREY, anchor="rb")
    slot = plot_w / max(1, len(days))
    bw = slot * 0.6
    for i, (day, kcal) in enumerate(days):
        x = ml + i * slot + (slot - bw) / 2
        h = plot_h * min(1.0, kcal / maxv)
        y0 = mt + plot_h - h
        col = _OK if kcal <= goal or goal <= 0 else _OVER
        d.rounded_rectangle([x, y0, x + bw, mt + plot_h], radius=6, fill=col)
        if kcal > 0:
            d.text((x + bw / 2, y0 - 4), f"{round(kcal)}", font=_font(14), fill=_GREY, anchor="md")
        wd = _WD[day.weekday()]
        d.text((x + bw / 2, mt + plot_h + 6), wd, font=_font(16), fill=_INK, anchor="ma")
    return _png(img)


def render_weight_chart(points: list[tuple[datetime, float]]) -> bytes:
    """Линия тренда веса: точки, ДАТЫ по оси X, значения концов и дельта за период."""
    img = Image.new("RGB", (720, 360), _BG)
    d = ImageDraw.Draw(img)
    d.text((24, 18), "Вес", font=_font(24, True), fill=_INK, anchor="lt")
    ml, mt, mb, mr = 24, 70, 64, 70
    plot_h, plot_w = 360 - mt - mb, 720 - ml - mr
    ws = [w for _, w in points]
    lo, hi = min(ws), max(ws)
    span = (hi - lo) or 1.0
    n = len(points)
    xs = [ml + (plot_w * i / max(1, n - 1)) for i in range(n)]
    ys = [mt + plot_h * (1 - (w - lo) / span) for w in ws]
    base = mt + plot_h
    d.line([(ml, base), (ml + plot_w, base)], fill=_MUTED, width=2)  # ось X
    d.line(list(zip(xs, ys, strict=True)), fill=_COLORS[0], width=3, joint="curve")
    for x, y in zip(xs, ys, strict=True):
        d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=_COLORS[0])
    d.text((xs[0], ys[0] - 10), f"{ws[0]:g}", font=_font(16, True), fill=_INK, anchor="mb")
    d.text((xs[-1], ys[-1] - 10), f"{ws[-1]:g}", font=_font(16, True), fill=_INK, anchor="mb")
    step = 1 if n <= 8 else (n + 6) // 7  # не чаще ~7 подписей дат, чтобы не слипались
    for i, (dt, _w) in enumerate(points):
        if i % step == 0 or i == n - 1:
            d.text((xs[i], base + 10), f"{dt.day}.{dt.month:02d}", font=_font(14),
                   fill=_GREY, anchor="ma")
    delta = round(ws[-1] - ws[0], 1)
    d.text((720 - mr, 24), _delta_str(delta), font=_font(20, True),
           fill=_OVER if delta > 0 else _OK, anchor="rt")
    return _png(img)


def _streak(days: list[tuple[date, float]], goal: float) -> int:
    """Серия подряд «зелёных» дней (в цели) с конца. Совпадает с раскраской клеток
    (0.5–1.1 цели): хвостовые незаполненные дни пропускаем, любой иной — рвёт серию."""
    n = 0
    for _day, kcal in reversed(days):
        if kcal <= 0:
            if n == 0:
                continue  # сегодня ещё не ел — не рвёт серию
            break
        if not in_goal(kcal, goal):  # тот же коридор, что и раскраска клеток
            break
        n += 1
    return n


def render_heatmap(days: list[tuple[date, float]], goal: float) -> bytes:
    """Календарь-хитмап: клетка на день, цвет = ккал/цель (серый-зелёный-красный)."""
    cell, gap, top, left = 56, 8, 80, 24
    pad = days[0][0].weekday() if days else 0  # сдвиг первой недели по дню недели
    weeks = (pad + len(days) + 6) // 7  # учитываем сдвиг — иначе последняя неделя обрезается
    img = Image.new("RGB", (left * 2 + 7 * (cell + gap), top + weeks * (cell + gap) + 20), _BG)
    d = ImageDraw.Draw(img)
    d.text((left, 18), "Дни в цели", font=_font(24, True), fill=_INK, anchor="lt")
    streak = _streak(days, goal)
    if streak >= 2:
        d.text((img.width - left, 26), f"серия {streak} дн.", font=_font(18, True),
               fill=_OK, anchor="rt")
    for i, wd in enumerate(_WD):
        wx = left + i * (cell + gap) + cell // 2
        d.text((wx, 56), wd, font=_font(15), fill=_GREY, anchor="mm")
    for idx, (day, kcal) in enumerate(days):
        slot = idx + pad
        x = left + (slot % 7) * (cell + gap)
        y = top + (slot // 7) * (cell + gap)
        if kcal <= 0:
            color = _MUTED
        elif goal and kcal > goal * IN_GOAL_HIGH:
            color = _OVER
        elif in_goal(kcal, goal):
            color = _OK
        else:
            color = _OK_DIM  # залогирован, но <50% цели — недобор
        d.rounded_rectangle([x, y, x + cell, y + cell], radius=8, fill=color)
        d.text((x + cell // 2, y + cell // 2), str(day.day), font=_font(16),
               fill=_INK if kcal > 0 else _GREY, anchor="mm")
    return _png(img)
