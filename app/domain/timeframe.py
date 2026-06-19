"""Границы суток/недели в таймзоне пользователя → диапазон в UTC для запросов к БД.

Считаем «день» по локальному времени пользователя с первого дня — иначе границы
суток поедут при любой смене сервера/юзера.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_UTC = ZoneInfo("UTC")


def day_bounds_utc(now_utc: datetime, tzname: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tzname)
    local = now_utc.astimezone(tz)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(_UTC), end_local.astimezone(_UTC)


def week_bounds_utc(now_utc: datetime, tzname: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tzname)
    local = now_utc.astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = midnight - timedelta(days=midnight.weekday())  # понедельник
    end_local = start_local + timedelta(days=7)
    return start_local.astimezone(_UTC), end_local.astimezone(_UTC)


def tz_from_utc_offset(hours: int) -> str:
    """UTC-офсет в часах (+7 = Вьетнам) → имя зоны. POSIX-инверсия знака: +7 → Etc/GMT-7.

    Может вернуть невалидное имя при |hours|>14 — вызывающий валидирует через ZoneInfo.
    """
    if hours == 0:
        return "UTC"
    sign = "-" if hours > 0 else "+"  # инверсия: восток (UTC+) → Etc/GMT-
    return f"Etc/GMT{sign}{abs(hours)}"


def tz_from_longitude(lon: float) -> str:
    """Грубая таймзона из долготы (📍-локация Telegram): солнечный офсет round(lon/15) → Etc/GMT∓N.

    Политические границы TZ игнорируем — для границ суток/напоминаний солнечного приближения хватает.
    ponytail: round(lon/15); нужна точная TZ по точке — timezonefinder/tzfpy.
    """
    return tz_from_utc_offset(round(lon / 15))


def reminder_due(
    now_utc: datetime, tzname: str, hour: int, last_sent: date | None
) -> tuple[bool, date]:
    """Пора ли слать вечернее напоминание: совпал локальный час и сегодня ещё не слали.

    Возвращает (надо_ли, локальная_дата) — дату используем как ключ дедупа.
    """
    local = now_utc.astimezone(ZoneInfo(tzname))
    today = local.date()
    return (local.hour == hour and last_sent != today, today)
