"""Рендер Result -> HTML-текст ответа бота.

Динамика (имена продуктов от LLM, транскрипты) экранируется. Прогресс-бары и
спарклайн веса рисуются текстом (█░ / ▁▂▃) — псевдо-дашборд без зависимостей.
"""
from __future__ import annotations

from datetime import date
from html import escape
from zoneinfo import ZoneInfo

from app.application.entities import Meal, MealItem
from app.application.results import (
    ClarificationNeeded,
    CorrectionFailed,
    DailyStats,
    DayView,
    History,
    ItemAdjusted,
    ItemRemoved,
    LabelRead,
    MealDeleted,
    MealLogged,
    Result,
    Unknown,
    VariantClarificationNeeded,
    WeeklyStats,
    WeightLogged,
    WeightTrend,
)
from app.domain.goals import Goal, Remaining
from app.domain.value_objects import sum_macros

_LOW_CONFIDENCE = 0.6  # ниже — помечаем разбор как сомнительный
_SPARK = "▁▂▃▄▅▆▇█"
_MONTHS_RU = (
    "янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек",
)


def format_result(result: Result) -> str:
    if isinstance(result, MealLogged):
        return _format_meal(result)
    if isinstance(result, ClarificationNeeded):
        return _format_clarification(result)
    if isinstance(result, VariantClarificationNeeded):
        return _format_variant_clarification(result)
    if isinstance(result, LabelRead):
        return _format_label_read(result)
    if isinstance(result, WeightLogged):
        return _format_weight(result)
    if isinstance(result, WeightTrend):
        return _format_weight_trend(result)
    if isinstance(result, DailyStats):
        return _format_stats(result)
    if isinstance(result, DayView):
        return _format_day_view(result)
    if isinstance(result, WeeklyStats):
        return _format_weekly(result)
    if isinstance(result, History):
        return _format_history(result)
    if isinstance(result, MealDeleted):
        return _format_deleted(result)
    if isinstance(result, ItemRemoved):
        return _format_item_removed(result)
    if isinstance(result, ItemAdjusted):
        return _format_item_adjusted(result)
    if isinstance(result, CorrectionFailed):
        return _format_correction_failed(result)
    if isinstance(result, Unknown):
        return _format_unknown(result.reason)
    return "⚠️ Не смог обработать. Переформулируй или напиши /help."


def _is_stated(it: MealItem) -> bool:
    """Названные пользователем тоталы (FR-22): не из каталога и НЕ AI-оценка → «📋»."""
    return it.product_id is None and not it.is_estimate


def _item_mark(it: MealItem) -> str:
    """Метка строки позиции (INV-13): «≈» для AI-оценки, «📋» для цифр юзера."""
    if it.is_estimate:
        return "≈ "
    if _is_stated(it):
        return "📋 "
    return ""


def _format_meal(r: MealLogged) -> str:
    lines = ["✅ Записал:"]
    for it in r.meal.items:
        m = it.macros
        grams_part = f"{_n(it.grams)} г · " if it.grams else ""  # блюда с готовыми КБЖУ — без граммов
        lines.append(
            f"• {_item_mark(it)}{_esc(it.name_snapshot)} — {grams_part}{_n(m.kcal)} ккал · "
            f"Б{_n(m.protein)} Ж{_n(m.fat)} У{_n(m.carb)}"
        )
    if len(r.meal.items) > 1:  # для одной позиции «Итого» дублирует строку — не показываем
        t = r.meal.totals
        lines.append(
            f"\nИтого: <b>{_n(t.kcal)} ккал</b> · Б{_n(t.protein)} Ж{_n(t.fat)} У{_n(t.carb)}"
        )
    lines += ["", _remaining_block(r.remaining)]
    notes = []
    if any(it.is_estimate for it in r.meal.items):  # INV-13: метка per-item, сноска поясняет
        notes.append("≈ — прикинул сам, приблизительно. Неверно? /products")
    if any(_is_stated(it) for it in r.meal.items):
        notes.append("📋 — по твоим цифрам (меню/этикетка)")
    if r.meal.source_confidence < _LOW_CONFIDENCE:
        notes.append("⚠️ не уверен в распознавании — проверь")
    if notes:
        lines += ["", *notes]
    return "\n".join(lines)


def _format_clarification(r: ClarificationNeeded) -> str:
    return f"🤔 «{_esc(r.item_name)}» — какая порция? Жми кнопку или впиши граммы."


def _format_variant_clarification(r: VariantClarificationNeeded) -> str:
    return f"🤔 «{_esc(r.item_name)}» — уточни (или впиши свой вариант):"


def _format_label_read(r: LabelRead) -> str:
    it = r.draft.items[r.item_index]
    pkg = f" · пачка {_n(it.unit_weight_g)} г" if it.unit_weight_g else ""
    tail = " или жми «вся пачка»." if it.unit_weight_g else "."
    warn = (
        "\n⚠️ Б/Ж/У не сходятся с ккал — возможно, я ошибся при чтении. Проверь или переснимь чётче."
        if r.atwater_warning
        else ""
    )
    return (
        f"📋 Этикетка: <b>{_esc(it.name)}</b>{pkg}\n"
        f"На 100 г: {_n(it.kcal_per_100g)} ккал · Б{_n(it.protein_per_100g)} "
        f"Ж{_n(it.fat_per_100g)} У{_n(it.carb_per_100g)}{warn}\n\n"
        f"Сколько грамм съел? Напиши число{tail}"
    )


