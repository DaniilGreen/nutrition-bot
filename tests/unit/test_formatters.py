from __future__ import annotations

from app.application.results import DailyStats
from app.bot.formatters import _bar, _esc, _sparkline, format_result
from app.domain.goals import Goal, remaining
from app.domain.value_objects import Macros


def test_bar_fill_levels() -> None:
    assert _bar(0, 100, width=10) == "▱" * 10
    assert _bar(100, 100, width=10) == "▰" * 10
    assert _bar(50, 100, width=10) == "▰" * 5 + "▱" * 5
    assert _bar(200, 100, width=10) == "▰" * 10  # перебор — заполнен, не длиннее
    assert _bar(50, 0, width=10) == "▱" * 10  # нулевая цель безопасна


def test_escape_dynamic() -> None:
    assert _esc("M&M's") == "M&amp;M's"
    assert _esc("сок <Я>") == "сок &lt;Я&gt;"


def test_sparkline_levels() -> None:
    s = _sparkline([1, 2, 3])
    assert len(s) == 3
    assert s[0] == "▁" and s[-1] == "█"
    assert _sparkline([5, 5, 5]) == "▁▁▁"  # без разброса — ровная линия
    assert _sparkline([]) == ""


def test_daily_card_has_bar_and_remaining() -> None:
    rem = remaining(Macros(500, 40, 20, 50), Goal(2000, 150, 65, 250))
    text = format_result(DailyStats(remaining=rem, meals_count=2))
    assert "📊 Сегодня" in text
    assert "▰" in text or "▱" in text  # полоска стиля C
    assert "1500" in text
