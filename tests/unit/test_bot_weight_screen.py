"""«⚖️ Вес» — только записанные значения (FR-8, D-40, AC-229, AC-230)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.application.entities import WeightLog
from app.application.results import DailyStats, MonthReport, WeightTrend
from app.bot.ui import screens
from app.bot.ui.context import DayContext
from app.bot.ui.kit import SAFE, lines_of, text_width
from app.domain.goals import Goal, remaining
from app.domain.value_objects import Macros
from app.domain.weight_change import demo, weight_change

GOAL = Goal(2200, 165, 61, 248, "cut")
NOW = datetime.now(UTC).replace(microsecond=0)


def _ctx(points: list[tuple[int, float]]) -> DayContext:
    """points — (дней назад, кг)."""
    logs = [WeightLog(1, NOW - timedelta(days=a), w) for a, w in sorted(points)]  # новые первыми
    empty = MonthReport(7, 0, 0, Macros(0, 0, 0, 0), None)
    return DayContext(
        today=DailyStats(remaining(Macros(0, 0, 0, 0), GOAL), 0, [], "Europe/Moscow"),
        days=[(date(2026, 10, 19) + timedelta(days=i), 0.0) for i in range(7)],
        goal=GOAL, week7=empty, month=empty,
        weight=WeightTrend(logs, "Europe/Moscow"), products=[],
        weight_kg=logs[0].weight_kg if logs else None, local_hour=12, est_kcal_share=0,
    )


def test_domain_demo() -> None:
    demo()


def test_screen_shows_recorded_values_only() -> None:
    """AC-229: последний замер как записан, разности с запятой; «тренда» нет нигде."""
    html = screens.weight(_ctx([(28, 86.8), (14, 86.1), (3, 85.9), (0, 85.2)]))
    assert "85,2 кг</b> · последний замер, сегодня" in html
    assert "−1,6 кг" in html and "за 4 недели −1,6 кг" in html
    low = html.lower()
    assert "тренд" not in low and "без скачков" not in low and "темп" not in low
    assert "<b>86,8</b> кг" in html  # список замеров — тоже с запятой


def test_no_month_number_without_month_of_history() -> None:
    """AC-230: самый ранний замер 20 дней назад — «за 4 недели» не показываем."""
    html = screens.weight(_ctx([(20, 86.0), (0, 85.0)]))
    assert "за 4 недели" not in html and "−1,0 кг" in html


def test_single_weigh_in() -> None:
    html = screens.weight(_ctx([(5, 85.2)]))
    assert "85,2 кг</b> · последний замер, 5 дн. назад" in html
    assert "<details>" not in html  # списка из одного замера нет


def test_no_weigh_ins() -> None:
    assert "Вес ещё не записан" in screens.weight(_ctx([]))


def test_lines_fit() -> None:
    html = screens.weight(_ctx([(40, 101.4), (28, 100.0), (0, 99.9)]))
    assert all(text_width(ln) <= SAFE for ln in lines_of(html))


def test_month_change_picks_closest_to_28_days() -> None:
    d = datetime(2026, 9, 1, tzinfo=UTC)
    pts = [(d, 90.0), (d + timedelta(days=5), 89.0), (d + timedelta(days=33), 88.0)]
    ch = weight_change(pts)
    assert ch is not None and ch.month_kg == -1.0  # сравнили с замером 5-го дня (ближе к −28)
    assert ch.period_kg == -2.0
