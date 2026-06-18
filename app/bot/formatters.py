"""Рендер Result -> HTML-текст ответа бота.

Динамика (имена продуктов от LLM, транскрипты) экранируется. Прогресс-бары и
спарклайн веса рисуются текстом (█░ / ▁▂▃) — псевдо-дашборд без зависимостей.
"""
from __future__ import annotations

from html import escape
from zoneinfo import ZoneInfo

from app.application.results import (
    ClarificationNeeded,
    CorrectionFailed,
    DailyStats,
    History,
    ItemRemoved,
    MealDeleted,
    MealLogged,
    Result,
    Unknown,
    VariantClarificationNeeded,
    WeeklyStats,
    WeightLogged,
    WeightTrend,
)
from app.domain.goals import Remaining

_LOW_CONFIDENCE = 0.6  # ниже — помечаем разбор как сомнительный
_SPARK = "▁▂▃▄▅▆▇█"


def format_result(result: Result) -> str:
    if isinstance(result, MealLogged):
        return _format_meal(result)
    if isinstance(result, ClarificationNeeded):
        return _format_clarification(result)
    if isinstance(result, VariantClarificationNeeded):
        return _format_variant_clarification(result)
    if isinstance(result, WeightLogged):
        return _format_weight(result)
    if isinstance(result, WeightTrend):
        return _format_weight_trend(result)
    if isinstance(result, DailyStats):
        return _format_stats(result.remaining, result.meals_count)
    if isinstance(result, WeeklyStats):
        return _format_weekly(result)
    if isinstance(result, History):
        return _format_history(result)
    if isinstance(result, MealDeleted):
        return _format_deleted(result)
    if isinstance(result, ItemRemoved):
        return _format_item_removed(result)
    if isinstance(result, CorrectionFailed):
        return _format_correction_failed(result)
    if isinstance(result, Unknown):
        return _format_unknown()
    return "Что-то пошло не так."


def _format_meal(r: MealLogged) -> str:
    lines = ["✅ Записал:"]
    for it in r.meal.items:
        m = it.macros
        lines.append(
            f"• {_esc(it.name_snapshot)} — {_n(it.grams)} г · {_n(m.kcal)} ккал · "
            f"Б{_n(m.protein)} Ж{_n(m.fat)} У{_n(m.carb)}"
        )
    t = r.meal.totals
    lines += [
        "",
        f"Итого: <b>{_n(t.kcal)} ккал</b> · Б{_n(t.protein)} Ж{_n(t.fat)} У{_n(t.carb)}",
        "",
        _remaining_block(r.remaining),
    ]
    notes = []
    if r.meal.estimated:
        notes.append("≈ часть цифр — AI-оценка. Неверно? /products")
    if r.meal.source_confidence < _LOW_CONFIDENCE:
        notes.append("⚠️ не уверен в распознавании — проверь")
    if notes:
        lines += ["", *notes]
    return "\n".join(lines)


def _format_clarification(r: ClarificationNeeded) -> str:
    return f"🤔 «{_esc(r.item_name)}» — какая порция?"


def _format_variant_clarification(r: VariantClarificationNeeded) -> str:
    return f"🤔 «{_esc(r.item_name)}» — уточни вариант (запомню как обычный):"


def _format_weight(r: WeightLogged) -> str:
    text = f"⚖️ Вес записан: <b>{_n(r.log.weight_kg)} кг</b>"
    if r.delta_kg is not None:
        sign = "+" if r.delta_kg > 0 else ""
        text += f" ({sign}{_n(r.delta_kg)} кг с прошлого раза)"
    return text


def _format_weight_trend(t: WeightTrend) -> str:
    if not t.logs:
        return "⚖️ Пока нет записей веса. Напиши: «вес 86.8»."
    newest = t.logs[0]
    text = f"⚖️ <b>{_n(newest.weight_kg)} кг</b>"
    if len(t.logs) >= 2:
        chrono = list(reversed(t.logs))  # старые → новые, чтобы спарклайн шёл по времени
        spark = _sparkline([log.weight_kg for log in chrono])
        delta = round(newest.weight_kg - chrono[0].weight_kg, 1)
        days = (newest.measured_at - chrono[0].measured_at).days
        sign = "+" if delta > 0 else ""
        text += f"\nΔ {sign}{_n(delta)} кг за {days} дн.  {spark}"
    text += f"\nЗаписей: {len(t.logs)} · обновить: «вес 86.5»"
    return text


