from __future__ import annotations

from datetime import datetime

from app.application.entities import Meal, MealItem
from app.application.results import DailyStats, MealLogged
from app.bot.formatters import _bar, _esc, _sparkline, format_result
from app.domain.goals import Goal, remaining
from app.domain.value_objects import Macros


def test_unknown_reason_routing() -> None:
    from app.application.results import Unknown

    assert "20–400" in format_result(Unknown(reason="вес вне разумных пределов (20–400 кг)"))
    assert "не читаю фото" in format_result(Unknown(reason="этикетки недоступно (нет vision)"))
    assert "крупно" in format_result(Unknown(reason="не разобрал этикетку — сфоткай чётче"))
    g = format_result(Unknown(reason="не распознал приём пищи или вес"))  # генерик
    assert "📷 фото" in g and "🎤 голос" in g


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
    assert "<code>" in text  # выровненная сетка моноширинно
    assert "2000" in text  # цель ккал в строке грида (500/2000)


def test_meal_card_per_item_marks() -> None:
    # INV-13 / AC-41/42: «≈» на оценочной строке, «📋» на стейтед, каталог — без метки
    rem = remaining(Macros(1012, 99, 47, 44), Goal(2000, 150, 65, 250))
    meal = Meal(
        user_id=1,
        eaten_at=datetime(2026, 6, 18, 12, 0),  # _format_meal не использует eaten_at
        input_type="text",
        raw_input="x",
        source_confidence=0.9,
        items=[
            MealItem(
                "куриная грудка", 250, Macros(412, 77, 9, 0), product_id=1, is_estimate=False
            ),
            MealItem("том ям", 400, Macros(280, 12, 18, 14), product_id=2, is_estimate=True),
            MealItem(
                "салат из кафе", 0, Macros(320, 10, 20, 30), product_id=None, is_estimate=False
            ),
        ],
        totals=Macros(1012, 99, 47, 44),
    )
    text = format_result(MealLogged(meal=meal, remaining=rem))
    assert "≈ том ям" in text  # оценка
    assert "📋 салат из кафе" in text  # цифры юзера
    assert "• куриная грудка" in text  # каталог — без метки
    assert "≈ — прикинул сам" in text and "📋 — по твоим цифрам" in text  # сноски
