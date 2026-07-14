"""ДИЗАЙН A — «Дашборд». Всё видно сразу, ноль кликов.

ФИЛОСОФИЯ: показать максимум правды на одном экране и не заставлять человека тыкать.
Четыре бара — по одному на макрос, как и просил владелец («главное — прогресс по макросам»).

ЧЕСТНЫЙ РИСК ЭТОГО ДИЗАЙНА (нутрициолог прав, и это надо знать, выбирая):
бар — это обязательство. Четыре одновременных обязательства арифметически невыполнимы разом
(закрыл белок → пробил жир), и человек живёт с ощущением вечного провала. Дизайн B решает это,
оставляя бары только ккал и белку. Здесь — сознательный размен: плотность против покоя.
"""
from __future__ import annotations

from app.bot.demo import blocks, kit
from app.bot.demo.data import DemoData
from app.bot.demo.kit import e, n, pct, tg_time, verdict


def _title(d: DemoData) -> str:
    """Заголовок дня. Гард на пустой days: падение = юзер видит «что-то пошло не так»."""
    return kit.day_ru(d.days[-1][0]) if d.days else "Сегодня"

def today(d: DemoData) -> str:
    c, g = d.today.remaining.consumed, d.goal
    if not d.today.meals:
        return (
            "<h3>📊 Сегодня</h3><p>Пока пусто. Запиши приём — «куриная грудка 250 г», "
            "голосом или фото этикетки.</p>" + blocks.anchor(d)
        )
    bars = "".join(blocks.macro_block(lab, cons, goal) for lab, cons, goal in kit.macros_rows(c, g))
    return (
        "<h3>📊 Сегодня</h3>"
        f"<p><i>{verdict(c, g)}</i></p>"
        + bars
        + blocks.anchor(d)
        + blocks.accuracy(d)
        + blocks.over_note(d)
        + blocks.advice(d)
        + blocks.eaten(d, open_=True)
    )


def meal(d: DemoData) -> str:
    """Карточка сразу после записи — самый горячий экран продукта.

    Внимание на 100%, человек ещё у тарелки: уточнить вес стоит 5 секунд. Через час — уже никогда.
    """
    m = d.last_meal
    if m is None:
        return "<h3>✅ Приём записан</h3><p>Сегодня приёмов ещё нет — записывать нечего.</p>"
    c, g = d.today.remaining.consumed, d.goal
    lis = []
    for it in m.items:
        mark = "≈ " if it.is_estimate else ""
        grams = f"{n(it.grams)} г · " if it.grams else ""
        lis.append(
            f"<li>{mark}{e(it.name_snapshot)} — {grams}<b>{n(it.macros.kcal)}</b> ккал · "
            f"Б{n(it.macros.protein)} Ж{n(it.macros.fat)} У{n(it.macros.carb)}</li>"
        )
    return (
        f"<h3>✅ Записал · {tg_time(m.eaten_at, d.today.timezone)}</h3>"
        f"<ul>{''.join(lis)}</ul>"
        f"<p>Приём: <b>{n(m.totals.kcal)} ккал</b> ({pct(m.totals.kcal, g.daily_kcal)}% цели) · "
        f"Б{n(m.totals.protein)} Ж{n(m.totals.fat)} У{n(m.totals.carb)}</p>"
        "<hr/>"
        + blocks.macro_block("Ккал", c.kcal, g.daily_kcal)
        + blocks.macro_block("Белок", c.protein, g.protein_g)
        + blocks.clarify(d)
        + blocks.advice(d)
    )


def week(d: DemoData) -> str:
    w = d.week7
    if not w.days_logged:
        return "<h3>📈 Неделя</h3><p>За 7 дней записей нет.</p>"
    a, g = w.avg_macros, d.goal
    bars = "".join(blocks.macro_block(lab, cons, goal) for lab, cons, goal in kit.macros_rows(a, g))
    return (
        "<h3>📈 Неделя · в среднем за день</h3>"
        f"<p><i>{verdict(a, g)}</i></p>"
        + bars
        + f"<p>Дней в цели: <b>{w.days_in_goal}</b> из {w.days_logged} записанных</p>"
        + blocks.week_table(d)
        + "<footer>Один день — шум. Управлять надо этой средней, а не вчерашним ужином.</footer>"
    )


def day(d: DemoData) -> str:
    c, g = d.today.remaining.consumed, d.goal
    if not d.today.meals:
        return "<h3>🗒 День</h3><p>Ничего не записано.</p>"
    return (
        f"<h3>🗒 {_title(d)}</h3>"
        + blocks.eaten(d, open_=True)
        + blocks.culprit(d)
        + blocks.accuracy(d)
        + blocks.clarify(d)
        + "<hr/>"
        + "".join(blocks.macro_block(lab, cons, goal) for lab, cons, goal in kit.macros_rows(c, g))
    )