def _format_stats(rem: Remaining, meals_count: int) -> str:
    return (
        f"📊 Сегодня · приёмов: {meals_count}\n\n"
        f"{_kcal_line(rem)}\n\n"
        f"{_macro_line('🥩 Б', rem.consumed.protein, rem.goal.protein_g)}\n"
        f"{_macro_line('🧈 Ж', rem.consumed.fat, rem.goal.fat_g)}\n"
        f"{_macro_line('🍞 У', rem.consumed.carb, rem.goal.carb_g)}"
    )


def _format_weekly(r: WeeklyStats) -> str:
    avg = round(r.consumed.kcal / r.days_logged) if r.days_logged else 0
    c = r.consumed
    return (
        "📈 За неделю\n\n"
        f"🔥 Всего {_n(c.kcal)} ккал · в среднем <b>{_n(avg)}/день</b> ({r.days_logged} дн.)\n"
        f"{_bar(avg, r.goal.daily_kcal)}  цель {_n(r.goal.daily_kcal)}\n\n"
        f"Б{_n(c.protein)} Ж{_n(c.fat)} У{_n(c.carb)} · приёмов: {r.meals_count}"
    )


def _format_history(r: History) -> str:
    if not r.meals:
        return "🗒 История пуста."
    tz = ZoneInfo(r.timezone)
    lines = ["🗒 Последние записи:"]
    for m in r.meals:
        when = m.eaten_at.astimezone(tz).strftime("%d.%m %H:%M")
        raw_names = ", ".join(i.name_snapshot for i in m.items) or "—"
        if len(raw_names) > 60:
            raw_names = raw_names[:59] + "…"  # ponytail: обрезка под лимит Telegram 4096
        lines.append(f"• {when} — {_n(m.totals.kcal)} ккал ({_esc(raw_names)})")
    return "\n".join(lines)


def _format_deleted(r: MealDeleted) -> str:
    if r.meal is None:
        return "Нечего удалять."
    names = _esc(", ".join(i.name_snapshot for i in r.meal.items) or "запись")
    return f"🗑 Удалил: {names} ({_n(r.meal.totals.kcal)} ккал)."


def _format_item_removed(r: ItemRemoved) -> str:
    names = ", ".join(_esc(i.name_snapshot) for i in r.items)
    kcal = sum(i.macros.kcal for i in r.items)
    return f"🗑 Убрал: {names} (−{_n(kcal)} ккал)\n\n{_remaining_block(r.remaining)}"


def _format_correction_failed(r: CorrectionFailed) -> str:
    if r.reason == "unsupported":
        return "Пока умею только удаление: напиши «удали рис». Замена и правка граммов — позже."
    target = f"«{_esc(r.target)}»" if r.target else "это"
    return f"Не нашёл {target} в недавних записях. Что удалить? Или /undo — снять последнюю запись."


def _format_unknown() -> str:
    return (
        "🤔 Не понял. Напиши, например:\n"
        "• «курица 250 грамм»\n"
        "• «два банана»\n"
        "• «вес 86.8»\n"
        "• «сколько осталось»"
    )


def _kcal_line(rem: Remaining) -> str:
    consumed = rem.consumed.kcal
    goal = rem.goal.daily_kcal
    pct = round(100 * consumed / goal) if goal else 0
    head = f"🔥 <b>{_n(consumed)} / {_n(goal)} ккал</b>"
    if rem.kcal_left >= 0:
        head += f" · осталось {_n(rem.kcal_left)}"
    else:
        head += f" · ⚠️ +{_n(-rem.kcal_left)} сверх"
    return f"{head}\n{_bar(consumed, goal)}  {pct}%"


def _macro_line(label: str, consumed: float, goal: float) -> str:
    return f"{label} {_n(consumed)}/{_n(goal)}  {_bar(consumed, goal)}"


def _remaining_block(rem: Remaining) -> str:
    return (
        f"{_kcal_line(rem)}\n"
        f"🥩 Б {_n(rem.consumed.protein)}/{_n(rem.goal.protein_g)} · "
        f"🧈 Ж {_n(rem.consumed.fat)}/{_n(rem.goal.fat_g)} · "
        f"🍞 У {_n(rem.consumed.carb)}/{_n(rem.goal.carb_g)}"
    )


def _bar(value: float, goal: float, width: int = 10) -> str:
    if goal <= 0:
        return "░" * width
    ratio = value / goal
    ratio = 0.0 if ratio < 0 else (1.0 if ratio > 1 else ratio)
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


def _sparkline(values: list[float]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return _SPARK[0] * len(values)
    span = hi - lo
    return "".join(_SPARK[round((v - lo) / span * (len(_SPARK) - 1))] for v in values)


def _esc(text: str) -> str:
    return escape(text, quote=False)


def _n(x: float) -> str:
    """Без хвостовых нулей: 412.0 -> 412, 86.8 -> 86.8."""
    return f"{x:g}"
