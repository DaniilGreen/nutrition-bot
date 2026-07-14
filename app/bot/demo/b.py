"""ДИЗАЙН B — «Тренер». Сначала ответ, детали — по клику.

ФИЛОСОФИЯ: экран открывают, чтобы решить «что положить в тарелку», а не любоваться цифрами.
Значит сверху — вердикт и действие, всё остальное под <details>.

ГЛАВНОЕ ОТЛИЧИЕ ОТ A — БАРЫ ТОЛЬКО НА ККАЛ И БЕЛОК.
Бар читается как обязательство. Четыре бара = четыре обязательства, которые арифметически нельзя
выполнить одновременно: закрыл белок — пробил жир; влез в жир — не добрал углеводы. Человек живёт
в состоянии вечного провала и в итоге бросает записывать. Поэтому:
  * Ккал  — бар (бюджет дня);
  * Белок — бар (единственный макрос, недобор которого при дефиците реально бьёт по мышцам);
  * Жир   — МЕТКА ПОЛА, не цель: важно не провалиться ниже (гормоны, жирорастворимые витамины);
  * Углеводы — просто число: это сдача после белка и жира, задачей они не являются никогда.
"""
from __future__ import annotations

from app.bot.demo import blocks, kit
from app.bot.demo.data import DemoData
from app.bot.demo.kit import e, n, pct, tg_time

FAT_FLOOR_RATIO = 0.8  # пол жира ≈ 80% цели: ниже страдают гормоны и жирорастворимые витамины


def _hero(title: str, sub: str) -> str:
    """Выносная цитата — главный ответ экрана. Первое, что видит глаз."""
    return f"<aside>{title}<cite>{sub}</cite></aside>"


def _fat_carb(d: DemoData) -> str:
    """Жир — метка пола. Углеводы — число. Ни у того, ни у другого бара нет и не будет."""
    c, g = d.today.remaining.consumed, d.goal
    floor = g.fat_g * FAT_FLOOR_RATIO
    note = "ниже пола" if c.fat < floor else "в порядке"
    return (
        f"<p>Жир {n(c.fat)} г · пол {n(floor)} г — {note}<br>"
        f"Углеводы {n(c.carb)} г — сдача, задачей не являются</p>"
    )



def _title(d: DemoData) -> str:
    """Заголовок дня. Гард на пустой days: падение = юзер видит «что-то пошло не так»."""
    return kit.day_ru(d.days[-1][0]) if d.days else "Сегодня"

def today(d: DemoData) -> str:
    c, g = d.today.remaining.consumed, d.goal
    if not d.today.meals:
        return (
            _hero("Сегодня пусто", "запиши первый приём")
            + "<p>«Куриная грудка 250 г», голосом или фото этикетки.</p>"
            + blocks.anchor(d)
        )
    left = g.daily_kcal - c.kcal
    sub = f"ещё {n(left)} ккал в бюджете" if left > 0 else f"перебор {n(-left)} ккал"
    return (
        _hero(kit.verdict(c, g), f"{n(c.kcal)} / {n(g.daily_kcal)} ккал · {sub}")
        + blocks.macro_block("Ккал", c.kcal, g.daily_kcal)
        + blocks.macro_block("Белок", c.protein, g.protein_g)
        + _fat_carb(d)
        + blocks.over_note(d)
        + blocks.advice(d)
        + blocks.anchor(d)
        + "<details><summary>🔍 Подробности</summary>"
        + blocks.accuracy(d)
        + blocks.protein_note(d)
        + blocks.culprit(d)
        + blocks.clarify(d)
        + "</details>"
        + blocks.eaten(d)
    )


def meal(d: DemoData) -> str:
    m = d.last_meal
    if m is None:
        return _hero("Приёмов нет", "сегодня ещё ничего не записано")
    c, g = d.today.remaining.consumed, d.goal
    lis = []
    for it in m.items:
        mark = "≈ " if it.is_estimate else ""
        grams = f"{n(it.grams)} г · " if it.grams else ""
        lis.append(f"<li>{mark}{e(it.name_snapshot)} — {grams}<b>{n(it.macros.kcal)}</b> ккал</li>")
    return (
        _hero(
            f"+{n(m.totals.kcal)} ккал · {n(m.totals.protein)} г белка",
            f"{pct(m.totals.kcal, g.daily_kcal)}% дневной цели · "
            f"{tg_time(m.eaten_at, d.today.timezone)}",
        )
        + f"<ul>{''.join(lis)}</ul>"
        + blocks.macro_block("Ккал", c.kcal, g.daily_kcal)
        + blocks.macro_block("Белок", c.protein, g.protein_g)
        + blocks.clarify(d)
        + blocks.advice(d)
    )


def week(d: DemoData) -> str:
    w = d.week7
    if not w.days_logged:
        return _hero("Неделя пуста", "записей за 7 дней нет")
    a, g = w.avg_macros, d.goal
    return (
        _hero(
            f"{n(a.kcal)} / {n(g.daily_kcal)} ккал в среднем",
            f"записано {w.days_logged} из 7 дней · в цели {w.days_in_goal}",
        )
        + blocks.macro_block("Ккал", a.kcal, g.daily_kcal)
        + blocks.macro_block("Белок", a.protein, g.protein_g)
        + "<p><i>Один день — шум. Эта средняя и есть «сколько я ем».</i></p>"
        + f"<details><summary>📅 По дням</summary>{blocks.week_table(d)}</details>"
    )