def _format_weight(r: WeightLogged) -> str:
    text = f"⚖️ Вес записан: <b>{_n(r.log.weight_kg)} кг</b>"
    if r.delta_kg is not None:
        sign = "+" if r.delta_kg > 0 else ""
        text += f" ({sign}{_n(r.delta_kg)} кг с прошлого раза)"
    return text


def format_goal(goal: Goal) -> str:
    """Грид дневной цели в общем выровненном стиле (для /goal и завершения профиля)."""
    rows = "\n".join(
        [
            f"{'🔥 Ккал':<10} {round(goal.daily_kcal)}",
            f"{'🥩 Белок':<10} {round(goal.protein_g)} г",
            f"{'🧈 Жир':<10} {round(goal.fat_g)} г",
            f"{'🍞 Углеводы':<10} {round(goal.carb_g)} г",
        ]
    )
    return f"<code>{rows}</code>"


def _format_weight_trend(t: WeightTrend) -> str:
    if not t.logs:
        return "⚖️ Вес ещё не записан.\n📌 Записать — напиши «вес 86.8»."
    newest = t.logs[0]
    text = f"⚖️ <b>{_n(newest.weight_kg)} кг</b>"
    if len(t.logs) >= 2:
        chrono = list(reversed(t.logs))  # старые → новые, чтобы спарклайн шёл по времени
        spark = _sparkline([log.weight_kg for log in chrono])
        delta = round(newest.weight_kg - chrono[0].weight_kg, 1)
        days = (newest.measured_at - chrono[0].measured_at).days
        sign = "+" if delta > 0 else ""
        period = f"за {days} дн." if days >= 1 else "сегодня"
        text += f"\n{spark}  Δ {sign}{_n(delta)} кг {period}"
    text += f"\nЗаписей: {len(t.logs)}\n📌 Новый замер — напиши «вес 86.5»"
    return text


def _format_stats(r: DailyStats) -> str:
    if r.meals_count == 0:
        return (
            "📊 Сегодня пока пусто.\nЗапиши приём — например «куриная грудка 250 г», "
            "наговори голосом или пришли фото этикетки 📷.\n\n" + _dashboard(r.remaining)
        )
    eaten = _eaten_lines(r.meals, r.timezone) if r.meals else ""
    return f"📊 Сегодня · приёмов: {r.meals_count}\n{eaten}\n{_dashboard(r.remaining)}"


def _eaten_lines(meals: list[Meal], tz_name: str) -> str:
    """Компактный список съеденного за день: «чч:мм · продукт — N ккал» по позициям."""
    tz = ZoneInfo(tz_name)
    rows = []
    for m in meals:
        when = m.eaten_at.astimezone(tz).strftime("%H:%M")
        for it in m.items:
            g = f"{_n(it.grams)} г · " if it.grams else ""
            rows.append(f" {when} · {_esc(it.name_snapshot)} — {g}{_n(it.macros.kcal)} ккал")
            when = "     "  # последующие позиции приёма — без повтора времени
    return "<code>" + "\n".join(rows) + "</code>\n"


def _day_title(d: date, days_ago: int) -> str:
    label = "Сегодня" if days_ago == 0 else "Вчера" if days_ago == 1 else ""
    ds = f"{d.day} {_MONTHS_RU[d.month - 1]}"
    return f"{label}, {ds}" if label else ds


def _format_day_view(r: DayView) -> str:
    title = _day_title(r.day, r.days_ago)
    if not r.meals:
        return f"🗒 <b>{title}</b>\nПусто — в этот день ничего не записано."
    t = sum_macros(m.totals for m in r.meals)
    return (
        f"🗒 <b>{title}</b>\n"
        f"{_eaten_lines(r.meals, r.timezone)}"
        f"─────\nИтого: <b>{_n(t.kcal)} ккал</b> · Б{_n(t.protein)} Ж{_n(t.fat)} У{_n(t.carb)}"
    )


def _format_weekly(r: WeeklyStats) -> str:
    if r.days_logged == 0:
        return "📈 За неделю пока нет записей. Запиши пару приёмов — и покажу среднее в день."
    days = r.days_logged or 1
    c = r.consumed
    g = r.goal
    rows = "\n".join(
        [
            _macro_line("🔥 Ккал", round(c.kcal / days), g.daily_kcal),
            _macro_line("🥩 Белок", round(c.protein / days), g.protein_g),
            _macro_line("🧈 Жир", round(c.fat / days), g.fat_g),
            _macro_line("🍞 Углеводы", round(c.carb / days), g.carb_g),
        ]
    )
    return (
        f"📈 Неделя · {r.days_logged} дн · приёмов {r.meals_count}\n"
        "среднее в день:\n"
        f"<code>{rows}</code>"
    )


