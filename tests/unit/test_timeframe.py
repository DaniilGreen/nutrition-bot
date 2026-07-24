from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.domain.timeframe import (
    day_bounds_utc,
    reminder_due,
    tz_from_longitude,
    tz_from_utc_offset,
    week_bounds_utc,
)


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


def test_reminder_due_at_local_hour() -> None:
    # 18:00 UTC == 21:00 в Москве
    now = datetime(2026, 6, 18, 18, 0, tzinfo=timezone.utc)
    due, day = reminder_due(now, "Europe/Moscow", 21, None)
    assert due is True
    assert day == date(2026, 6, 18)


def test_reminder_not_due_if_already_sent_today() -> None:
    now = datetime(2026, 6, 18, 18, 0, tzinfo=timezone.utc)
    due, _ = reminder_due(now, "Europe/Moscow", 21, date(2026, 6, 18))
    assert due is False


def test_reminder_not_due_wrong_hour() -> None:
    now = datetime(2026, 6, 18, 17, 0, tzinfo=timezone.utc)  # 20:00 МСК
    due, _ = reminder_due(now, "Europe/Moscow", 21, None)
    assert due is False


def test_reminder_due_at_23_59_local() -> None:
    # 20:59 UTC == 23:59 в Москве — итог у самого конца дня
    now = datetime(2026, 6, 18, 20, 59, tzinfo=timezone.utc)
    due, day = reminder_due(now, "Europe/Moscow", 23, None, 59)
    assert due is True
    assert day == date(2026, 6, 18)  # день ещё сегодняшний, не съехал за полночь


def test_reminder_not_due_before_23_59() -> None:
    now = datetime(2026, 6, 18, 20, 58, tzinfo=timezone.utc)  # 23:58 МСК
    due, _ = reminder_due(now, "Europe/Moscow", 23, None, 59)
    assert due is False


def test_tz_from_longitude_sign_inversion() -> None:
    # Москва (lon≈37.6) → восток → UTC+3; POSIX-инверсия: имя Etc/GMT-3
    assert tz_from_longitude(37.6) == "Etc/GMT-3"
    now = datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc)
    moscow = now.astimezone(ZoneInfo("Europe/Moscow"))
    derived = now.astimezone(ZoneInfo(tz_from_longitude(37.6)))
    assert derived.utcoffset() == moscow.utcoffset()  # оба UTC+3


def test_tz_from_longitude_west_and_zero() -> None:
    assert tz_from_longitude(-75) == "Etc/GMT+5"  # восточное побережье США UTC-5
    assert tz_from_longitude(0) == "UTC"
    assert tz_from_longitude(5) == "UTC"  # round(5/15)=0


def test_tz_from_utc_offset_vietnam() -> None:
    # Вьетнам UTC+7: имя Etc/GMT-7, по факту даёт +7 часов
    assert tz_from_utc_offset(7) == "Etc/GMT-7"
    now = datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc)
    local = now.astimezone(ZoneInfo(tz_from_utc_offset(7)))
    assert local.utcoffset() == timedelta(hours=7)
    assert tz_from_utc_offset(-5) == "Etc/GMT+5"
    assert tz_from_utc_offset(0) == "UTC"
