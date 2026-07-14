"""Содержательные блоки, общие для трёх дизайнов. Дизайн решает КАК показать, блок — ЧТО сказать.

Живут отдельно, потому что это не оформление, а продуктовые решения. Если каждый дизайн будет
формулировать «перебор» по-своему, мы получим три разных продукта, а сравнивать надо оформление.

Главный принцип, который они несут (из разбора нутрициолога):
    ПРОВАЛ — ЭТО НЕ ПЕРЕБОР. ПРОВАЛ — ЭТО НЕ ЗАПИСАТЬ.
Отсюда: перебор никогда не стыдит и всегда переводится в недельный контекст; «компенсировать»
не предлагаем никогда (это вход в цикл рестрикция→срыв); молчание — валидный ответ.
"""
from __future__ import annotations

from app.bot.demo import kit
from app.bot.demo.data import DemoData, day_status
from app.bot.demo.kit import e, n, pct
from app.domain.value_objects import Macros

KCAL_PER_KG_FAT = 7700  # ккал в килограмме жировой ткани — перевод абстракции в физику


def anchor(d: DemoData) -> str:
    """Недельный якорь. ОБЯЗАН стоять рядом с дневной цифрой на каждом экране.

    Зачем: −530 ккал за день = 70 г жира (530/7700) — это в 10–20 раз меньше суточного шума веса
    (вода, гликоген, ЖКТ: ±0.5–1.5 кг). Один день не решает ничего. Показывать день без недельного
    якоря — значит провоцировать реакцию на шум: панику от перебора и эйфорию от недобора.

    Покрытие («записано 6 из 7») не украшение: среднее по 4 дням из 7 — не среднее. И смещено
    в приятную сторону, потому что пропускают чаще те дни, где переели.
    """
    if not d.logged7:
        return "<footer>За 7 дней записей нет — среднее считать не из чего.</footer>"
    tail = f"записано {d.logged7} из 7 дней"
    if d.logged7 < 5:
        tail += " · дырявая неделя, среднее скорее занижено"
    avg, goal = n(d.avg7_kcal), n(d.goal.daily_kcal)
    return f"<footer>7 дней: в среднем {avg} / {goal} ккал · {tail}</footer>"


def accuracy(d: DemoData) -> str:
    """Погрешность дня. Продукт продаёт точность — обязан честно говорить, где её не гарантирует.

    σ складывается В КВАДРАТУРЕ (ошибки позиций независимы). «1820 ± 230» — это не придирка:
    при таком разбросе бессмысленно подгонять последние 40 ккал, и человек должен это знать.
    """
    if not d.items:
        return ""
    kcal = d.today.remaining.consumed.kcal
    if not d.est_items:
        return f"<p>🎯 Точность: всё по весу, {n(kcal)} ккал — можно верить.</p>"
    s = round(d.sigma)
    word = kit.plural(d.est_items, "позиция", "позиции", "позиций")
    return (
        f"<p>🎯 {n(kcal)} ± {s} ккал — {d.est_items} {word} оценкой "
        f"({d.est_kcal_share}% калорий). Реально между {n(kcal - s)} и {n(kcal + s)}.</p>"
    )


def clarify(d: DemoData) -> str:
    """Что уточнить, чтобы погрешность упала сильнее всего. Одна цель, а не список из десяти."""
    item = d.to_clarify
    if item is None or item.macros.kcal < 150:  # мелочь уточнять — налог на внимание без отдачи
        return ""
    return (
        f"<p>📏 Взвесь «{e(item.name_snapshot)}» ({n(item.macros.kcal)} ккал) — "
        "точность дня заметно вырастет.</p>"
    )


def culprit(d: DemoData) -> str:
    """«Где были деньги» — топ-позиция по вкладу в калории.

    Единственный факт о прошлом, который меняет будущий выбор. Не осуждающе: просто где деньги.
    """
    item = d.top_item
    kcal = d.today.remaining.consumed.kcal
    if item is None or not kcal or item.macros.kcal / kcal < 0.25:
        return ""  # явного лидера нет — и показывать нечего
    share = round(100 * item.macros.kcal / kcal)
    return (
        f"<p>💰 Больше всего дала <b>{e(item.name_snapshot)}</b> — "
        f"{n(item.macros.kcal)} ккал, {share}% дня.</p>"
    )


