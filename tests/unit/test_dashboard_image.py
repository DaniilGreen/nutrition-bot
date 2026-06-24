"""Дашборд-картинка (E): рендер возвращает валидный PNG (без дисплея/сети)."""
from __future__ import annotations

from app.bot.dashboard_image import render_dashboard
from app.domain.goals import Goal, remaining
from app.domain.value_objects import Macros


def test_render_dashboard_returns_png() -> None:
    rem = remaining(Macros(605, 32, 25, 98), Goal(2200, 165, 61, 248))
    png = render_dashboard(rem)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # PNG-сигнатура
    assert len(png) > 1000


def test_render_dashboard_handles_overflow_and_zero_goal() -> None:
    # перебор (>100%) и нулевая цель не должны падать
    rem = remaining(Macros(3000, 200, 90, 0), Goal(2000, 150, 65, 0))
    png = render_dashboard(rem)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
