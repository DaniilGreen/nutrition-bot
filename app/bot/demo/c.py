"""ДИЗАЙН C — «Лента». День как хронология, а не как остаток.

ЭТО НЕ ПЕРЕКРАСКА A И B, А ТРЕТЬЯ ИНФОРМАЦИОННАЯ АРХИТЕКТУРА.
A и B отвечают на вопрос «сколько осталось». C отвечает на «как идёт день»: приёмы по времени,
накопительный итог после каждого и главное — РАСПРЕДЕЛЕНИЕ БЕЛКА ПО ПРИЁМАМ.

ЗАЧЕМ БЕЛОК ПО ПРИЁМАМ. Расклад «завтрак 12 г · обед 38 · ужин 90» ломает день не теорией про
синтез белка (её эффект второго порядка, и врать о его величине нечестно), а простой логистикой:
к вечеру калорийный люфт уже съеден углеводами и жиром, и 90 г белка ФИЗИЧЕСКИ НЕ ВЛЕЗАЮТ. День
закрывается недобором, а человек не понимает почему. Лента показывает это ДО того, как поздно.

Порог на приём — 0.3 г/кг массы (для 88 кг ≈ 26 г). Подсветка нейтральная: это не двойка, это
подсказка перенести часть белка на утро.
"""
from __future__ import annotations

from app.bot.demo import blocks, kit
from app.bot.demo.data import DemoData, day_status
from app.bot.demo.kit import e, n, pct, tg_time

PROTEIN_PER_MEAL_RATIO = 0.3  # г/кг массы на приём — ниже этого приём «пустой» по белку
MEAL_MIN_KCAL = 300  # порог белка применяем только к существенному приёму, не к чаю и гарниру
MIN_MEALS_FOR_SPREAD = 3  # «распределение» из одного-двух чисел — не распределение
_ICONS = {"none": "▫️", "ok": "🟢", "low": "🟡", "over": "🔴"}


def _short(name: str, limit: int = 16) -> str:
    """Обрезка с многоточием: молча урезанное имя искажает продукт на экране, где его же чинят."""
    return name if len(name) <= limit else name[: limit - 1] + "…"


def _meal_threshold(d: DemoData) -> float:
    return d.weight_kg * PROTEIN_PER_MEAL_RATIO if d.weight_kg else 25.0


def _feed(d: DemoData) -> str:
    """Лента приёмов с накопительным итогом. Видно, КАК набирался день, а не только результат."""
    g = d.goal
    thr = _meal_threshold(d)
    acc = 0.0
    out = []
    for m in d.today.meals:
        acc += m.totals.kcal
        names = ", ".join(f"{'≈' if it.is_estimate else ''}{e(it.name_snapshot)}" for it in m.items)
        # Порог применим только к существенному приёму: у риса и чая белка не бывает,
        # и метка «мало белка» на гарнире — капитан очевидность.
        low = " · мало белка" if m.totals.kcal >= MEAL_MIN_KCAL and m.totals.protein < thr else ""
        out.append(
            f"<blockquote>{tg_time(m.eaten_at, d.today.timezone)} · {names}<br>"
            f"<b>{n(m.totals.kcal)}</b> ккал · белок <b>{n(m.totals.protein)}</b> г{low}"
            f"<cite>всего за день: {n(acc)} / {n(g.daily_kcal)} ккал</cite></blockquote>"
        )
    return "".join(out)


