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

from aiogram.types import InlineKeyboardMarkup

from app.application.entities import Meal, Product
from app.application.results import DailyStats, DayPace
from app.bot.ui.context import est_share
from app.bot.ui.kit import MON, WD
from app.domain.goals import Goal
from app.domain.nutrition import macros_for_grams
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


def frequent_data(
    markup: InlineKeyboardMarkup, products: dict[int, Product], sets: dict[str, Meal]
) -> str:
    """`chat_data` виджета «⚡ Частое» (FR-49.2): ровно плитки клавиатуры FR-40 — тот же топ,
    те же подписи и граммы, что у кнопок (`frequent_keyboard`), плюс ккал порции.

    Формат v1: {"v":1,"t":[[подпись, хвост, операция, ккал, ≈ 0/1], …]}; операция — `p:<id>:<г>`
    или `s:<ключ набора>`, она же уходит в запрос `w:<операция>:<ключ>`. Ккал продукта —
    граммы × запиннутая плотность (INV-2), набора — его записанный итог (INV-3)."""
    tiles = []
    for row in markup.inline_keyboard:
        for b in row:
            op = (b.callback_data or "").split(":", 1)[-1]
            label, _, tail = b.text.rpartition(" · ")
            parts = op.split(":")
            kcal, est = 0, 0
            if parts[0] == "p" and len(parts) == 3:
                prod = products.get(int(parts[1]))
                if prod is not None:
                    kcal = round(macros_for_grams(prod.density, float(parts[2])).kcal)
                    est = int(prod.is_estimate)
            elif parts[0] == "s" and len(parts) == 2 and parts[1] in sets:
                m = sets[parts[1]]
                kcal = round(m.totals.kcal)
                est = int(any(i.is_estimate for i in m.items))
            tiles.append([label.removeprefix("📌 "), tail, op, kcal, est])
    text = _dump({"v": 1, "t": tiles})
    while size(text) > LIMIT and tiles:  # ponytail: 8 плиток × короткие подписи влезают с запасом
        tiles.pop()
        text = _dump({"v": 1, "t": tiles})
    return text


def weight_data(points: list[tuple[datetime, float]], tz: str, target: str) -> str | None:
    """`chat_data` виджета «⚖️ Вес» (FR-49.3): записанные замеры как есть (D-40).

    Формат v1: "p" — [[дни от первого замера, кг], …] по настоящему времени; "a0" — ISO-дата
    первого замера и "h0" — её час (даты точек JS считает от них); "a"/"z" — подписи краёв
    оси; "l" — последний;
    "dp" — Δ за период; "dm" — Δ за 4 недели или null; "st" — статус Δ (ok/bad/mute, MA-INV-20).
    Все разности — `weight_change`, как в тексте экрана: числа не разъедутся. None — замеров
    меньше двух (график не нужен, FR-8)."""
    from app.domain.weight_change import weight_change

    zone = ZoneInfo(tz)
    pts = list(points)
    while len(pts) >= 2:
        ch = weight_change(pts)
        assert ch is not None
        t0 = pts[0][0]
        d0 = t0.astimezone(zone).date()
        first, last = d0, pts[-1][0].astimezone(zone).date()
        status = (
            "mute" if target == "maintain" or abs(ch.period_kg) < 0.05
            else "ok" if (ch.period_kg < 0) == (target != "bulk") else "bad"
        )
        data = {
            "v": 1,
            "a0": d0.isoformat(),
            "h0": round(t0.astimezone(zone).hour + t0.astimezone(zone).minute / 60, 1),
            "a": f"{first.day} {MON[first.month - 1]}",
            "z": f"{last.day} {MON[last.month - 1]}",
            "p": [[round((t - t0).total_seconds() / 86400, 1), round(w, 1)] for t, w in pts],
            "l": round(ch.last_kg, 1),
            "dp": ch.period_kg,
            "dm": ch.month_kg,
            "st": status,
        }
        text = _dump(data)
        if size(text) <= LIMIT:
            return text
        pts = pts[1:]  # старейший замер уходит; Δ «за период» — по показанным, как и было
    return None