def day(d: DemoData) -> str:
    c, g = d.today.remaining.consumed, d.goal
    if not d.today.meals:
        return _hero("День пуст", "ничего не записано")
    return (
        _hero(_title(d), f"{n(c.kcal)} / {n(g.daily_kcal)} ккал")
        + blocks.culprit(d)
        + blocks.accuracy(d)
        + blocks.clarify(d)
        + blocks.eaten(d)
    )


def products(d: DemoData) -> str:
    if not d.products:
        return _hero("Каталог чист", "продуктов с моей оценкой нет")
    lis = "".join(
        f"<li><b>{e(p.display_name)}</b> — {n(p.density.kcal_per_100g)} ккал/100 г · "
        f"Б{n(p.density.protein_per_100g)} Ж{n(p.density.fat_per_100g)} "
        f"У{n(p.density.carb_per_100g)}</li>"
        for p in d.products
    )
    return (
        _hero(f"{len(d.products)} продуктов с оценкой", "плотность задал я, а не ты")
        + "<p>Плотность фиксируется навсегда: если она врёт — она будет врать каждый раз.</p>"
        + f"<details open><summary>🧾 Проверить</summary><ul>{lis}</ul></details>"
    )


def weight(d: DemoData) -> str:
    logs = d.weight.logs
    if not logs:
        return _hero("Вес не записан", "напиши «вес 86.8»")
    if d.trend_kg is None or len(logs) < 2:
        return _hero(f"{logs[0].weight_kg} кг", "одно взвешивание — тренда пока нет")
    lis = "".join(
        f"<li>{log.measured_at.strftime('%d.%m')} · <b>{log.weight_kg}</b> кг</li>"
        for log in logs[:8]
    )
    return (
        _hero(f"Тренд {d.trend_kg} кг", f"последнее взвешивание {logs[0].weight_kg} кг")
        + "<p><i>Одно взвешивание — шум: вода и содержимое ЖКТ дают ±1–1.5 кг за сутки. "
        "Линия — сигнал.</i></p>"
        + f"<details><summary>📋 Замеры</summary><ul>{lis}</ul></details>"
    )


def goal(d: DemoData) -> str:
    g = d.goal
    ppk = f"{round(g.protein_g / d.weight_kg, 1)} г/кг" if d.weight_kg else "—"
    return (
        _hero(f"{n(g.daily_kcal)} ккал в день", f"белок {n(g.protein_g)} г · {ppk}")
        + f"<p><b>Белок {n(g.protein_g)} г</b> — обязательство. Коридор при дефиците 1.6–2.2 г/кг: "
        "ниже организм добирает аминокислоты из мышц.</p>"
        + f"<p><b>Жир {n(g.fat_g)} г</b> — пол, а не цель. Ниже {n(g.fat_g * FAT_FLOOR_RATIO)} г "
        "страдают гормоны.</p>"
        + f"<p><b>Углеводы {n(g.carb_g)} г</b> — сдача: всё, что осталось после белка и жира. "
        "Не попал — ничего не случилось.</p>"
        + "<footer>Пересчитать — ⚙️ Профиль. Вручную — /goal 1800</footer>"
    )


def evening(d: DemoData) -> str:
    """Пуш — самая дорогая интервенция: приходит без спроса. Тратить его на констатацию того,
    на что уже нельзя повлиять, — расточительство. Поэтому ветвимся, и одна ветка — молчание."""
    c, g = d.today.remaining.consumed, d.goal
    if not d.today.meals:
        return _hero("Сегодня пусто", "кинь текстом, что ел") + blocks.anchor(d)
    if c.kcal < g.daily_kcal * 0.4:
        return (
            _hero("Похоже, не всё записано", f"{n(c.kcal)} ккал за день — маловато")
            + "<p>Кинь текстом, что ел. Дыра в логе хуже перебора: перебор — просто число, "
            "а пропуск — потерянные данные.</p>"
            + blocks.anchor(d)
        )
    adv = blocks.advice(d)
    if not adv and c.kcal <= g.daily_kcal:
        # Бот ОБЯЗАН уметь молчать. Похвала и конфетти тут были бы шумом.
        return (
            _hero("Всё закрыто", f"{n(c.kcal)} / {n(g.daily_kcal)} ккал · белок {n(c.protein)} г")
            + "<p>Делать ничего не надо.</p>"
            + blocks.anchor(d)
        )
    return (
        _hero(kit.verdict(c, g), f"{n(c.kcal)} / {n(g.daily_kcal)} ккал")
        + blocks.macro_block("Белок", c.protein, g.protein_g)
        + blocks.over_note(d)
        + adv
        + blocks.anchor(d)
    )


def month(d: DemoData) -> str:
    m = d.month
    if not m.days_logged:
        return _hero("Месяц пуст", "за 30 дней записей нет")
    a, g = m.avg_macros, d.goal
    dw = f"{m.weight_delta:+.1f} кг" if m.weight_delta is not None else "нет данных"
    return (
        _hero(f"{n(a.kcal)} ккал в среднем", f"вес за период: {dw}")
        + blocks.coverage_block(d)
        + blocks.macro_block("Ккал", a.kcal, g.daily_kcal)
        + blocks.macro_block("Белок", a.protein, g.protein_g)
        + blocks.weight_block(d)
        + "<footer>Записанных дней важнее идеальных. Провал — это не перебор, а пропуск.</footer>"
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