def over_note(d: DemoData) -> str:
    """Перебор: ни стыда, ни вранья. Число → физика → недельный контекст → «не компенсируй».

    «Компенсировать» не предлагаем НИКОГДА: «отработаю кардио» и «завтра поголодаю» — это вход
    в цикл рестрикция→растормаживание, единственная механика, способная сделать трекер вредным.
    """
    c, g = d.today.remaining.consumed, d.goal
    over = c.kcal - g.daily_kcal
    if over <= 0:
        return ""
    grams_fat = round(over / KCAL_PER_KG_FAT * 1000)
    week_ok = d.logged7 >= 5 and d.avg7_kcal <= g.daily_kcal
    tail = (
        " Неделя всё равно в дефиците — курс держится."
        if week_ok
        else " Компенсировать не надо: завтра просто обычная цель."
    )
    return f"<p>+{n(over)} ккал сверх цели ≈ {grams_fat} г жировой ткани.{tail}</p>"


def protein_note(d: DemoData) -> str:
    """Белок в г/кг. Абсолютные «164 г» ничего не значат, пока не знаешь массу человека.

    При дефиците + силовых цель 1.6–2.2 г/кг: ниже — организм добирает аминокислоты из мышц,
    и вес уходит не оттуда, откуда хотелось.
    """
    ppk = d.protein_per_kg
    if ppk is None or not d.weight_kg:
        return ""
    goal_ppk = round(d.goal.protein_g / d.weight_kg, 1)
    return f"<p>🥩 Белок {ppk} г/кг массы (цель {goal_ppk}). Коридор при дефиците — 1.6–2.2.</p>"


def advice(d: DemoData) -> str:
    """Совет = добор. С гейтом доверия: при >50% оценок точечный совет вреден — база плывёт
    сильнее совета. Тогда просим уточнить вес, а не выдумываем граммы творога."""
    from app.bot.demo import topup  # локальный импорт: topup не нужен прочим импортёрам blocks

    if not d.trust_ok:
        item = d.to_clarify
        if item is None:
            return ""
        return (
            "<p>🤔 Больше половины калорий сегодня — оценка. Точный совет дать не могу: "
            f"взвесь «{e(item.name_snapshot)}», и посчитаю добор.</p>"
        )
    return topup.line(d.today.remaining.consumed, d.goal)


def eaten(d: DemoData, *, open_: bool = False) -> str:
    """Съеденное. Позиции — ПО ВКЛАДУ В КАЛОРИИ, а не хронологически: человек должен за две
    секунды увидеть, где были деньги, а не восстанавливать распорядок дня.

    Метка ≈ — как в проде: позиция посчитана оценкой, а не по точному весу.
    """
    if not d.today.meals:
        return ""
    rows = [(it.macros.kcal, it, m) for m in d.today.meals for it in m.items]
    rows.sort(key=lambda r: -r[0])
    lis = []
    for kcal, it, m in rows[:12]:
        mark = "≈ " if it.is_estimate else ""
        when = kit.tg_time(m.eaten_at, d.today.timezone)
        lis.append(f"<li>{when} · {mark}{e(it.name_snapshot)} — <b>{n(kcal)}</b> ккал</li>")
    k = d.today.meals_count
    word = kit.plural(k, "приём", "приёма", "приёмов")
    attr = " open" if open_ else ""
    return (
        f"<details{attr}><summary>🍽 Съедено · {k} {word} · по вкладу в ккал</summary>"
        f"<ul>{''.join(lis)}</ul></details>"
    )


def week_table(d: DemoData) -> str:
    """Таблица недели. ТАБЛИЦА ЗДЕСЬ ЗАКОННА (Правило 2 кита): узкие числа, бара в ячейках нет.

    Статус дня — кружком, а не баром: бар в ячейку пихать нельзя, он ломает ширину колонок.
    """
    icons = {"none": "▫️", "ok": "🟢", "low": "🟡", "over": "🔴"}
    rows = []
    for day, kcal in d.days:
        st = day_status(kcal, d.goal.daily_kcal)
        rows.append(
            f"<tr><td>{kit.WD[day.weekday()]}</td>"
            f'<td align="center">{icons[st]}</td>'
            f'<td align="right">{n(kcal) if kcal > 0 else "—"}</td></tr>'
        )
    return f"<table striped>{''.join(rows)}</table>"


