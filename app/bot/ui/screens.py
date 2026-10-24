"""Все экраны бота в едином стиле «Дашборд» на Rich Messages (Bot API 10.1).

ОДИН визуальный язык на весь бот (выбор владельца — дизайн A из демо):
* заголовок <h3> с эмодзи раздела;
* макросы — строка «подпись · факт · %», под ней сплошной цветной бар (цвет = наполнение);
* дневная цифра всегда рядом с недельным якорем (D-25);
* «недобор» — только к концу дня (D-29), перебор виден всегда (D-24);
* съеденное — по вкладу в калории, под <details>.

Функции возвращают ГОТОВЫЙ HTML. Отправку (send_rich_message vs answer) выбирает хендлер:
rich — там, где есть бары/таблицы; plain — короткие подсказки внутри диалога.
Экранирование пользовательских строк — через kit.e (имена продуктов приходят от LLM и юзера).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.application.entities import Meal, MealItem, Product
from app.application.results import DayView, ItemAdjusted, ItemRemoved, LabelRead, MealLogged
from app.bot.ui import blocks, kit
from app.bot.ui.context import DayContext, day_status
from app.bot.ui.kit import e, em, n, pct, tg_time, verdict
from app.domain.goals import Goal, Remaining
from app.domain.value_objects import Macros


def _short(name: str, limit: int = 16) -> str:
    """Обрезка с многоточием: молча урезанное имя искажает продукт на экране, где его же чинят."""
    return name if len(name) <= limit else name[: limit - 1] + "…"


def _bars(cons: Macros, goal: Goal) -> str:
    """Четыре бара подряд — сердце «Дашборда». Один и тот же вид на всех экранах."""
    return "".join(blocks.macro_block(lab, c, g) for lab, c, g in kit.macros_rows(cons, goal))


# ---------- «сегодняшние» экраны (питаются DayContext.gather) ----------


def today(d: DayContext) -> str:
    c, g = d.today.remaining.consumed, d.goal
    if not d.today.meals:
        return (
            "<h3>📊 Сегодня</h3><p>Пока пусто. Запиши приём — «куриная грудка 250 г», "
            "голосом или фото блюда и этикетки 📷.</p>"
        )
    # Недельный якорь убран с «Сегодня» по просьбе владельца (остаётся в вечернем итоге). <hr/>
    # разделяет прогресс по макросам и блок «точность / действие / съедено».
    return (
        "<h3>📊 Сегодня</h3>"
        f"<p><i>{verdict(c, g, final=d.day_over)}</i></p>"
        + _bars(c, g)
        + "<hr/>"
        + blocks.accuracy(d)
        + blocks.over_note(d)
        + blocks.advice(d)
        + blocks.eaten(d)  # свёрнут по умолчанию
    )


def _meal_day_label(eaten_at: datetime, tz_name: str, today: date) -> str:
    """« на вчера, 16 июл» для бэкдейта; пусто для сегодня (FR-33).

    Без метки бэкдейт был бы молчаливым: юзер сказал «вчера», а карточка выглядит как сегодняшняя.
    """
    md = eaten_at.astimezone(ZoneInfo(tz_name)).date()
    if md == today:
        return ""
    word = {-1: "вчера", -2: "позавчера", 1: "завтра", 2: "послезавтра"}.get((md - today).days)
    ds = f"{md.day} {kit.MON[md.month - 1]}"
    return f" на {word}, {ds}" if word else f" на {ds}"


def meal_card(
    d: DayContext, r: MealLogged, *, edited: bool = False, restored: bool = False
) -> str:
    """Карточка сразу после записи — самый горячий момент: человек ещё у тарелки, уточнить 5 сек.

    Показываем ЧТО записали (метки ≈/📋), вклад приёма, прогресс ЕГО дня всеми барами и одну
    подсказку. При «вчера/завтра» бары и заголовок — про ТОТ день (r.remaining), а добор молчит:
    он про сегодняшний конец дня, к прошлому/будущему дню неприменим. restored — после «⏪ Вернуть»
    (FR-41): заголовок называет то, что человек нажал.
    """
    m, g = r.meal, d.goal
    today = d.days[-1][0] if d.days else m.eaten_at.astimezone(ZoneInfo(d.today.timezone)).date()
    day_label = _meal_day_label(m.eaten_at, d.today.timezone, today)
    lis = []
    for it in m.items:
        grams = f"{n(it.grams)} г · " if it.grams else ""
        lis.append(
            f"<li>{_item_mark(it)}{e(it.name_snapshot)} — {grams}<b>{n(it.macros.kcal)}</b> ккал · "
            f"Б{n(it.macros.protein)} Ж{n(it.macros.fat)} У{n(it.macros.carb)}</li>"
        )
    total = (  # при одной позиции строка «Приём: …» дословно повторяет её же
        f"<p>Приём: <b>{n(m.totals.kcal)} ккал</b> ({pct(m.totals.kcal, g.daily_kcal)}% цели) · "
        f"Б{n(m.totals.protein)} Ж{n(m.totals.fat)} У{n(m.totals.carb)}</p>"
        if len(m.items) > 1
        else ""
    )
    return (
        # MA-INV-70: правленая карточка не должна читаться как новая запись.
        f"<h3>{'⏪ Вернул' if restored else '✏️ Поправил' if edited else '✅ Записал'}{day_label} · "
        f"{tg_time(m.eaten_at, d.today.timezone)}</h3>"
        + f"<ul>{''.join(lis)}</ul>"
        + total
        + _notes(r)
        + "<hr/>"
        + _bars(r.remaining.consumed, r.remaining.goal)  # прогресс ДНЯ ПРИЁМА (бэкдейт → тот день)
        + (blocks.advice(d) if not day_label else "")  # добор — только про сегодня
    )


def evening(d: DayContext) -> str:
    """Вечерний итог (пуш). Асимметрия: недобор → действие, перебор → контекст и «ничего не делай».

    Всё закрыто — бот обязан УМЕТЬ МОЛЧАТЬ: короткое «делать нечего», без похвал и конфетти.
    """
    c, g = d.today.remaining.consumed, d.goal
    if not d.today.meals:
        return "<h3>🌙 Итог дня</h3><p>Сегодня ничего не записано. Кинь текстом, что ел.</p>"
    if c.kcal < g.daily_kcal * 0.4:
        return (
            "<h3>🌙 Итог дня</h3>"
            f"<p>Записано {n(c.kcal)} ккал — похоже, не всё попало в бота. Кинь текстом.</p>"
            + blocks.anchor(d)
        )
    adv, over = blocks.advice(d), blocks.over_note(d)
    if not adv and not over:
        return (
            "<h3>🌙 Итог дня</h3>"
            f"<p><b>{n(c.kcal)} / {n(g.daily_kcal)} ккал</b> · белок {n(c.protein)} г — всё "
            "закрыто. Делать ничего не надо.</p>" + blocks.week_balance(d) + blocks.anchor(d)
        )
    return (
        "<h3>🌙 Итог дня</h3>"
        + blocks.macro_block("Ккал", c.kcal, g.daily_kcal)
        + blocks.macro_block("Белок", c.protein, g.protein_g)
        + over
        + adv
        + blocks.week_balance(d)
        + blocks.anchor(d)
    )


def week(d: DayContext) -> str:
    w = d.week7
    if not w.days_logged:
        return (
            "<h3>📈 Неделя</h3><p>За 7 дней записей нет. Запиши пару приёмов — покажу среднее.</p>"
        )
    a, g = w.avg_macros, d.goal
    return (
        blocks.week_balance(d)
        + "<h3>📈 Неделя · в среднем за день</h3>"
        f"<p><i>{verdict(a, g, final=True)}</i></p>"
        + _bars(a, g)
        + f"<p>Дней в цели: <b>{w.days_in_goal}</b> из {w.days_logged} записанных</p>"
        + blocks.week_table(d)
        + "<footer>Один день — шум. Управляй этой средней, а не вчерашним ужином.</footer>"
    )


def month(d: DayContext) -> str:
    m = d.month
    if not m.days_logged:
        return "<h3>📅 30 дней</h3><p>За месяц записей нет — поедим, потом покажу отчёт.</p>"
    a, g = m.avg_macros, d.goal
    dw = f"{m.weight_delta:+.1f} кг" if m.weight_delta is not None else "—"
    rows = (
        f'<tr><td>Ккал/день</td><td align="right"><b>{n(a.kcal)}</b> / {n(g.daily_kcal)}</td></tr>'
        f'<tr><td>Белок/день</td><td align="right">{n(a.protein)} / {n(g.protein_g)} г</td></tr>'
        f'<tr><td>Вес за период</td><td align="right">{dw}</td></tr>'
    )
    return (
        "<h3>📅 30 дней</h3>"
        + blocks.coverage_block(d)
        + f"<table striped>{rows}</table>"
        + blocks.weight_block(d)
    )


def streak(days: Sequence[tuple[date, float]], goal_kcal: float) -> str:
    """Календарь «дней в цели» цветными эмодзи, 7 в ряд. Замена PNG-хитмапа.

    Цвет = статус дня (пусто/мало/в цели/перебор), тот же словарь, что в неделе. 7 эмодзи ≈ 322
    у.е. — влезает. Ряды — недели, свежий день внизу справа.
    """
    if not any(k > 0 for _, k in days):
        return "<h3>🗓 Дни в цели</h3><p>Пока пусто — запиши пару дней, появится календарь.</p>"
    cells = [em(*_seg_for(day_status(k, goal_kcal))) for _, k in days]
    rows = "".join("<p>" + "".join(cells[i : i + 7]) + "</p>" for i in range(0, len(cells), 7))
    hit = sum(1 for _, k in days if day_status(k, goal_kcal) == "ok")
    return (
        "<h3>🗓 Дни в цели</h3>"
        + rows
        + f"<footer>🟢 в цели: {hit} из {len(days)} дней. Провал — не перебор, а пропуск.</footer>"
    )


def weight(d: DayContext) -> str:
    """Экран веса. Тренд — только когда его честно есть из чего считать (иначе «встань на весы»)."""
    logs = d.weight.logs
    body = blocks.weight_block(d)
    if len(logs) >= 2:
        lis = "".join(
            f"<li>{log.measured_at.strftime('%d.%m')} · <b>{log.weight_kg:.1f}</b> кг</li>"
            for log in logs[:8]
        )
        body += f"<details><summary>📋 Замеры</summary><ul>{lis}</ul></details>"
    return "<h3>⚖️ Вес</h3>" + body


TARGET_RU = {"cut": "🔻 Похудение", "maintain": "⚖️ Поддержание", "bulk": "🔺 Набор массы"}


def goal_screen(goal: Goal, weight_kg: float | None) -> str:
    """Дневная цель. Иерархия: белок — обязательство, жир — минимум, углеводы — сдача.

    Цель (похудение/поддержание/набор) показываем ЯВНО: от неё зависит вся раскладка (белок на кг,
    доля жира), и без неё цифры выглядят взятыми с потолка.
    """
    ppk = f" ({round(goal.protein_g / weight_kg, 1)} г/кг)" if weight_kg else ""
    fat_ppk = f" ({round(goal.fat_g / weight_kg, 2)} г/кг)" if weight_kg else ""
    rows = (
        f'<tr><td>🔥 Ккал</td><td align="right"><b>{n(goal.daily_kcal)}</b></td></tr>'
        f'<tr><td>🥩 Белок</td><td align="right"><b>{n(goal.protein_g)}</b> г{ppk}</td></tr>'
        f'<tr><td>🧈 Жир</td><td align="right">{n(goal.fat_g)} г{fat_ppk}</td></tr>'
        f'<tr><td>🍞 Углеводы</td><td align="right">{n(goal.carb_g)} г</td></tr>'
    )
    return (
        f"<h3>🎯 Цель · {TARGET_RU.get(goal.target, goal.target)}</h3>"
        f"<table striped>{rows}</table>"
        "<p>Белок — обязательство: якорь на твой вес, от калорий не зависит. Жир — минимум, не "
        "ниже 0.8 г/кг. Углеводы — сдача: что осталось после белка и жира.</p>"
        "<p>Меняешь калории — двигаются макросы. Меняешь макросы — двигаются калории:</p>"
        "<p><code>/goal 1800</code> — калории, разложу сам<br>"
        f"<code>/goal {n(goal.protein_g)} {n(goal.fat_g)} {n(goal.carb_g)}</code> — Б Ж У, "
        "калории посчитаю</p>"
        "<footer>Цель — кнопками ниже. Пересчитать всё по себе — ⚙️ Профиль.</footer>"
    )


def products(items: list[Product], query: str | None = None) -> str:
    """Каталог с AI-оценкой. Единственное место, где чинится СИСТЕМАТИЧЕСКАЯ ошибка плотности
    (пиннится при первом появлении, INV-2 — если врёт, будет врать в каждой будущей записи)."""
    if not items:
        if query:  # искали конкретное и не нашли — это не то же самое, что «всё точно»
            return (
                f"<h3>🧾 Поиск: {e(query)}</h3><p>Ничего не нашёл. Попробуй короче — «тофу» "
                "вместо «тофу молодой»: ищу по части названия.</p>"
            )
        return (
            "<h3>🧾 Продукты</h3><p>Пока всё точно — здесь появятся продукты, где я прикинул "
            "КБЖУ сам (≈).</p>"
        )
    rows = "".join(
        f"<tr><td>{e(_short(p.display_name))}</td>"
        f'<td align="right">{n(p.density.kcal_per_100g)}</td>'
        f'<td align="right">Б{n(p.density.protein_per_100g)} Ж{n(p.density.fat_per_100g)} '
        f"У{n(p.density.carb_per_100g)}</td></tr>"
        for p in items
    )
    head = f"🧾 Поиск: {e(query)}" if query else "🧾 Продукты · моя оценка (≈)"
    # Без поиска показываются только 10 последних — иначе про поиск никто не узнает и старый
    # продукт останется «непочинябельным» (ровно баг-репорт #26).
    hint = (
        "КБЖУ на 100 г. Правь тот, что ешь чаще — он даёт больше всего калорий."
        if query
        else (
            "КБЖУ на 100 г. Здесь 10 последних; остальное — "
            "<code>/products тофу</code> по названию."
        )
    )
    return (
        f"<h3>{head}</h3><p>{hint} Кнопка ниже открывает правку.</p>"
        f"<table bordered striped>{rows}</table>"
    )


# ---------- экраны произвольного дня и разбора ввода ----------


def day(dv: DayView, rem: Remaining) -> str:
    """Один день истории (стрелки ← →). НЕ сегодня-центричный: без якоря и советов — это прошлое.

    ПЛОСКИЙ список: строка на КАЖДУЮ позицию с её временем «время · продукт — ккал». Заголовка-
    подытога приёма («13:37 · 852 ккал») НЕТ намеренно: владелец логирует по одному продукту, а не
    составными приёмами, поэтому подытог у одиночной позиции дублировал бы её же (11:56 · 325 ккал →
    «Фо бо 325 ккал» — одно и то же дважды). Время у позиции = время её приёма.
    """
    title = _day_title(dv)
    if not dv.meals:
        return f"<h3>🗒 {title}</h3><p>Ничего не записано.</p>"
    lines = "".join(
        f"<li>{tg_time(m.eaten_at, dv.timezone)} · {_item_mark(it)}{e(it.name_snapshot)} — "
        f"{kit.g(it.grams)}<b>{n(it.macros.kcal)}</b> ккал</li>"
        for m in dv.meals
        for it in m.items
    )
    return f"<h3>🗒 {title}</h3><ul>{lines}</ul><hr/>" + _bars(rem.consumed, rem.goal)


def meal_deleted(meal: Meal, tz_name: str) -> str:
    """«↩️ Отменить всё» под карточкой (FR-41): карточка не исчезает, а говорит, что отменено, —
    под ней «⏪ Вернуть». Раньше её удаляли, и вернуть было не из чего."""
    names = ", ".join(e(i.name_snapshot) for i in meal.items)
    return (
        f"<h3>🗑 Отменил приём · {tg_time(meal.eaten_at, tz_name)}</h3>"
        f"<p>{names} — <b>{n(meal.totals.kcal)}</b> ккал.</p>"
    )


def item_removed(r: ItemRemoved) -> str:
    """Коррекция «удали X»: что убрали + обновлённый дневной прогресс всеми барами."""
    names = ", ".join(e(i.name_snapshot) for i in r.items)
    kcal = sum(i.macros.kcal for i in r.items)
    return (
        f"<h3>🗑 Убрал</h3><p>{names} — минус {n(kcal)} ккал.</p><hr/>"
        + _bars(r.remaining.consumed, r.remaining.goal)
    )


def item_adjusted(r: ItemAdjusted) -> str:
    """Коррекция «поправь граммы»: было→стало + обновлённый прогресс."""
    it = r.item
    return (
        f"<h3>✏️ Поправил</h3><p>{e(it.name_snapshot)}: {n(r.old_grams)} → {n(it.grams)} г "
        f"({n(it.macros.kcal)} ккал).</p><hr/>"
        + _bars(r.remaining.consumed, r.remaining.goal)
    )


def label(r: LabelRead) -> str:
    """Прочитанная этикетка: КБЖУ на 100 г таблицей + вопрос про вес порции."""
    it = r.draft.items[r.item_index]
    pkg = f" · пачка {n(it.unit_weight_g)} г" if it.unit_weight_g else ""
    hint = "число, или жми «вся пачка»." if it.unit_weight_g else "число."
    warn = (
        "<p>⚠️ Б/Ж/У не сходятся с калориями — возможно, я ошибся при чтении. Проверь "
        "или переснимь чётче.</p>"
        if r.atwater_warning
        else ""
    )
    rows = (
        f'<tr><td>🔥 Ккал</td><td align="right"><b>{n(it.kcal_per_100g)}</b></td></tr>'
        f'<tr><td>🥩 Белок</td><td align="right">{n(it.protein_per_100g)} г</td></tr>'
        f'<tr><td>🧈 Жир</td><td align="right">{n(it.fat_per_100g)} г</td></tr>'
        f'<tr><td>🍞 Углеводы</td><td align="right">{n(it.carb_per_100g)} г</td></tr>'
    )
    return (
        f"<h3>📋 Этикетка: {e(it.name)}{pkg}</h3>"
        "<p>На 100 г:</p>"
        f"<table striped>{rows}</table>" + warn + f"<p>Сколько грамм съел? Напиши {hint}</p>"
    )


# ---------- меню-экраны (start / help): тоже единый стиль ----------


def start(*, onboarded: bool) -> str:
    tail = (
        ""
        if onboarded
        else "<p>Настроим твою <b>дневную норму</b> за 30 секунд — так остаток будет точным. "
        "Кнопка ⚙️ Профиль внизу.</p>"
    )
    return (
        "<h3>👋 Привет! Я считаю КБЖУ</h3>"
        "<p>Просто расскажи, что съел — текстом или голосом:</p>"
        "<ul><li>«куриная грудка 250 г»</li><li>«два банана»</li>"
        "<li>«фо бо средняя порция»</li></ul>"
        "<p>📷 Фото блюда или этикетки (вес тарелки — подписью) · ⚖️ «вес 86.8» · "
        "📊 «сколько осталось»</p>"
        + tail
        + "<footer>Кнопки меню — снизу. Полная инструкция — /help</footer>"
    )


def help_() -> str:
    return (
        "<h3>📖 Как пользоваться</h3>"
        "<p>Считаю калории и БЖУ по твоим сообщениям и показываю, сколько осталось до цели. "
        "Пиши свободно — разберу сам.</p>"
        "<details><summary>🍽 Записать еду</summary>"
        "<ul><li>«куриная грудка 250 г» — точный вес</li>"
        "<li>«два банана», «3 яйца» — штуки</li>"
        "<li>«фо бо», «шакшука» — хватит названия блюда</li>"
        "<li>🎤 голосом — наговори как удобно</li>"
        "<li>⚡ Частое — свои наборы и частая еда в один тап</li></ul></details>"
        "<details><summary>📷 Фото и цифры</summary>"
        "<p>Сфоткай тарелку — разложу блюдо на составляющие и спрошу про масло. Взвесил — "
        "напиши вес подписью к фото («420»): так точнее.</p>"
        "<p>Бланк InBody — тоже фото: сверю цифры, сохраню замер и предложу цели по составу "
        "тела.</p>"
        "<p>Пришли снимок таблицы КБЖУ — прочитаю сам, спрошу только вес. Знаешь цифры из меню? "
        "Назови прямо: «рис из кафе, 400 ккал, 30 белка, 10 жира, 50 углеводов».</p></details>"
        "<details><summary>✏️ Исправить</summary>"
        "<ul><li>«удали рис» — уберу позицию</li>"
        "<li>↩️ Отменить — снять последнюю запись или продукт из неё</li>"
        "<li>/products — поправить КБЖУ, если оценил неточно (≈)</li></ul></details>"
        "<details><summary>📊 Посмотреть</summary>"
        "<ul><li>📊 Сегодня · 📈 Неделя · 🗒 История</li>"
        "<li>⚖️ Вес · 🎯 Цель · /month · /streak</li></ul></details>"
        "<details><summary>🎯 Настроить</summary>"
        "<ul><li>⚙️ Профиль — пол, вес, рост, возраст, цель → посчитаю норму</li>"
        "<li><code>/goal 1800</code> — задать калории, макросы разложу под твою цель</li>"
        "<li><code>/goal 180 80 200</code> — задать Б Ж У, калории посчитаю сам</li>"
        "<li>🎯 Цель — сменить похудение/поддержание/набор кнопкой</li>"
        "<li>/timezone — часовой пояс, чтобы «день» совпадал с твоим</li></ul></details>"
        "<footer>Чем точнее профиль и каталог — тем точнее учёт.</footer>"
    )


# ---------- мелкие помощники ----------


def _item_mark(it: MealItem) -> str:
    """Метка позиции (INV-13): ≈ AI-оценка, 📋 цифры юзера (не из каталога и не оценка)."""
    if it.is_estimate:
        return "≈ "
    if it.product_id is None:  # названо юзером
        return "📋 "
    return ""


def _notes(r: MealLogged) -> str:
    """Сноски-легенды под карточкой приёма (только те, что реально встретились)."""
    notes = []
    if any(it.is_estimate for it in r.meal.items):
        notes.append("≈ — прикинул сам, приблизительно. Неверно? /products")
    if any(it.product_id is None and not it.is_estimate for it in r.meal.items):
        notes.append("📋 — по твоим цифрам (меню/этикетка)")
    if r.meal.source_confidence < 0.6:
        notes.append("⚠️ не уверен в распознавании — проверь")
    return f"<footer>{'<br>'.join(notes)}</footer>" if notes else ""


def _day_title(dv: DayView) -> str:
    label_ = "Сегодня" if dv.days_ago == 0 else "Вчера" if dv.days_ago == 1 else ""
    ds = f"{dv.day.day} {kit.MON[dv.day.month - 1]}"
    return f"{label_}, {ds}" if label_ else ds


def _seg_for(status: str) -> tuple[str, str]:
    """Сегмент-эмодзи под статус клетки хитмапа — те же кастом-эмодзи, что в баре."""
    return {
        "none": (kit.SEG_EMPTY, "⬜"),
        "ok": (kit.SEG_OK, "🟩"),
        "low": (kit.SEG_LOW, "🟨"),
        "over": (kit.SEG_OVER, "🟥"),
    }[status]


__all__ = [
    "day",
    "evening",
    "goal_screen",
    "help_",
    "item_adjusted",
    "item_removed",
    "label",
    "meal_card",
    "month",
    "products",
    "start",
    "streak",
    "today",
    "week",
    "weight",
]
