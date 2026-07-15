"""Единственный уцелевший PNG — линия веса: валидный PNG, без падений на краевых точках."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.bot.dashboard_image import render_weight_chart

_PNG = b"\x89PNG\r\n\x1a\n"


def test_render_weight_chart_up_and_down() -> None:
    pts = [(datetime(2026, 6, 1 + i), 86.0 + d) for i, d in enumerate([0, -0.4, -0.2, -0.9, -1.3])]
    png = render_weight_chart(pts)
    assert png[:8] == _PNG and len(png) > 1000


def test_render_weight_chart_flat_no_zero_division() -> None:
    flat = [(datetime(2026, 6, 1), 80.0), (datetime(2026, 6, 2), 80.0)]  # span=0 → не делим на ноль
    assert render_weight_chart(flat)[:8] == _PNG


def test_render_weight_chart_many_points_thins_dates() -> None:
    pts = [(datetime(2026, 6, 1) + timedelta(days=i), 85.0 - i * 0.1) for i in range(20)]
    assert render_weight_chart(pts)[:8] == _PNG