def products(d: DemoData) -> str:
    """Каталог. Единственное место, где чинится СИСТЕМАТИЧЕСКАЯ ошибка: плотность пиннится при
    первом появлении продукта (INV-2) — значит, если она врёт, она врёт потом каждый раз."""
    if not d.products:
        return "<h3>🧾 Продукты</h3><p>Всё посчитано точно — продуктов с моей оценкой нет.</p>"
    rows = []
    for p in d.products:
        den = p.density
        rows.append(
            f"<tr><td>{e(p.display_name[:16])}</td>"
            f'<td align="right">{n(den.kcal_per_100g)}</td>'
            f'<td align="right">{n(den.protein_per_100g)}·'
            f"{n(den.fat_per_100g)}·{n(den.carb_per_100g)}</td></tr>"
        )
    return (
        "<h3>🧾 Продукты · моя оценка (≈)</h3>"
        "<p>КБЖУ на 100 г. Плотность фиксируется навсегда — если врёт, будет врать всегда.</p>"
        f"<table bordered striped>{''.join(rows)}</table>"
        "<footer>Правь тот, что ешь чаще: он даёт больше всего калорий.</footer>"
    )


def weight(d: DemoData) -> str:
    logs = d.weight.logs
    body = blocks.weight_block(d)
    if len(logs) >= 2:
        lis = "".join(
            f"<li>{log.measured_at.strftime('%d.%m')} · <b>{log.weight_kg}</b> кг</li>"
            for log in logs[:8]
        )
        body += f"<details><summary>📋 Замеры</summary><ul>{lis}</ul></details>"
    return "<h3>⚖️ Вес</h3>" + body


def goal(d: DemoData) -> str:
    g = d.goal
    ppk = f" ({round(g.protein_g / d.weight_kg, 1)} г/кг)" if d.weight_kg else ""
    return (
        "<h3>🎯 Дневная цель</h3>"
        "<table striped>"
        f'<tr><td>Ккал</td><td align="right"><b>{n(g.daily_kcal)}</b></td></tr>'
        f'<tr><td>Белок</td><td align="right"><b>{n(g.protein_g)}</b> г{ppk}</td></tr>'
        f'<tr><td>Жир</td><td align="right">{n(g.fat_g)} г</td></tr>'
        f'<tr><td>Углеводы</td><td align="right">{n(g.carb_g)} г</td></tr>'
        "</table>"
        "<p>Белок — обязательство. Жир — минимум. Углеводы — сдача: то, что осталось после "
        "белка и жира.</p>"
        "<footer>Пересчитать — ⚙️ Профиль. Вручную — /goal 1800</footer>"
    )


def evening(d: DemoData) -> str:
    """Вечерний пуш. Асимметрия: при недоборе — действие (ещё исполнимо), при переборе —
    контекст и явное «ничего не делай»."""
    c, g = d.today.remaining.consumed, d.goal
    if not d.today.meals:
        return "<h3>🌙 Итог дня</h3><p>Сегодня ничего не записано. Кинь текстом, что ел.</p>"
    if c.kcal < g.daily_kcal * 0.4:
        return (
            "<h3>🌙 Итог дня</h3>"
            f"<p>Записано {n(c.kcal)} ккал — похоже, ужин не попал в бота. Кинь текстом.</p>"
            + blocks.anchor(d)
        )
    return (
        "<h3>🌙 Итог дня</h3>"
        + blocks.macro_block("Ккал", c.kcal, g.daily_kcal)
        + blocks.macro_block("Белок", c.protein, g.protein_g)
        + blocks.over_note(d)
        + blocks.advice(d)
        + blocks.anchor(d)
    )


def month(d: DemoData) -> str:
    m = d.month
    if not m.days_logged:
        return "<h3>📅 Месяц</h3><p>За 30 дней записей нет.</p>"
    a, g = m.avg_macros, d.goal
    dw = f"{m.weight_delta:+.1f} кг" if m.weight_delta is not None else "—"
    rows = (
        f'<tr><td>Ккал/день</td><td align="right"><b>{n(a.kcal)}</b>'
        f" / {n(g.daily_kcal)}</td></tr>"
        f'<tr><td>Белок/день</td><td align="right">{n(a.protein)} / {n(g.protein_g)} г</td></tr>'
        f'<tr><td>Вес за период</td><td align="right">{dw}</td></tr>'
    )
    return (
        "<h3>📅 30 дней</h3>"
        + blocks.coverage_block(d)
        + f"<table striped>{rows}</table>"
        + blocks.weight_block(d)
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
