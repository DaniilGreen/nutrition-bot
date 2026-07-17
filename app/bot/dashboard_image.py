"""Единственный уцелевший PNG — линия тренда веса.

Всё остальное (бары/дашборды/хитмап) переехало на Rich-текст (D-31, SPEC §14). Линию текстом
нарисовать нельзя, поэтому вес остаётся картинкой — и то шлётся, только когда тренд можно честно
показать (см. `DayContext.trend_ok`). Рисуется ЛОКАЛЬНО (Pillow), без OpenAI — ~мс, ~десятки КБ.

КАЧЕСТВО (D-33; техника из finance_bot, где владелец и диагностировал причину мыла):
* PNG отдаётся в 2× (1440×720). Telegram на retina растягивает 720px до ~1100+ физических, и
  картинка мылится. Текст рисуем сразу в 2× — он чёткий, а не растянутый.
* Линия и точки — со СУПЕРСЭМПЛИНГОМ ×_SS и ужатием LANCZOS: Pillow не сглаживает line/ellipse,
  а линия веса диагональная и без этого идёт ступеньками. Ось X сглаживать не надо — у
  горизонтали нет косых краёв, она рисуется прямо в 2×.
"""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

# тёмная тема, палитра «Минимал»: линия — василёк; ось — приглушённая; дельта — зелёная/красная.
_LINE = (124, 156, 224)
_OK, _OVER = (92, 184, 122), (232, 115, 111)  # снижение / рост веса
_MUTED, _BG = (52, 57, 66), (28, 31, 38)
_INK, _GREY = (237, 239, 243), (151, 157, 166)

_SCALE = 2  # выход в 2× — иначе retina растягивает 720px и мылит
_SS = 3  # суперсэмплинг слоя линии: Pillow не сглаживает диагонали

_W, _H = 720, 360  # логическая сетка; реальный PNG в _SCALE раз крупнее
_ML, _MT, _MB, _MR = 24, 70, 64, 70
_INSET = 6  # чтобы точки на краях не срезало границей слоя

# Кириллица обязана рисоваться: DejaVu (Linux/Docker) → Arial (macOS-dev) → дефолт Pillow.
_FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial.ttf")
_FONTS_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
)


@lru_cache(maxsize=16)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Шрифт логического кегля: сам домножает на _SCALE — вызывающий про 2× не думает."""
    size *= _SCALE
    for path in _FONTS_BOLD if bold else _FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:  # Pillow ≥10 понимает размер у дефолтного шрифта
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _png(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _delta_str(delta: float | None) -> str:
    if delta is None:
        return "—"
    return f"{'+' if delta > 0 else ''}{delta:g} кг"


def _plot_layer(xs: list[float], ys: list[float], w: int, h: int) -> Image.Image:
    """Слой линии и точек: рисуем в ×_SS крупнее и ужимаем LANCZOS.

    Только так у диагонали пропадают ступеньки — Pillow line/ellipse не сглаживает.
    """
    k = _SCALE * _SS
    layer = Image.new("RGB", (w * k, h * k), _BG)
    d = ImageDraw.Draw(layer)
    pts = [(x * k, y * k) for x, y in zip(xs, ys, strict=True)]
    if len(pts) > 1:
        d.line(pts, fill=_LINE, width=3 * k, joint="curve")
    r = 4 * k
    for x, y in pts:
        d.ellipse([x - r, y - r, x + r, y + r], fill=_LINE)
    return layer.resize((w * _SCALE, h * _SCALE), Image.Resampling.LANCZOS)


def render_weight_chart(points: list[tuple[datetime, float]]) -> bytes:
    """Линия тренда веса: точки, ДАТЫ по оси X, значения концов и дельта за период."""
    img = Image.new("RGB", (_W * _SCALE, _H * _SCALE), _BG)
    d = ImageDraw.Draw(img)
    d.text((_ML * _SCALE, 18 * _SCALE), "Вес", font=_font(24, True), fill=_INK, anchor="lt")

    plot_w, plot_h = _W - _ML - _MR, _H - _MT - _MB
    ws = [w for _, w in points]
    lo, hi = min(ws), max(ws)
    span = (hi - lo) or 1.0
    n = len(points)
    # Логические координаты внутри слоя. Отступ по ОБЕИМ осям: без него крайние точки
    # упираются в границу слоя и срезаются ровно пополам (первая и последняя).
    xs = [_INSET + (plot_w - 2 * _INSET) * i / max(1, n - 1) for i in range(n)]
    ys = [_INSET + (plot_h - 2 * _INSET) * (1 - (w - lo) / span) for w in ws]

    img.paste(_plot_layer(xs, ys, plot_w, plot_h), (_ML * _SCALE, _MT * _SCALE))

    base_y = (_MT + plot_h) * _SCALE
    d.line(
        [(_ML * _SCALE, base_y), ((_ML + plot_w) * _SCALE, base_y)], fill=_MUTED, width=2 * _SCALE
    )

    # Подписи концов — поверх слоя, сразу в 2× (чёткие).
    for i in (0, n - 1):
        d.text(
            ((_ML + xs[i]) * _SCALE, (_MT + ys[i]) * _SCALE - 12 * _SCALE),
            f"{ws[i]:g}", font=_font(16, True), fill=_INK, anchor="mb",
        )
    step = 1 if n <= 8 else (n + 6) // 7  # не чаще ~7 подписей дат, чтобы не слипались
    for i, (dt, _w) in enumerate(points):
        if i % step == 0 or i == n - 1:
            d.text(
                ((_ML + xs[i]) * _SCALE, base_y + 10 * _SCALE),
                f"{dt.day}.{dt.month:02d}", font=_font(14), fill=_GREY, anchor="ma",
            )
    delta = round(ws[-1] - ws[0], 1)
    d.text(
        ((_W - _MR) * _SCALE, 24 * _SCALE), _delta_str(delta), font=_font(20, True),
        fill=_OVER if delta > 0 else _OK, anchor="rt",
    )
    return _png(img)
