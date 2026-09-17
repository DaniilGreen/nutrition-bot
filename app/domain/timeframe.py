"""Границы суток/недели в таймзоне пользователя → диапазон в UTC для запросов к БД.

Считаем «день» по локальному времени пользователя с первого дня — иначе границы
суток поедут при любой смене сервера/юзера.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_UTC = ZoneInfo("UTC")


def local_date(moment: datetime, tzname: str, day_start: int = 0) -> date:
    """К какому дню человека относится момент. При `day_start=4` перекус в 00:30
    — это ещё вчера (§6.5). Единственное место, где считается «чей это день»."""
    return (moment.astimezone(ZoneInfo(tzname)) - timedelta(hours=day_start)).date()


def _day_start_local(now_utc: datetime, tzname: str, day_start: int) -> datetime:
    tz = ZoneInfo(tzname)
    day = local_date(now_utc, tzname, day_start)
    # ponytail: сутки на переходе летнего времени считаются как 24 ч; для
    # Etc/GMT±N (наш случай) перехода нет вовсе.
    return datetime(day.year, day.month, day.day, day_start, tzinfo=tz)


def day_bounds_utc(
    now_utc: datetime, tzname: str, day_start: int = 0
) -> tuple[datetime, datetime]:
    start_local = _day_start_local(now_utc, tzname, day_start)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(_UTC), end_local.astimezone(_UTC)


def week_bounds_utc(
    now_utc: datetime, tzname: str, day_start: int = 0
) -> tuple[datetime, datetime]:
    first = _day_start_local(now_utc, tzname, day_start)
    start_local = first - timedelta(days=first.weekday())  # понедельник
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
    now_utc: datetime, tzname: str, hour: int, last_sent: date | None, minute: int = 0
) -> tuple[bool, date]:
    """Пора ли слать вечерний итог: локально дошло до HH:MM и сегодня ещё не слали.

    Порог `>=`, а не «ровно час»: в минутное окно 23:59 иначе не попасть — промах тика означал
    бы, что итога за день нет вообще. Дедуп по локальной дате не даёт слать дважды.
    Возвращает (надо_ли, локальная_дата) — дата и есть ключ дедупа.
    """
    local = now_utc.astimezone(ZoneInfo(tzname))
    today = local.date()
    return ((local.hour, local.minute) >= (hour, minute) and last_sent != today, today)
