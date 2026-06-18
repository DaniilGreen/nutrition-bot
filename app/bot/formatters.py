"""Рендер Result -> HTML-текст ответа бота."""
from __future__ import annotations

from zoneinfo import ZoneInfo

from app.application.results import (
    ClarificationNeeded,
    DailyStats,
    History,
    MealDeleted,
    MealLogged,
    Result,
    Unknown,
    WeeklyStats,
    WeightLogged,
)
from app.domain.goals import Remaining


def format_result(result: Result) -> str:
    if isinstance(result, MealLogged):
        return _format_meal(result)
    if isinstance(result, ClarificationNeeded):
        return _format_clarification(result)
    if isinstance(result, WeightLogged):
        return _format_weight(result)
    if isinstance(result, DailyStats):
        return _format_stats(result.remaining, result.meals_count)
    if isinstance(result, WeeklyStats):
        return _format_weekly(result)
    if isinstance(result, History):
        return _format_history(result)
    if isinstance(result, MealDeleted):
        return _format_deleted(result)
    if isinstance(result, Unknown):
        return _format_unknown()
    return "Что-то пошло не так."


def _format_meal(r: MealLogged) -> str:
    lines = ["✅ Записал:"]
    for it in r.meal.items:
        m = it.macros
        lines.append(
            f"• {it.name_snapshot} — {_n(it.grams)} г · {_n(m.kcal)} ккал · "
            f"Б{_n(m.protein)} Ж{_n(m.fat)} У{_n(m.carb)}"
        )
    t = r.meal.totals
    lines += [
        "",
        f"Итого: <b>{_n(t.kcal)} ккал</b> · Б{_n(t.protein)} Ж{_n(t.fat)} У{_n(t.carb)}",
        "",
        _remaining_block(r.remaining),
    ]
    return "\n".join(lines)


def _format_clarification(r: ClarificationNeeded) -> str:
    return f"🤔 «{r.item_name}» — какая порция?"


def _format_weight(r: WeightLogged) -> str:
    text = f"⚖️ Вес записан: <b>{_n(r.log.weight_kg)} кг</b>"
    if r.delta_kg is not None:
        sign = "+" if r.delta_kg > 0 else ""
        text += f" ({sign}{_n(r.delta_kg)} кг с прошлого раза)"
    return text


def _format_stats(rem: Remaining, meals_count: int) -> str:
    return f"📊 Сегодня (приёмов пищи: {meals_count})\n\n{_remaining_block(rem)}"


def _format_weekly(r: WeeklyStats) -> str:
    avg = round(r.consumed.kcal / r.days_logged) if r.days_logged else 0
    c = r.consumed
    return (
        "📈 За неделю:\n"
        f"Калории: <b>{_n(c.kcal)}</b> · в среднем {_n(avg)}/день за {r.days_logged} дн.\n"
        f"Б{_n(c.protein)} Ж{_n(c.fat)} У{_n(c.carb)} · приёмов: {r.meals_count}\n"
        f"Цель: {_n(r.goal.daily_kcal)} ккал/день"
    )


def _format_history(r: History) -> str:
    if not r.meals:
        return "🗒 История пуста."
    tz = ZoneInfo(r.timezone)
    lines = ["🗒 Последние записи:"]
    for m in r.meals:
        when = m.eaten_at.astimezone(tz).strftime("%d.%m %H:%M")
        names = ", ".join(i.name_snapshot for i in m.items) or "—"
        lines.append(f"• {when} — {_n(m.totals.kcal)} ккал ({names})")
    return "\n".join(lines)


def _format_deleted(r: MealDeleted) -> str:
    if r.meal is None:
        return "Нечего удалять."
    names = ", ".join(i.name_snapshot for i in r.meal.items) or "запись"
    return f"🗑 Удалил: {names} ({_n(r.meal.totals.kcal)} ккал)."


def _remaining_block(rem: Remaining) -> str:
    return (
        f"Осталось: <b>{_n(rem.kcal_left)} / {_n(rem.goal.daily_kcal)} ккал</b>\n"
        f"Б: {_n(rem.protein_left)} · Ж: {_n(rem.fat_left)} · У: {_n(rem.carb_left)}"
    )


def _format_unknown() -> str:
    return (
        "🤔 Не понял. Напиши, например:\n"
        "• «курица 250 грамм»\n"
        "• «два банана»\n"
        "• «вес 86.8»\n"
        "• «сколько осталось»"
    )


def _n(x: float) -> str:
    """Без хвостовых нулей: 412.0 -> 412, 86.8 -> 86.8."""
    return f"{x:g}"
