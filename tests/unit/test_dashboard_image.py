"""График веса (FR-8, D-40): валидный PNG, ось X по времени, только записанные значения."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from PIL import Image

from app.bot.dashboard_image import kg_axis, render_weight_chart, stats, x_positions

_PNG = b"\x89PNG\r\n\x1a\n"
_D = datetime(2026, 6, 1, 7, tzinfo=UTC)


def _ok(png: bytes) -> None:
    assert png[:8] == _PNG
    assert Image.open(BytesIO(png)).size == (1440, 1080)  # ×2 — retina не мылит


def test_render_weight_chart_up_and_down() -> None:
    pts = [(_D + timedelta(days=i), 86.0 + d) for i, d in enumerate([0, -0.4, -0.2, -0.9, -1.3])]
    _ok(render_weight_chart(pts, tz="Europe/Moscow", target="cut"))


@pytest.mark.parametrize(
    "pts",
    [
        [(_D, 80.0), (_D + timedelta(days=1), 80.0)],  # ровный вес: span=0 → не делим на ноль
        [(_D, 80.0)],  # одна точка — не падаем (экран её не шлёт, но функция устойчива)
        [(_D, 80.0), (_D + timedelta(hours=1), 80.4)],  # два замера в одно утро
        [(_D, 84.0), (_D + timedelta(days=2), 83.6), (_D + timedelta(days=3), 83.9)],  # три точки
        [(_D, 90.0), (_D + timedelta(days=1), 89.5), (_D + timedelta(days=60), 85.0)],  # пропуск
        [(_D + timedelta(days=i), 85.0 - i * 0.1) for i in range(60)],  # 60 замеров
    ],
    ids=["flat", "one", "same-morning", "three", "big-gap", "sixty"],
)
def test_render_survives_edge_cases(pts: list[tuple[datetime, float]]) -> None:
    _ok(render_weight_chart(pts, tz="Europe/Moscow", target="cut"))


def test_naive_datetimes_still_render() -> None:
    """Вызов без tz и с наивным временем не должен падать."""
    _ok(render_weight_chart([(datetime(2026, 6, 1), 86.0), (datetime(2026, 6, 9), 85.1)]))


def test_x_is_time_scaled_not_index_spaced() -> None:
    """AC-231: замеры 1, 2 и 30 числа — промежуток 2→30 в 28 раз длиннее, чем 1→2.

    Старый PNG ставил их равным шагом, и месяц без весов выглядел как один день.
    """
    t = [_D, _D + timedelta(days=1), _D + timedelta(days=29)]
    x = x_positions(t, 0, 1000)
    assert (x[2] - x[1]) == pytest.approx(28 * (x[1] - x[0]))
    assert 0 < x[0] < x[1] < x[2] < 1000  # отступ по краям: крайние точки не режет рамкой


def test_kg_axis_round_and_covers_data() -> None:
    lo, hi, step = kg_axis(83.1, 87.3)
    assert lo <= 83.1 and hi >= 87.3 and step in (0.5, 1, 2)
    assert (hi - lo) / step <= 6
    lo, hi, _ = kg_axis(80.0, 80.0)  # ровный вес — видимый коридор, а не ноль
    assert hi > lo


def test_stats_are_raw_differences_with_comma() -> None:
    """AC-229: внизу — «последний замер», «за период», «за 4 недели»; запятая, без «тренда»."""
    pts = [(_D, 86.8), (_D + timedelta(days=10), 86.9), (_D + timedelta(days=28), 85.2)]
    got = [(label, text) for label, text, _ in stats(pts)]
    assert got == [
        ("последний замер", "85,2 кг"),
        ("за период", "−1,6 кг"),
        ("за 4 недели", "−1,6 кг"),
    ]


def test_stats_without_month_history() -> None:
    """AC-230: самый ранний замер 20 дней назад — «за 4 недели» не показываем."""
    pts = [(_D, 86.0), (_D + timedelta(days=20), 85.0)]
    assert [label for label, _, _ in stats(pts)] == ["последний замер", "за период"]
