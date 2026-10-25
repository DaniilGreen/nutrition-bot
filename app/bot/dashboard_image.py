"""Единственный уцелевший PNG — график веса (FR-8, D-40).

Всё остальное (бары/дашборды/хитмап) переехало на Rich-текст (D-31, SPEC §14). Линию через замеры
текстом не нарисовать, поэтому вес — картинкой. Рисуется ЛОКАЛЬНО (Pillow), без OpenAI.

ЧТО НА ГРАФИКЕ (D-40, выбор владельца — «карточка»; «как записал, так и показывай»):
* линия через САМИ замеры, точка на каждом, мягкая заливка под линией — без сглаживания;
* ось X — НАСТОЯЩЕЕ время: старый PNG ставил точки равным шагом, и неделя без весов занимала
  столько же места, сколько два замера в одно утро — наклон врал там, где взвешивания неровные;
* ось Y — сетка в кг; подписи мин/макс; последняя точка выделена;
* шапка: «Вес», последний замер, плашка Δ за период; внизу три числа из `weight_change`.

КАЧЕСТВО: всё рисуется в ×6 и ужимается LANCZOS до ×2 (1440 px). ×2 — иначе retina растягивает
720 px и мылит; ×3 сверху — суперсэмплинг: Pillow не сглаживает диагонали и края кругов.
Палитра — токены аппки (web/app.css, тёмная тема). Кириллица — DejaVu (в образе), Arial на маке.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from functools import lru_cache
from io import BytesIO
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from app.domain.weight_change import kg, weight_change

# --- палитра: токены аппки (web/app.css) ---
_BG = (26, 28, 32)          # --surface: карточка
_GRID = (50, 54, 61)        # --surface-3
_AXIS = (110, 116, 127)     # --border
_INK = (255, 255, 255)
_MUTED = (167, 171, 178)    # --text-muted
_FAINT = (140, 144, 149)    # --text-faint
_LINE = (94, 154, 193)      # --accent
_PILL = (38, 41, 47)        # --surface-2
_OK, _BAD = (52, 199, 89), (255, 69, 58)  # --ok / --danger

_OUT, _SS = 2, 3
_K = _OUT * _SS
_W, _H = 720, 540            # логическая сетка; шрифты ≥ 15 — иначе на телефоне не прочесть
_X0, _X1, _Y0, _Y1 = 70.0, _W - 34.0, 124.0, _H - 156.0  # поле графика

_MON = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")

_FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
          "/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf")
_FONTS_BOLD = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
               "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf")

Point = tuple[datetime, float]


@lru_cache(maxsize=32)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Шрифт логического кегля: сам домножает на _K — вызывающий про масштаб не думает."""
    for path in _FONTS_BOLD if bold else _FONTS:
        try:
            return ImageFont.truetype(path, size * _K)
        except OSError:
            continue
    try:  # Pillow ≥10 понимает размер у дефолтного шрифта
        return ImageFont.load_default(size * _K)
    except TypeError:
        return ImageFont.load_default()


# ---------- чистая геометрия и подписи (тестируются без картинки) ----------


def x_positions(times: Sequence[datetime], x0: float = _X0, x1: float = _X1) -> list[float]:
    """x по НАСТОЯЩЕМУ времени, с отступом в полдня по краям (крайние точки не режет рамкой)."""
    t0, t1 = times[0] - timedelta(hours=12), times[-1] + timedelta(hours=12)
    span = (t1 - t0).total_seconds()
    return [x0 + (x1 - x0) * (t - t0).total_seconds() / span for t in times]


def kg_axis(lo: float, hi: float) -> tuple[float, float, float]:
    """Круглые границы и шаг сетки, ≤ 5 делений. Ровный вес — коридор в 1 кг, без деления на 0."""
    span = max(hi - lo, 1.0) + 0.6
    step = next((s for s in (0.2, 0.5, 1, 2, 5, 10, 20) if span / s <= 5), 50)
    return math.floor((lo - 0.3) / step) * step, math.ceil((hi + 0.3) / step) * step, step