def _protein_spread(d: DemoData) -> str:
    """Распределение белка по приёмам — сердце этого дизайна.

    ДВА ПРАВИЛА, БЕЗ КОТОРЫХ БЛОК ВРЁТ:
    1. Распределения из одного-двух чисел не бывает. При <3 приёмах молчим.
    2. Совет обязан смотреть, КАКОЙ приём слабый. Старый шаблон писал «перенеси белок на утро»
       даже когда провален был УЖИН (а завтрак с обедом отличные) — и даже когда единственный
       приём БЫЛ утренним. Это не совет, это подстановка.
    """
    meals = [m for m in d.today.meals if m.totals.kcal >= MEAL_MIN_KCAL]
    if len(meals) < MIN_MEALS_FOR_SPREAD:
        return ""
    thr = _meal_threshold(d)
    parts = " · ".join(n(m.totals.protein) for m in meals)
    head = f"<p>🥩 Белок по приёмам: <b>{parts}</b> г (порог {n(thr)} г)</p>"

    weak = [i for i, m in enumerate(meals) if m.totals.protein < thr]
    if not weak:
        return head + "<p>Распределён ровно — день закроется без ужина-монстра.</p>"
    early = [i for i in weak if i < len(meals) // 2]  # слабые приёмы в первой половине дня
    where = "в начале дня" if early else "к вечеру"
    tip = (
        "Добавь белка в ранние приёмы — вечером он уже не влезет в остаток калорий."
        if early
        else "Ранние приёмы вытянули день, но вечер провис — на нём и добирай."
    )
    return head + f"<p>Провисли приёмы {where}: {len(weak)}. {tip}</p>"



def _title(d: DemoData) -> str:
    """Заголовок дня. Гард на пустой days: падение = юзер видит «что-то пошло не так»."""
    return kit.day_ru(d.days[-1][0]) if d.days else "Сегодня"

def today(d: DemoData) -> str:
    c, g = d.today.remaining.consumed, d.goal
    if not d.today.meals:
        return (
            "<h3>📊 Лента дня</h3><p>День ещё не начался. Запиши первый приём.</p>"
            + blocks.anchor(d)
        )
    return (
        f"<h3>📊 Лента дня · {n(c.kcal)} / {n(g.daily_kcal)} ккал</h3>"
        + _feed(d)
        + "<hr/>"
        + blocks.macro_block("Ккал", c.kcal, g.daily_kcal)
        + blocks.macro_block("Белок", c.protein, g.protein_g)
        + _protein_spread(d)
        + blocks.advice(d)
        + blocks.anchor(d)
    )


def meal(d: DemoData) -> str:
    m = d.last_meal
    if m is None:
        return "<h3>✅ Приём</h3><p>Сегодня приёмов ещё нет.</p>"
    c, g = d.today.remaining.consumed, d.goal
    thr = _meal_threshold(d)
    lis = []
    for it in m.items:
        mark = "≈ " if it.is_estimate else ""
        grams = f"{n(it.grams)} г · " if it.grams else ""
        lis.append(f"<li>{mark}{e(it.name_snapshot)} — {grams}<b>{n(it.macros.kcal)}</b> ккал</li>")
    note = (
        f"Белок {n(m.totals.protein)} г — ниже порога {n(thr)} г на приём"
        if m.totals.protein < thr
        else f"Белок {n(m.totals.protein)} г — приём полноценный"
    )
    return (
        f"<h3>✅ {tg_time(m.eaten_at, d.today.timezone)} · +{n(m.totals.kcal)} ккал</h3>"
        f"<ul>{''.join(lis)}</ul>"
        f"<p>{note}. Вклад в день: {pct(m.totals.kcal, g.daily_kcal)}% калорий.</p>"
        "<hr/>"
        + blocks.macro_block("Ккал", c.kcal, g.daily_kcal)
        + blocks.macro_block("Белок", c.protein, g.protein_g)
        + blocks.clarify(d)
        + blocks.advice(d)
    )


def week(d: DemoData) -> str:
    """Неделя как лента дней: волатильность читается глазом. «Недоел → сорвался» видно сразу."""
    w = d.week7
    if not w.days_logged:
        return "<h3>📈 Неделя</h3><p>За 7 дней записей нет.</p>"
    lis = "".join(
        f"<li>{_ICONS[day_status(kcal, d.goal.daily_kcal)]} <b>{kit.day_ru(day)}</b> — "
        f"{n(kcal) if kcal else '—'} ккал</li>"
        for day, kcal in reversed(d.days)
    )
    a = w.avg_macros
    return (
        f"<h3>📈 7 дней · {n(a.kcal)} ккал в среднем</h3>"
        f"<ul>{lis}</ul>"
        + blocks.macro_block("Ккал", a.kcal, d.goal.daily_kcal)
        + blocks.macro_block("Белок", a.protein, d.goal.protein_g)
        + f"<p>Записано {w.days_logged} из 7 · в цели {w.days_in_goal}</p>"
        + "<footer>Ровная линия бьёт идеальные дни.</footer>"
    )


def day(d: DemoData) -> str:
    if not d.today.meals:
        return "<h3>🗒 День</h3><p>Ничего не записано.</p>"
    return (
        f"<h3>🗒 {_title(d)}</h3>"
        + _feed(d)
        + "<hr/>"
        + _protein_spread(d)
        + blocks.culprit(d)
        + blocks.accuracy(d)
        + blocks.clarify(d)
    )


def products(d: DemoData) -> str:
    """Каталог по белковой эффективности (г белка на ккал) — это ровно та величина, по которой
    алгоритм добора решает, чем дешевле закрыть белок."""
    if not d.products:
        return "<h3>🧾 Продукты</h3><p>Продуктов с моей оценкой нет — всё точно.</p>"
    ranked = sorted(
        d.products,
        key=lambda p: -(p.density.protein_per_100g / max(p.density.kcal_per_100g, 1)),
    )
    rows = []
    for p in ranked:
        den = p.density
        ppk = den.protein_per_100g / den.kcal_per_100g if den.kcal_per_100g else 0
        rows.append(
            f"<tr><td>{e(_short(p.display_name))}</td>"
            f'<td align="right">{n(den.kcal_per_100g)}</td>'
            f'<td align="right">{ppk:.2f}</td></tr>'
        )
    return (
        "<h3>🧾 Продукты · белок на калорию</h3>"
        f"<table bordered striped>{''.join(rows)}</table>"
        "<p>Третья колонка — граммов белка на 1 ккал: чем больше, тем дешевле продукт закрывает "
        "белок. Тунец в с/с ≈ 0.23, сыр 45% ≈ 0.07.</p>"
        "<footer>Плотность фиксируется навсегда — ошибка тут врёт в каждой будущей записи.</footer>"
    )


def weight(d: DemoData) -> str:
    logs = d.weight.logs
    if not logs:
        return "<h3>⚖️ Вес</h3><p>Не записан. Напиши «вес 86.8».</p>"
    if not d.trend_ok:
        stale = d.days_since_weigh_in or 0
        why = (
            f"последний замер {stale} дн. назад — тренд протух"
            if stale > 10
            else f"замеров всего {len(logs)} — линии пока нет"
        )
        return f"<h3>⚖️ Вес</h3><p><b>{logs[0].weight_kg:.1f} кг</b> · {why}.</p>"
    lis = "".join(
        f"<li>{log.measured_at.strftime('%d.%m')} · <b>{log.weight_kg:.1f}</b> кг</li>"
        for log in logs[:10]
    )
    return (
        f"<h3>⚖️ Тренд {d.trend_kg} кг</h3>"
        f"<blockquote>Последнее взвешивание {logs[0].weight_kg:.1f} кг"
        f"<cite>{len(logs)} замеров</cite></blockquote>"
        f"<ul>{lis}</ul>"
        "<footer>Вода и содержимое ЖКТ двигают вес на ±1–1.5 кг за сутки. Тренд сглаживает это "
        "и показывает, что происходит на самом деле.</footer>"
    )


def goal(d: DemoData) -> str:
    g = d.goal
    ppk = f" · {round(g.protein_g / d.weight_kg, 1)} г/кг" if d.weight_kg else ""
    thr = _meal_threshold(d)
    return (
        f"<h3>🎯 {n(g.daily_kcal)} ккал в день</h3>"
        f"<blockquote>Белок {n(g.protein_g)} г{ppk}<cite>обязательство, а не пожелание</cite>"
        "</blockquote>"
        f"<p>Жир {n(g.fat_g)} г — минимум. Углеводы {n(g.carb_g)} г — сдача.</p>"
        f"<p>В пересчёте на приёмы: клади <b>{n(thr)}+ г белка</b> в каждый. Тогда вечер не "
        "превращается в попытку впихнуть 90 г белка в 300 оставшихся ккал.</p>"
        "<footer>Пересчитать — ⚙️ Профиль. Вручную — /goal 1800</footer>"
    )


def evening(d: DemoData) -> str:
    c, g = d.today.remaining.consumed, d.goal
    if not d.today.meals:
        return "<h3>🌙 День закрыт</h3><p>Ничего не записано. Кинь текстом, что ел.</p>"
    return (
        f"<h3>🌙 День закрыт · {n(c.kcal)} / {n(g.daily_kcal)} ккал</h3>"
        + _feed(d)
        + "<hr/>"
        + _protein_spread(d)
        + blocks.over_note(d)
        + blocks.advice(d)
        + blocks.anchor(d)
    )


def month(d: DemoData) -> str:
    m = d.month
    if not m.days_logged:
        return "<h3>📅 Месяц</h3><p>За 30 дней записей нет.</p>"
    a = m.avg_macros
    dw = f"{m.weight_delta:+.1f} кг" if m.weight_delta is not None else "нет данных"
    return (
        f"<h3>📅 30 дней · {n(a.kcal)} ккал в среднем</h3>"
        f"<blockquote>Вес за период: {dw}"
        f"<cite>записано {m.days_logged} из {m.total_days} дней</cite></blockquote>"
        + blocks.macro_block("Ккал", a.kcal, d.goal.daily_kcal)
        + blocks.macro_block("Белок", a.protein, d.goal.protein_g)
        + blocks.coverage_block(d)
        + "<footer>Провал — это не перебор. Провал — это не записать.</footer>"
    )


SECTIONS = (
    ("Сегодня", today),
    ("Приём записан", meal),
    ("Неделя", week),
    ("День", day),
    ("Продукты", products),
    ("Вес", weight),
    ("Цель", goal),
    ("Итог дня", evening),
    ("Месяц", month),
    ("Ветки", lambda d: blocks.branches(d)),  # демо-витрина: см. blocks.branches
)


def build(d: DemoData) -> list[tuple[str, str]]:
    return [(title, fn(d)) for title, fn in SECTIONS]
