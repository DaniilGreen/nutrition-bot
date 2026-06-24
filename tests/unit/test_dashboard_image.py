"""Картинки дашбордов/графиков: каждый рендер возвращает валидный PNG (без дисплея/сети)."""
from __future__ import annotations

from datetime import date, datetime

from app.bot.dashboard_image import (
    render_dashboard,
    render_day_card,
    render_heatmap,
    render_today,
    render_weekly_chart,
    render_weight_chart,
)
from app.domain.goals import Goal, remaining
from app.domain.value_objects import Macros

_PNG = b"\x89PNG\r\n\x1a\n"


def _rem(kcal: float = 605, p: float = 32, f: float = 25, c: float = 98) -> object:
    return remaining(Macros(kcal, p, f, c), Goal(2200, 165, 61, 248))


def test_render_dashboard_returns_png() -> None:
    png = render_dashboard(_rem())
    assert png[:8] == _PNG and len(png) > 1000


def test_render_dashboard_handles_overflow_and_zero_goal() -> None:
    # перебор (>100%) и нулевая цель не должны падать
    rem = remaining(Macros(3000, 200, 90, 0), Goal(2000, 150, 65, 0))
    assert render_dashboard(rem)[:8] == _PNG


def test_render_today_bars_and_ring() -> None:
    assert render_today(_rem())[:8] == _PNG


def test_render_today_zero_consumed_ring() -> None:
    # кольцо при нулевом потреблении (total=0) — серый круг, без деления на ноль
    assert render_today(_rem(0, 0, 0, 0))[:8] == _PNG


def test_render_day_card() -> None:
    assert render_day_card(_rem(), date(2026, 6, 24))[:8] == _PNG


def test_render_weekly_chart() -> None:
    days = [(date(2026, 6, 18 + i), k) for i, k in enumerate([1800, 0, 2400, 2100, 0, 2600, 1500])]
    assert render_weekly_chart(days, 2000)[:8] == _PNG


def test_render_weekly_chart_zero_goal_no_crash() -> None:
    days = [(date(2026, 6, 18 + i), 0.0) for i in range(7)]
    assert render_weekly_chart(days, 0)[:8] == _PNG


def test_render_weight_chart_up_and_down() -> None:
    pts = [(datetime(2026, 6, 1 + i), 86.0 + d) for i, d in enumerate([0, -0.4, -0.2, -0.9, -1.3])]
    assert render_weight_chart(pts)[:8] == _PNG
    flat = [(datetime(2026, 6, 1), 80.0), (datetime(2026, 6, 2), 80.0)]  # span=0 → не делим на ноль
    assert render_weight_chart(flat)[:8] == _PNG


def test_render_heatmap_28_days_with_pad() -> None:
    # старт в среду (pad=2): первая неделя смещена, нули серым, перебор красным
    days = [(date(2026, 6, 3 + i), float((i * 311) % 2600)) for i in range(28)]
    assert render_heatmap(days, 2000)[:8] == _PNG
