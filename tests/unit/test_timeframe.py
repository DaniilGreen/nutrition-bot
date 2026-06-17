from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.timeframe import day_bounds_utc, week_bounds_utc


def test_day_bounds_span_24h() -> None:
    now = datetime(2026, 6, 18, 9, 30, tzinfo=timezone.utc)
    start, end = day_bounds_utc(now, "UTC")
    assert end - start == timedelta(days=1)
    assert start <= now < end
    assert start.hour == 0


def test_day_bounds_moscow_offset() -> None:
    # Москва UTC+3: локальная полночь == 21:00 UTC предыдущего дня
    now = datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc)
    start, _ = day_bounds_utc(now, "Europe/Moscow")
    assert start.hour == 21


def test_week_bounds_span_7d() -> None:
    now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    start, end = week_bounds_utc(now, "UTC")
    assert end - start == timedelta(days=7)
    assert start <= now < end