def stats(points: Sequence[Point]) -> list[tuple[str, str, float | None]]:
    """Нижний ряд: (подпись, текст, Δ для цвета). «За 4 недели» — только если есть с чем (FR-8)."""
    ch = weight_change(list(points))
    if ch is None:
        return []
    out: list[tuple[str, str, float | None]] = [
        ("последний замер", f"{kg(ch.last_kg)} кг", None),
        ("за период", f"{kg(ch.period_kg, sign=True)} кг", ch.period_kg),
    ]
    if ch.month_kg is not None:
        out.append(("за 4 недели", f"{kg(ch.month_kg, sign=True)} кг", ch.month_kg))
    return out


def _good(delta: float, target: str) -> tuple[int, int, int]:
    """Цвет = статус (как в боте): к цели — зелёный, от неё — красный, на поддержании — серый."""
    if target == "maintain" or abs(delta) < 0.05:
        return _MUTED
    return _OK if (delta < 0) == (target != "bulk") else _BAD


def _dshort(d: date) -> str:
    return f"{d.day} {_MON[d.month - 1]}"


def _plural(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "замер"
    return "замера" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "замеров"


# ---------- холст ----------


class _Canvas:
    def __init__(self) -> None:
        self.img = Image.new("RGB", (_W * _K, _H * _K), _BG)
        self.d = ImageDraw.Draw(self.img, "RGBA")  # RGBA поверх RGB — полупрозрачные заливки

    def text(self, x: float, y: float, s: str, size: int, fill: tuple, bold: bool = False,
             anchor: str = "la") -> None:
        self.d.text((x * _K, y * _K), s, font=_font(size, bold), fill=fill, anchor=anchor)

    def line(self, pts: list[tuple[float, float]], fill: tuple, width: float) -> None:
        self.d.line([(x * _K, y * _K) for x, y in pts], fill=fill, width=round(width * _K),
                    joint="curve")

    def dot(self, x: float, y: float, r: float, fill: tuple, ring: tuple | None = None) -> None:
        for rad, col in ((r + 2.5, ring), (r, fill)):
            if col is not None:
                self.d.ellipse([(x - rad) * _K, (y - rad) * _K, (x + rad) * _K, (y + rad) * _K],
                               fill=col)

    def pill(self, x: float, y: float, s: str, fg: tuple, bg: tuple = _PILL, size: int = 17,
             anchor: str = "center") -> None:
        w = self.d.textlength(s, font=_font(size, True)) / _K + 16
        h = size + 12
        x0 = {"center": x - w / 2, "right": x - w, "left": x}[anchor]
        self.d.rounded_rectangle([x0 * _K, (y - h / 2) * _K, (x0 + w) * _K, (y + h / 2) * _K],
                                 radius=h / 2 * _K, fill=bg)
        self.text(x0 + w / 2, y, s, size, fg, True, anchor="mm")

    def png(self) -> bytes:
        buf = BytesIO()
        self.img.resize((_W * _OUT, _H * _OUT), Image.Resampling.LANCZOS).save(
            buf, format="PNG", optimize=True)
        return buf.getvalue()


def render_weight_chart(
    points: list[Point], *, tz: str | None = None, target: str = "maintain"
) -> bytes:
    """Карточка веса (FR-8, D-40). points — (время, кг), старые → новые, ≥ 1 точки.

    tz — часовой пояс человека для дат на оси (замер в 01:00 по Москве — его день, а не вчерашний
    по UTC); target — cut/maintain/bulk, чтобы покрасить Δ как статус.
    """
    zone = ZoneInfo(tz) if tz else None
    times = [t.astimezone(zone) if zone and t.tzinfo else t for t, _ in points]
    ws = [w for _, w in points]
    c = _Canvas()

    # шапка
    delta = round(ws[-1] - ws[0], 1)
    c.text(24, 22, "Вес", 28, _INK, bold=True)
    sub = f"{_dshort(times[0].date())} – {_dshort(times[-1].date())} · {len(ws)} {_plural(len(ws))}"
    c.text(24, 64, sub, 18, _MUTED)
    c.text(_W - 24, 20, f"{kg(ws[-1])} кг", 30, _INK, bold=True, anchor="ra")
    col = _good(delta, target)
    c.pill(_W - 24, 78, f"{kg(delta, sign=True)} кг", col, bg=(*col, 46), anchor="right")

    # ось кг
    lo, hi, step = kg_axis(min(ws), max(ws))

    def y(v: float) -> float:
        return _Y1 - (_Y1 - _Y0) * (v - lo) / (hi - lo)

    v = lo
    while v <= hi + 1e-9:
        c.line([(_X0, y(v)), (_X1, y(v))], _GRID, 1.2)
        c.text(_X0 - 10, y(v), f"{v:g}".replace(".", ","), 18, _FAINT, anchor="rm")
        v += step
    c.text(_X0 - 10, _Y0 - 22, "кг", 16, _FAINT, anchor="rm")
    c.line([(_X0, _Y1), (_X1, _Y1)], _AXIS, 1.5)

    # ось времени: метки по дням, при шаге от недели — по понедельникам (их глаз узнаёт)
    t0, t1 = times[0] - timedelta(hours=12), times[-1] + timedelta(hours=12)
    days = max(1, (t1 - t0).days)
    sd = next((s for s in (1, 2, 3, 7, 14, 28, 56) if days / s <= 5), 112)
    d = t0.date() + timedelta(days=1)
    if sd >= 7:
        d += timedelta(days=(7 - d.weekday()) % 7)
    while (tick := datetime(d.year, d.month, d.day, tzinfo=times[0].tzinfo)) <= t1:
        x = x_positions([t0 + timedelta(hours=12), tick, t1 - timedelta(hours=12)])[1]
        if _X0 + 10 < x < _X1 - 10:
            c.line([(x, _Y1), (x, _Y1 + 6)], _AXIS, 1.5)
            c.text(x, _Y1 + 12, _dshort(d), 17, _FAINT, anchor="ma")
        d += timedelta(days=sd)

    # заливка, линия, точки — по САМИМ замерам
    xy = [(x, y(w)) for x, w in zip(x_positions(times), ws, strict=True)]
    c.d.polygon([(px * _K, py * _K) for px, py in [*xy, (xy[-1][0], _Y1), (xy[0][0], _Y1)]],
                fill=(*_LINE, 30))
    if len(xy) > 1:
        c.line(xy, _LINE, 3)
    for px, py in xy:
        c.dot(px, py, 4, _LINE, ring=_BG)

    # мин/макс — не у самой последней точки: там подпись налезла бы на неё. Макс — над точкой,
    # мин — под ней; не влезает в поле (точка у края) — сбоку, а не поверх линии.
    i_max, i_min = ws.index(max(ws)), ws.index(min(ws))
    if i_max != i_min:
        for i, word, dy in ((i_max, "макс", -28), (i_min, "мин", 28)):
            px, py = xy[i]
            if xy[-1][0] - px < 130:
                continue
            text = f"{word} {kg(ws[i])}"
            if _Y0 <= py + dy <= _Y1 - 16:
                left = px < _X0 + 40
                c.pill(px - 6 if left else px, py + dy, text, _MUTED,
                       anchor="left" if left else "center")
            elif px < _X1 - 170:
                c.pill(px + 14, py, text, _MUTED, anchor="left")
            else:
                c.pill(px - 14, py, text, _MUTED, anchor="right")
    c.dot(*xy[-1], 7, _INK, ring=_LINE)

    # нижний ряд — ровно те же числа, что в тексте экрана (weight_change)
    y0 = _H - 90
    c.line([(24, y0 - 16), (_W - 24, y0 - 16)], _GRID, 1.2)
    colw = (_W - 48) / 3
    for i, (lab, val, dlt) in enumerate(stats(points)):
        c.text(24 + colw * i, y0, lab, 17, _FAINT)
        c.text(24 + colw * i, y0 + 26, val, 26, _INK if dlt is None else _good(dlt, target),
               bold=True)
    return c.png()
