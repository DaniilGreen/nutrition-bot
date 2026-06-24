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

from app.domain.goals import Remaining
from app.domain.value_objects import Macros

_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
# цвета: ккал-оранжевый, белок-красный, жир-золотой, угл-коричневый; статусы под/над целью
_COLORS = ((240, 138, 0), (214, 69, 65), (224, 168, 0), (150, 111, 51))
_OK, _OVER, _MUTED, _BG = (76, 175, 80), (224, 90, 80), (233, 233, 233), (255, 255, 255)
_INK, _GREY = (30, 30, 30), (110, 110, 110)
_WD = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_MON = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


@lru_cache(maxsize=16)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_FONT_BOLD if bold else _FONT, size)


def _png(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


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
    img = Image.new("RGB", (720, 600), _BG)
    d = ImageDraw.Draw(img)
    title = f"{day.day} {_MON[day.month - 1]}"
    d.text((24, 22), title, font=_font(30, True), fill=_INK, anchor="lt")
    d.line([(24, 70), (696, 70)], fill=_MUTED, width=2)
    bottom = _draw_bars(d, rem, 24, 88, 696)
    _draw_ring(d, rem.consumed, 360, bottom + 128, 104)
    d.text((24, 568), "BurnFat · учёт КБЖУ", font=_font(16), fill=_GREY, anchor="lt")
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
        d.line([(gx, gy), (gx + 7, gy)], fill=(170, 170, 170), width=2)
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
    """Линия тренда веса по датам (точки + значения концов + дельта)."""
    img = Image.new("RGB", (720, 340), _BG)
    d = ImageDraw.Draw(img)
    d.text((24, 18), "Вес", font=_font(24, True), fill=_INK, anchor="lt")
    ml, mt, mb, mr = 24, 70, 46, 70
    plot_h, plot_w = 340 - mt - mb, 720 - ml - mr
    ws = [w for _, w in points]
    lo, hi = min(ws), max(ws)
    span = (hi - lo) or 1.0
    n = len(points)
    xs = [ml + (plot_w * i / max(1, n - 1)) for i in range(n)]
    ys = [mt + plot_h * (1 - (w - lo) / span) for w in ws]
    d.line(list(zip(xs, ys, strict=True)), fill=_COLORS[0], width=3, joint="curve")
    for x, y in zip(xs, ys, strict=True):
        d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=_COLORS[0])
    d.text((xs[0], ys[0] - 10), f"{ws[0]:g}", font=_font(16, True), fill=_INK, anchor="mb")
    d.text((xs[-1], ys[-1] - 10), f"{ws[-1]:g}", font=_font(16, True), fill=_INK, anchor="mb")
    delta = round(ws[-1] - ws[0], 1)
    sign = "+" if delta > 0 else ""
    d.text((720 - mr, 24), f"{sign}{delta} кг", font=_font(20, True),
           fill=_OVER if delta > 0 else _OK, anchor="rt")
    return _png(img)


def render_heatmap(days: list[tuple[date, float]], goal: float) -> bytes:
    """Календарь-хитмап: клетка на день, цвет = ккал/цель (серый-зелёный-красный)."""
    cell, gap, top, left = 56, 8, 80, 24
    weeks = (len(days) + 6) // 7
    img = Image.new("RGB", (left * 2 + 7 * (cell + gap), top + weeks * (cell + gap) + 20), _BG)
    d = ImageDraw.Draw(img)
    d.text((left, 18), "Дни в цели", font=_font(24, True), fill=_INK, anchor="lt")
    for i, wd in enumerate(_WD):
        wx = left + i * (cell + gap) + cell // 2
        d.text((wx, 56), wd, font=_font(15), fill=_GREY, anchor="mm")
    pad = days[0][0].weekday() if days else 0  # первая неделя по дню недели
    for idx, (day, kcal) in enumerate(days):
        slot = idx + pad
        x = left + (slot % 7) * (cell + gap)
        y = top + (slot // 7) * (cell + gap)
        if kcal <= 0:
            color = _MUTED
        else:
            r = kcal / goal if goal else 1.0
            color = _OVER if r > 1.1 else _OK if r >= 0.5 else (200, 224, 200)
        d.rounded_rectangle([x, y, x + cell, y + cell], radius=8, fill=color)
        d.text((x + cell // 2, y + cell // 2), str(day.day), font=_font(16),
               fill=_BG if kcal > 0 else _GREY, anchor="mm")
    return _png(img)
