"""Юнит-тест self-consistency (D-15): медиана+CV и маппинг уверенности."""
from __future__ import annotations

from app.domain.consensus import confidence_from_cv, median_cv, worst


def test_median_cv_stable_and_spread() -> None:
    assert median_cv([100, 100, 100]) == (100, 0.0)  # консенсус
    med, cv = median_cv([90, 100, 110])
    assert med == 100 and 0.05 < cv < 0.12  # лёгкий разброс
    med, cv = median_cv([100, 220, 340])
    assert med == 220 and cv > 0.35  # сильный разброс → воздержание


def test_median_cv_edge_cases() -> None:
    assert median_cv([]) == (0.0, 0.0)
    assert median_cv([150]) == (150, 0.0)  # один сэмпл → CV неопределён → 0
    assert median_cv([0, 0]) == (0.0, 0.0)  # mean 0 → не делим


def test_confidence_thresholds() -> None:
    assert confidence_from_cv(0.0) == "high"
    assert confidence_from_cv(0.15) == "high"
    assert confidence_from_cv(0.25) == "med"
    assert confidence_from_cv(0.5) == "low"


def test_worst_picks_least_confident() -> None:
    assert worst("high", "med") == "med"
    assert worst("high", "low", "med") == "low"
    assert worst(None, "high") == "high"
    assert worst(None, None) is None  # один сэмпл/легаси → нет уровня