def weight_block(d: DemoData) -> str:
    """Вес: тренд, а не последнее взвешивание.

    Дневной вес — шум: вода, гликоген и содержимое ЖКТ дают ±1–1.5 кг за сутки. Показывать его
    как «мой вес» — значит заставлять человека реагировать на воду. Линия — сигнал.
    """
    logs = d.weight.logs
    if not logs:
        return "<p>⚖️ Вес ещё не записан. Напиши «вес 86.8».</p>"
    last = logs[0].weight_kg
    if d.trend_kg is None or len(logs) < 2:
        return f"<p>⚖️ <b>{last} кг</b> · одно взвешивание — тренда пока нет.</p>"
    return (
        f"<p>⚖️ Тренд <b>{d.trend_kg} кг</b> · последнее {last} кг · {len(logs)} замеров<br>"
        "<i>Одно взвешивание — шум. Линия — сигнал.</i></p>"
    )


def coverage_block(d: DemoData) -> str:
    """Покрытие лога за 30 дней ВМЕСТО стрика.

    Стрик хрупок: один пропуск обнуляет, и человек либо пишет мусор ради серии (портит главный
    актив — данные), либо бросает от стыда. Покрытие не ломается — оно просаживается и
    восстанавливается.
    """
    m = d.month
    cov = round(100 * m.days_logged / m.total_days) if m.total_days else 0
    return (
        f"<p>📈 Записано {m.days_logged} из {m.total_days} дней ({cov}%) · "
        f"в цели {m.days_in_goal} из {m.days_logged}</p>"
    )


def branches(d: DemoData) -> str:
    """Витрина веток, которых на СЕГОДНЯШНИХ данных не видно.

    Нужна только демо: у владельца сегодня белок закрыт, калории в бюджете — значит «чем добрать»,
    перебор и «похоже, не всё записано» просто не сработают, и он решит, что их выкинули. Цифры
    тут СИНТЕТИЧЕСКИЕ и подписаны как таковые. В проде этот блок не поедет.
    """
    from dataclasses import replace

    from app.domain.goals import remaining

    g = d.goal

    def what_if(m: Macros) -> DemoData:
        return replace(d, today=replace(d.today, remaining=remaining(m, g)))

    gap = Macros(kcal=1400, protein=95, fat=40, carb=160)  # белка мало, место есть
    fat_out = Macros(kcal=1200, protein=100, fat=g.fat_g * 1.4, carb=150)  # жир уже перебран
    big = Macros(kcal=g.daily_kcal + 620, protein=162, fat=88, carb=300)  # перебор

    return (
        "<h3>🎬 Ветки, которых сегодня не видно</h3>"
        "<p><i>Цифры выдуманы — у тебя сегодня белок закрыт и калории в бюджете, поэтому эти "
        "сообщения не сработали бы. Показываю, как они выглядят.</i></p>"
        "<p><b>1. Белка не хватает, место есть</b></p>" + advice(what_if(gap)) + "<hr/>"
        "<p><b>2. Жир уже перебран — добирать белок нечем</b></p>" + advice(what_if(fat_out)) +
        "<hr/>"
        "<p><b>3. Перебор калорий</b></p>" + over_note(what_if(big)) +
        "<p><i>Ни стыда, ни компенсации: «отработать кардио» и «завтра поголодаю» — прямой путь "
        "в цикл срывов.</i></p>"
    )


def macro_block(label: str, cons: float, goal: float) -> str:
    """Ядро всех дизайнов: строка «подпись · факт · %», под ней — бар от левого края.

    Бар на СВОЕЙ строке (Правило 1 кита) — единственная вёрстка, которая не разъезжается.
    """
    return f"<p>{kit.macro_line(label, cons, goal)}<br>{kit.bar(cons, goal, label)}</p>"


def pct_line(label: str, cons: float, goal: float) -> str:
    """Компактная строка без бара — для макросов, которым бар не положен (жир, углеводы)."""
    return f"{label} {n(cons)} / {n(goal)} г · {pct(cons, goal)}%"
