"""Данные HTML-виджетов (FR-49): `chat_data` и подписи из того же `gather()`, что rich-экраны.

Шаблон виджета статичен (`app/bot/widgets/<имя>.html`), всё конкретное — здесь, JSON ≤ 1024
байт (W-INV-3). Числа посчитаны Python'ом (W-INV-1): JS их только рисует.

Формат `today` (v1), ключи короткие — бюджет 1024 байта UTF-8, а кириллица по два байта:
  d   ISO-дата дня (ключ запроса `t:<d>`)        dl  «вт, 10 ноя»       at  «14:20» — свежесть
  sh  час начала суток человека                   hn  индекс текущего часа от `sh`
  g   цель [ккал, Б, Ж, У]                        e   съедено [ккал, Б, Ж, У]
  l   осталось [ккал, Б] (`Remaining`, со знаком)  pk  белок на кг веса (нет веса — нет ключа)
  w   последний вес, кг (или нет ключа)           es  % ккал оценкой (≈, §3.5 MA)
  s   серия [дней, заморозка недели 0/1] (D-44)   n   норма по часам от `sh` (нет — MA-INV-37)
  h   [[индекс часа, ккал]] — все часы с записями (никогда не режутся)
  it  [[индекс часа, «мм», имя, ккал, ≈ 0/1]] — записи, свежие первыми (режутся с хвоста)
  x   сколько записей не влезло
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.application.entities import Meal
from app.application.results import DailyStats, DayPace
from app.bot.ui.context import est_share
from app.bot.ui.kit import MON, WD
from app.domain.goals import Goal
from app.domain.streak import Streak
from app.domain.timeframe import local_date

LIMIT = 1024  # байт UTF-8 на chat_data / ответ запроса (Bot API 10.4)
NAME_STEPS = (22, 16, 12)  # имя записи режется по ступеням, прежде чем выкидывать записи


def _name(meal: Meal, limit: int) -> str:
    """Имя записи — то, что записано (MA-INV-64): позиции через запятую, больше двух — «+N»."""
    names = [i.name_snapshot.strip() for i in meal.items] or [meal.raw_input.strip()]
    text = ", ".join(names[:2]) + (f" +{len(names) - 2}" if len(names) > 2 else "")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def size(text: str) -> int:
    return len(text.encode())


def today_data(
    stats: DailyStats, goal: Goal, weight_kg: float | None, pace: DayPace | None,
    streak: Streak | None, now: datetime, start_h: int,
) -> str:
    """`chat_data` виджета «Сегодня» (FR-49.1). Всегда ≤ LIMIT байт.

    Данные — те же сервисы, что у rich-экрана (`assistant.today`, цель, вес, темп): одна правда
    (MA-INV-1). Полный `gather()` здесь не нужен — он считает неделю и месяц, а виджет
    пересобирается после каждого апдейта человека (D-46)."""
    tz = ZoneInfo(stats.timezone)
    local = now.astimezone(tz)
    day = local_date(now, stats.timezone, start_h)
    c, g = stats.remaining.consumed, goal

    def idx(dt: datetime) -> int:
        return (dt.astimezone(tz).hour - start_h) % 24

    meals = sorted(stats.meals, key=lambda m: m.eaten_at, reverse=True)
    hours: dict[int, float] = {}
    for m in meals:
        hours[idx(m.eaten_at)] = hours.get(idx(m.eaten_at), 0.0) + m.totals.kcal
    base: dict[str, Any] = {
        "v": 1,
        "d": day.isoformat(),
        "dl": f"{WD[day.weekday()].lower()}, {day.day} {MON[day.month - 1]}",
        "at": local.strftime("%H:%M"),
        "sh": start_h,
        "hn": (local.hour - start_h) % 24,
        "g": [round(g.daily_kcal), round(g.protein_g), round(g.fat_g), round(g.carb_g)],
        "e": [round(c.kcal), round(c.protein), round(c.fat), round(c.carb)],
        # Остатки — тем же `Remaining`, что у rich-экрана и подписи: JS их не вычитает (W-INV-1).
        "l": [round(stats.remaining.kcal_left), round(stats.remaining.protein_left)],
        "es": est_share(stats),
        "h": [[h, round(k)] for h, k in sorted(hours.items())],
    }
    if weight_kg:
        base["w"] = round(weight_kg, 1)
        base["pk"] = round(c.protein / weight_kg, 1)
    if streak is not None and streak.days > 0:
        base["s"] = [streak.days, int(streak.frozen)]
    if pace is not None and pace.norm:
        base["n"] = [round(v) for v in pace.norm]

    text = ""
    for limit in NAME_STEPS:
        rows = [
            [idx(m.eaten_at), m.eaten_at.astimezone(tz).strftime("%M"), _name(m, limit),
             round(m.totals.kcal), int(any(i.is_estimate for i in m.items))]
            for m in meals
        ]
        while True:
            text = _dump({**base, "it": rows, "x": len(meals) - len(rows)})
            if size(text) <= LIMIT or not rows:
                break
            rows = rows[:-1]  # выпадает самая старая запись; её час с суммой остаётся в `h`
        if len(rows) == len(meals):
            break
    if size(text) > LIMIT:  # ponytail: норма — первое, чем жертвуем (кривая не нарисуется)
        base.pop("n", None)
        text = _dump({**base, "it": [], "x": len(meals)})
    return text


def today_caption(stats: DailyStats, g: Goal) -> str:
    """Подпись (W-INV-2): главная цифра дня текстом — её видят список чатов и старые клиенты."""
    c = stats.remaining.consumed
    if not stats.meals:
        return f"📊 Сегодня: пока пусто · цель {round(g.daily_kcal)} ккал"
    left = round(stats.remaining.kcal_left)
    rest = f"осталось {left}" if left >= 0 else f"перебор {-left}"
    return (
        f"📊 Сегодня: {round(c.kcal)} из {round(g.daily_kcal)} ккал · {rest} · "
        f"белок {round(c.protein)} из {round(g.protein_g)} г"
    )