def _format_history(r: History) -> str:
    if not r.meals:
        return "🗒 История пуста."
    tz = ZoneInfo(r.timezone)
    rows = []
    for m in r.meals:
        when = m.eaten_at.astimezone(tz).strftime("%d.%m %H:%M")
        names = ", ".join(i.name_snapshot for i in m.items) or "—"
        if len(names) > 28:
            names = names[:27] + "…"  # ponytail: обрезка под выровненную колонку
        rows.append(f"{when} {_n(m.totals.kcal):>5} · {_esc(names)}")
    return "🗒 Последние записи:\n<code>" + "\n".join(rows) + "</code>"


def _format_deleted(r: MealDeleted) -> str:
    if r.meal is None:
        return "Нечего удалять."
    names = _esc(", ".join(i.name_snapshot for i in r.meal.items) or "запись")
    return f"🗑 Удалил: {names} ({_n(r.meal.totals.kcal)} ккал)."


def _format_item_removed(r: ItemRemoved) -> str:
    names = ", ".join(_esc(i.name_snapshot) for i in r.items)
    kcal = sum(i.macros.kcal for i in r.items)
    return f"🗑 Убрал: {names} (−{_n(kcal)} ккал)\n\n{_remaining_block(r.remaining)}"


def _format_item_adjusted(r: ItemAdjusted) -> str:
    it = r.item
    return (
        f"✏️ Поправил: {_esc(it.name_snapshot)} {_n(r.old_grams)}→{_n(it.grams)} г "
        f"({_n(it.macros.kcal)} ккал)\n\n{_remaining_block(r.remaining)}"
    )


def _format_correction_failed(r: CorrectionFailed) -> str:
    if r.reason == "unsupported":
        return (
            "Умею удаление («удали рис») и правку веса («рис не 250 а 180»). "
            "Чтобы заменить продукт — удали и запиши заново."
        )
    target = f"«{_esc(r.target)}»" if r.target else "это"
    return f"Не нашёл {target} в недавних записях. Что удалить? Или /undo — снять последнюю запись."


def _format_unknown(reason: str = "") -> str:
    """Отбивка «не понял». Под конкретную причину — точное сообщение, иначе общие подсказки."""
    r = reason.lower()
    if "пределов" in r:  # вес вне 20–400 кг
        return "⚖️ Вес должен быть 20–400 кг. Проверь число и пришли снова («вес 86»)."
    if "vision" in r:  # распознавание этикеток выключено (нет ключа)
        return (
            "📷 Сейчас не читаю фото этикеток. Впиши КБЖУ текстом: "
            "«творог, 120 ккал, 18 белка, 5 жира, 3 углевода»."
        )
    if "этикет" in r:  # фото пришло, но таблицу КБЖУ не разобрали
        return "📋 Не разобрал этикетку. Сфоткай таблицу КБЖУ крупно, при свете и без бликов."
    return (
        "🤔 Не понял. Я считаю КБЖУ:\n"
        "• еда — «куриная грудка 200 г», «два банана»\n"
        "• 🎤 голосом или 📷 фото этикетки\n"
        "• ⚖️ «вес 86» · 📊 «сколько осталось»"
    )


def _dashboard(rem: Remaining) -> str:
    """Выровненная сетка B (моноширинно <code>): ккал + БЖУ слитно, в одной колонке."""
    rows = "\n".join(
        [
            _macro_line("🔥 Ккал", rem.consumed.kcal, rem.goal.daily_kcal),
            _macro_line("🥩 Белок", rem.consumed.protein, rem.goal.protein_g),
            _macro_line("🧈 Жир", rem.consumed.fat, rem.goal.fat_g),
            _macro_line("🍞 Углеводы", rem.consumed.carb, rem.goal.carb_g),
        ]
    )
    return f"<code>{rows}</code>"


def _macro_line(label: str, consumed: float, goal: float) -> str:
    """Строка макроса: ПОЛОСКА первой (col 0) → выравнивается на любой платформе. На Android
    кириллица в <code> не моноширинна, поэтому подпись/числа в ХВОСТ — их «рваность» не видна.
    Бар (рамочные символы) + % — единственное, что выровнено; считаем по точным значениям."""
    pct = round(100 * consumed / goal) if goal else 0
    nums = f"{round(consumed)}/{round(goal)}"
    return f"{_bar(consumed, goal)} {pct:>3}% · {label} {nums}"


def _remaining_block(rem: Remaining) -> str:
    return _dashboard(rem)


def _bar(value: float, goal: float, fill: str = "▰", empty: str = "▱", width: int = 10) -> str:
    """Текстовая полоска (стиль C ▰▱ по умолчанию); fill/empty параметризованы (FR-9)."""
    if goal <= 0:
        return empty * width
    ratio = 0.0 if value < 0 else min(1.0, value / goal)
    filled = max(0, min(width, round(ratio * width)))
    return fill * filled + empty * (width - filled)


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
