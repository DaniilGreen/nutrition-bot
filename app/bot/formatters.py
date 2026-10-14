"""Рендер Result -> HTML-текст ответа бота.

Динамика (имена продуктов от LLM, транскрипты) экранируется. Прогресс-бары и
спарклайн веса рисуются текстом (█░ / ▁▂▃) — псевдо-дашборд без зависимостей.
"""
from __future__ import annotations

from html import escape
from zoneinfo import ZoneInfo

from app.application.entities import Meal, MealItem
from app.application.results import (
    ClarificationNeeded,
    CorrectionFailed,
    DailyStats,
    DishSaved,
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
from app.domain.goals import Remaining

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
    if isinstance(result, DishSaved):
        return format_recipe_saved(result)
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


def _meal_head(r: MealLogged) -> list[str]:
    lines = ["✅ Записал:"]
    for it in r.meal.items:
        m = it.macros
        grams_part = f"{_n(it.grams)} г · " if it.grams else ""  # готовые КБЖУ — без граммов
        lines.append(
            f"• {_item_mark(it)}{_esc(it.name_snapshot)} — {grams_part}{_n(m.kcal)} ккал · "
            f"Б{_n(m.protein)} Ж{_n(m.fat)} У{_n(m.carb)}"
        )
    if len(r.meal.items) > 1:  # для одной позиции «Итого» дублирует строку — не показываем
        t = r.meal.totals
        lines.append(
            f"\nИтого: <b>{_n(t.kcal)} ккал</b> · Б{_n(t.protein)} Ж{_n(t.fat)} У{_n(t.carb)}"
        )
    return lines


def _meal_notes(r: MealLogged) -> list[str]:
    notes = []
    if any(it.is_estimate for it in r.meal.items):  # INV-13: метка per-item, сноска поясняет
        notes.append("≈ — прикинул сам, приблизительно. Неверно? /products")
    if any(_is_stated(it) for it in r.meal.items):
        notes.append("📋 — по твоим цифрам (меню/этикетка)")
    if r.meal.source_confidence < _LOW_CONFIDENCE:
        notes.append("⚠️ не уверен в распознавании — проверь")
    return notes


def _format_meal(r: MealLogged) -> str:
    """Текстовая карточка (бары текстом) — для путей без фото (после уточнения)."""
    lines = _meal_head(r) + ["", _remaining_block(r.remaining)]
    notes = _meal_notes(r)
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
    """Строка макроса (подпись первой). Числа округляем до целых, поля фикс-ширины.
    Глиф полоски — █/░ (дефолт _bar): полные блоки ровнее ▰▱. Бар по точным значениям."""
    pct = round(100 * consumed / goal) if goal else 0
    nums = f"{round(consumed)}/{round(goal)}"
    return f"{label:<10} {nums:<9} {_bar(consumed, goal)} {pct}%"


def _remaining_block(rem: Remaining) -> str:
    return _dashboard(rem)


def _bar(value: float, goal: float, fill: str = "█", empty: str = "░", width: int = 10) -> str:
    """Текстовая полоска (█/░ — полные блоки, ровнее ▰▱ на Android); fill/empty парам. (FR-9)."""
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


_FAT_WORD = {"none": "без масла", "little": "немного, 5 г", "spoon": "ложка, 10 г",
             "lots": "много, 20 г"}
_DRESS_WORD = {"none": "без", "sour_cream": "сметана, 20 г", "mayo": "майонез, 15 г",
               "oil": "масло, 10 г"}


def format_recipe_saved(saved: object) -> str:
    """FR-39.1: рецепт сохранён продуктом. В день НЕ записан — человек это приготовил, а не съел."""
    product = saved.product  # type: ignore[attr-defined]
    d = product.density
    name = escape(product.display_name, quote=False)
    mark = " ≈" if product.is_estimate else ""
    lines = [
        f"📖 Рецепт <b>{name}</b>{mark} сохранён",
        f"На 100 г: {d.kcal_per_100g:.0f} ккал · Б {d.protein_per_100g:.0f}"
        f" · Ж {d.fat_per_100g:.0f} · У {d.carb_per_100g:.0f}",
    ]
    if product.unit_weight_g:
        lines.append(f"Порция {product.unit_weight_g:.0f} г — пиши «{name}», запишу порцию.")
    else:
        lines.append(f"В день не записал. Дальше пиши «{name} 120 г».")
    return "\n".join(lines)


def format_set_saved(meal: object, name: str) -> str:
    """FR-39.2: набор сохранён. Показываем состав и как звать его дальше."""
    items = meal.items  # type: ignore[attr-defined]
    parts = ", ".join(f"{escape(i.name_snapshot, quote=False)} {i.grams:.0f} г" for i in items)
    safe = escape(name, quote=False)
    first = escape(items[0].name_snapshot, quote=False) if items else "позиции"
    return (
        f"📌 Набор <b>{safe}</b> сохранён\n"
        f"{parts}\n"
        f"Дальше пиши «{safe}» — запишу этот же состав, «2 {safe}» — вдвое. "
        f"Править как обычно: убрать позицию кнопкой или «поправь граммы у {first}»."
    )


def format_photo_card(
    plate: object, grams: list[float], *, weighed: bool,
    fat: str | None = None, dressing: str | None = None,
) -> str:
    """FR-34: что увидели на фото и чего ждём. Калорий нет — их посчитает разбор
    после «Записать», и цифра до записи разошлась бы с цифрой после (INV-1)."""
    lines = [f"📷 <b>{escape(plate.dish or 'Блюдо')}</b> · "
             + ("взвешено" if weighed else "вес на глаз")]
    for c, g in zip(plate.components, grams, strict=True):
        lines.append(f"• {escape(c.name)} — {g:g} г")
    lines.append(f"Всего {sum(grams):g} г")
    if not weighed:
        lines.append("\nВзвешивал? Пришли вес тарелки числом — пересчитаю, "
                     "так точнее.")
    if plate.ask_fat:
        oil = plate.fat_kind.replace("масло ", "") if plate.fat_kind else ""
        answer = _FAT_WORD.get(fat or "", "❔ отметь кнопкой")
        lines.append(f"\nМасло при готовке{f' ({escape(oil)})' if oil else ''}: {answer}")
    if plate.ask_dressing:
        lines.append(f"Заправка: {_DRESS_WORD.get(dressing or '', '❔ отметь кнопкой')}")
    return "\n".join(lines)


SCAN_REFUSAL = {
    "not_inbody": "Пока понимаю только бланки InBody — у других приборов обмен считается иначе.",
    "no_numbers": "Не нашёл на бланке вес и жир — сними его ровно и целиком, при свете.",
}

_SCAN_LINES = (
    ("weight_kg", "Вес", "кг"),
    ("skeletal_muscle_kg", "Скелетные мышцы", "кг"),
    ("body_fat_kg", "Жир", "кг"),
    ("body_fat_pct", "Процент жира", "%"),
    ("fat_free_kg", "Безжировая масса", "кг"),
    ("visceral_level", "Висцеральный жир", "· норма 1–9"),
    ("bmr_kcal", "Базовый обмен", "ккал"),
    ("smi", "Мышечный индекс", "кг/м²"),
    ("water_l", "Вода", "л"),
    ("protein_kg", "Белок", "кг"),
    ("minerals_kg", "Минералы", "кг"),
)
_ALWAYS = {"weight_kg", "skeletal_muscle_kg", "body_fat_kg", "body_fat_pct", "fat_free_kg",
           "visceral_level", "bmr_kcal", "water_l", "smi"}


def _num(v: float) -> str:
    return f"{v:g}".replace(".", ",")


def _fat(v: float) -> str:
    """Жир сегмента бланк печатает с одной цифрой после запятой — «12,0», не «12»."""
    return f"{v:.1f}".replace(".", ",")


def format_scan_card(check: object, tz: str) -> str:
    """FR-36: что прочитали с бланка; несошедшееся — с ⚠️ (MA-INV-50)."""
    s = check.scan  # type: ignore[attr-defined]
    local = s.measured_at.astimezone(ZoneInfo(tz))
    head = f"📋 <b>{escape(s.device or 'InBody')} · {local:%d.%m.%Y %H:%M}</b>"
    who = " · ".join(x for x in (
        f"{_num(s.height_cm)} см" if s.height_cm else "",
        f"{s.age} лет" if s.age else "",
    ) if x)
    lines = [head + (f"\n{who}" if who else ""), ""]
    bad = check.bad  # type: ignore[attr-defined]
    for key, label, unit in _SCAN_LINES:
        v = getattr(s, key)
        if v is None or (key not in _ALWAYS and key not in bad):
            continue
        mark = "⚠️ " if key in bad else ""
        lines.append(f"{mark}{label}: <b>{_num(v)}</b> {unit}")
    if s.inbody_score is not None:
        lines.append(f"Балл InBody: <b>{_num(s.inbody_score)}</b> из 100")
    if s.lean_trunk_kg is not None:
        lines += [
            "",
            "<i>Сегменты, безжировая (жир), кг:</i>",
            f"Руки {_num(s.lean_arm_l_kg)} ({_fat(s.fat_arm_l_kg)}) · "
            f"{_num(s.lean_arm_r_kg)} ({_fat(s.fat_arm_r_kg)})",
            f"Ноги {_num(s.lean_leg_l_kg)} ({_fat(s.fat_leg_l_kg)}) · "
            f"{_num(s.lean_leg_r_kg)} ({_fat(s.fat_leg_r_kg)})",
            f"Туловище {_num(s.lean_trunk_kg)} ({_fat(s.fat_trunk_kg)})",
        ]
    notes = []
    if getattr(check, "dropped_segments", False):
        notes.append("Сегментный анализ не сошёлся с безжировой массой — сохраню без него. "
                     "На цели он не влияет.")
    if bad:
        notes.append("Отмеченные числа не сходятся с остальными — сверь с бланком. Поправить "
                     "их можно в мини-аппе: сними бланк там.")
    if check.foreign:  # type: ignore[attr-defined]
        notes.append("Похоже, бланк не твой: рост, пол, возраст или вес не сходятся с прошлыми.")
    if check.date_ambiguous:  # type: ignore[attr-defined]
        notes.append("Дату можно прочитать двумя способами — проверь её.")
    if check.date_problem == "unread":  # type: ignore[attr-defined]
        notes.append("Дату на бланке не разобрал — поставил сегодняшнюю. Другая дата — "
                     "поправь её в мини-аппе.")
    if check.date_problem == "old":  # type: ignore[attr-defined]
        notes.append("Замеру больше двух лет — точно этот?")
    if check.date_problem == "future":  # type: ignore[attr-defined]
        notes.append("Дата замера в будущем — так сохранить нельзя.")
    if check.existing is not None:  # type: ignore[attr-defined]
        notes.append("Такой замер уже есть — сохраню поверх.")
    if notes:
        lines += [""] + notes
    lines += ["", "<i>Сравнивай замеры, сделанные в одинаковых условиях: утром, натощак, "
              "без тренировки накануне.</i>"]
    return "\n".join(lines)


_TARGET_WORD = {"cut": "похудение", "maintain": "поддержание", "bulk": "набор"}
_ACTIVITY_FACTOR = {"low": 1.2, "medium": 1.375, "high": 1.55}


def format_goal_proposal(p: object) -> str:
    """«Было → станет» (MA-INV-52): цели меняются только кнопкой."""
    cur, new = p.current, p.proposed  # type: ignore[attr-defined]
    target = _TARGET_WORD.get(cur.target, cur.target)
    if p.base == "energy":  # type: ignore[attr-defined]
        basis = f"{p.tdee_kcal:g} расход по замерам × {target}"  # type: ignore[attr-defined]
    else:
        basis = (f"{p.bmr_kcal:g} обмен × {_num(_ACTIVITY_FACTOR[p.activity])} активность × "  # type: ignore[attr-defined]
                 f"{target}")

    def row(label: str, a: float, b: float, unit: str) -> str:
        d = round(b) - round(a)
        diff = f"{'+' if d > 0 else '−'}{abs(d)}" if d else "без изменений"
        return f"{label}: {round(a)} → <b>{round(b)}</b>{unit} ({diff})"

    return "\n".join([
        "🎯 <b>Цели по замеру</b>",
        f"<i>{escape(basis)}; белок — от безжировой массы</i>",
        row("Калории", cur.daily_kcal, new.daily_kcal, ""),
        row("Белок", cur.protein_g, new.protein_g, " г"),
        row("Жир", cur.fat_g, new.fat_g, " г"),
        row("Углеводы", cur.carb_g, new.carb_g, " г"),
        "Прошедшие дни недели останутся посчитанными по старой цели.",
    ])
