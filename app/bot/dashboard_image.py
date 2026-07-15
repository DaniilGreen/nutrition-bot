"""Единственный уцелевший PNG — линия тренда веса.

Всё остальное (бары/дашборды/хитмап) переехало на Rich-текст (D-31, SPEC §14). Линию текстом
нарисовать нельзя, поэтому вес остаётся картинкой — и то шлётся, только когда тренд можно честно
показать (см. `DayContext.trend_ok`). Рисуется ЛОКАЛЬНО (Pillow), без OpenAI — ~мс, ~десятки КБ.
"""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
# тёмная тема, палитра «Минимал»: линия — василёк; ось — приглушённая; дельта — зелёная/красная.
_LINE = (124, 156, 224)
_OK, _OVER = (92, 184, 122), (232, 115, 111)  # снижение / рост веса
_MUTED, _BG = (52, 57, 66), (28, 31, 38)
_INK, _GREY = (237, 239, 243), (151, 157, 166)


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
    d.line(list(zip(xs, ys, strict=True)), fill=_LINE, width=3, joint="curve")
    for x, y in zip(xs, ys, strict=True):
        d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=_LINE)
    d.text((xs[0], ys[0] - 10), f"{ws[0]:g}", font=_font(16, True), fill=_INK, anchor="mb")
    d.text((xs[-1], ys[-1] - 10), f"{ws[-1]:g}", font=_font(16, True), fill=_INK, anchor="mb")
    step = 1 if n <= 8 else (n + 6) // 7  # не чаще ~7 подписей дат, чтобы не слипались
    for i, (dt, _w) in enumerate(points):
        if i % step == 0 or i == n - 1:
            d.text(
                (xs[i], base + 10), f"{dt.day}.{dt.month:02d}", font=_font(14),
                fill=_GREY, anchor="ma",
            )
    delta = round(ws[-1] - ws[0], 1)
    d.text(
        (720 - mr, 24), _delta_str(delta), font=_font(20, True),
        fill=_OVER if delta > 0 else _OK, anchor="rt",
    )
    return _png(img)
