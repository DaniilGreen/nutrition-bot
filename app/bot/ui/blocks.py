"""Содержательные блоки экранов. Экран решает КАК показать, блок — ЧТО сказать.

Живут отдельно, потому что это не оформление, а продуктовые решения: чтобы «перебор», «якорь»,
«добор» формулировались ОДИНАКОВО на всех экранах, а не разъезжались.

Главный принцип, который они несут (из разбора нутрициолога):
    ПРОВАЛ — ЭТО НЕ ПЕРЕБОР. ПРОВАЛ — ЭТО НЕ ЗАПИСАТЬ.
Отсюда: перебор никогда не стыдит и всегда переводится в недельный контекст; «компенсировать»
не предлагаем никогда (это вход в цикл рестрикция→срыв); молчание — валидный ответ.
"""
from __future__ import annotations

from app.application.results import MealSession
from app.bot.ui import kit
from app.bot.ui.context import DayContext, day_status
from app.bot.ui.kit import e, n

KCAL_PER_KG_FAT = 7700  # ккал в килограмме жировой ткани — перевод абстракции в физику


def anchor(d: DayContext) -> str:
    """Недельный якорь. ОБЯЗАН стоять рядом с дневной цифрой на каждом экране.

    Зачем: −530 ккал за день = 70 г жира (530/7700) — это в 10–20 раз меньше суточного шума веса
    (вода, гликоген, ЖКТ: ±0.5–1.5 кг). Один день не решает ничего. Показывать день без недельного
    якоря — значит провоцировать реакцию на шум: панику от перебора и эйфорию от недобора.

    Покрытие («записано 6 из 7») не украшение: среднее по 4 дням из 7 — не среднее. И смещено
    в приятную сторону, потому что пропускают чаще те дни, где переели.
    """
    if not d.logged7:
        return "<footer>За 7 дней записей нет — среднее считать не из чего.</footer>"
    tail = "" if d.logged7 >= 5 else " · неделя дырявая, среднее занижено"
    avg, goal = n(d.avg7_kcal), n(d.goal.daily_kcal)
    return f"<footer>7 дней: {avg} / {goal} ккал · записано {d.logged7} из 7{tail}</footer>"


def accuracy(d: DayContext) -> str:
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


def over_note(d: DayContext) -> str:
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


def advice(d: DayContext) -> str:
    """Совет = добор белка к концу дня.

    БЕЗ гейта «>50% оценок → не советуем»: в этом боте is_estimate помечает LLM-оценку ПЛОТНОСТИ
    (КБЖУ/100 г), а не веса, и почти вся еда идёт с LLM-плотностью → гейт срабатывал бы всегда и
    глушил добор. Честность про погрешность несёт блок accuracy (± ккал), а не блокировка совета.
    Просить «взвесь» тоже нельзя: вес юзер даёт сам, неточна плотность — её правят в /products.
    """
    from app.bot.ui import topup  # локальный импорт: topup не нужен прочим импортёрам blocks

    if not d.advice_due:
        return ""  # утром разрыв закроется сам — советовать 400 г грудки на завтрак абсурдно
    return topup.line(d.today.remaining.consumed, d.goal)


def eaten(d: DayContext, *, open_: bool = False) -> str:
    """Съеденное. Позиции — ПО ВКЛАДУ В КАЛОРИИ, а не хронологически: человек должен за две
    секунды увидеть, где были деньги, а не восстанавливать распорядок дня.

    Метка ≈ — как в проде: позиция посчитана оценкой, а не по точному весу.
    """
    if not d.today.meals:
        return ""
    rows = [(it.macros.kcal, it, m) for m in d.today.meals for it in m.items]
    rows.sort(key=lambda r: -r[0])
    lis = []
    for kcal, it, _m in rows[:12]:
        mark = "≈ " if it.is_estimate else ""
        # Времени тут НЕТ намеренно: список отсортирован по калориям, а «12:30» впереди строки
        # обещает хронологию и путает. Хронология — в «Дне» и в ленте дизайна C.
        row = f"{mark}{e(it.name_snapshot)} — {kit.g(it.grams)}<b>{n(kcal)}</b> ккал"
        lis.append(f"<li>{kit.with_macros(row, it.macros, est=it.is_estimate)}</li>")
    # Счёт — приёмов пищи, а не записей (FR-47): бургер пятью сообщениями — один приём пищи.
    # Сам список по времени не группируем — он по калориям, и группы сломали бы этот порядок.
    k, items = len(MealSession.group(d.today.meals)), len(rows)
    head = (
        f"🍽 Съедено · {k} {kit.plural(k, 'приём пищи', 'приёма пищи', 'приёмов пищи')} · "
        f"{items} {kit.plural(items, 'позиция', 'позиции', 'позиций')}"
    )
    attr = " open" if open_ else ""
    return f"<details{attr}><summary>{head}</summary><ul>{''.join(lis)}</ul></details>"


def week_table(d: DayContext) -> str:
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


def weight_block(d: DayContext) -> str:
    """Вес: тренд — но ТОЛЬКО когда его честно есть из чего считать.

    Дневной вес — шум: вода, гликоген и содержимое ЖКТ дают ±1–1.5 кг за сутки. Показывать сырое
    число как «мой вес» — значит заставлять человека реагировать на воду. Линия — сигнал.

    НО линия по четырём точкам месячной давности — не сглаживание, а выдумка. Если замеров мало
    или последний протух, честнее сказать «встань на весы», чем нарисовать красивую ложь.
    """
    logs = d.weight.logs
    if not logs:
        return "<p>⚖️ Вес ещё не записан. Напиши «вес 86.8».</p>"
    last = f"{logs[0].weight_kg:.1f}"
    stale = d.days_since_weigh_in or 0
    if not d.trend_ok:
        why = (
            f"последний замер {stale} дн. назад — тренд протух"
            if stale > 10
            else f"замеров всего {len(logs)} — линии пока нет"
        )
        return f"<p>⚖️ <b>{last} кг</b> · {why}.<br><i>Встань на весы — покажу линию.</i></p>"
    return (
        f"<p>⚖️ Тренд <b>{d.trend_kg} кг</b> · последнее {last} кг · {len(logs)} замеров</p>"
    )


def coverage_block(d: DayContext) -> str:
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


def macro_block(label: str, cons: float, goal: float) -> str:
    """Ядро всех дизайнов: строка «подпись · факт · %», под ней — бар от левого края.

    Бар на СВОЕЙ строке (Правило 1 кита) — единственная вёрстка, которая не разъезжается.
    """
    return f"<p>{kit.macro_line(label, cons, goal)}<br>{kit.bar(cons, goal, label)}</p>"




def week_balance(d: DayContext, *, sunday_summary: bool = True) -> str:
    """Недельный баланс (§6.23) — те же правила показа, что у плашки в аппке:
    не в понедельник и не без прошедших записанных дней."""
    w = d.week_bal
    if w is None or not w.goal_kcal or w.verdict == "empty" or w.today == w.start:
        return ""
    if not w.start <= w.today <= w.end:
        return ""
    if not any(x.kcal is not None and x.day < w.today for x in w.days):
        return ""
    if w.verdict == "few_logs":
        return f"<p>🗓 Неделя: записано {w.logged_days} дн. — для баланса маловато.</p>"
    if w.today == w.end and sunday_summary:
        verdict = "в плане ✅" if w.balance_kcal <= 0 else "перебор"
        return (
            f"<p>🗓 <b>Неделя {w.start.day}–{w.end.day} {kit.MON[w.end.month - 1]}</b><br/>"
            f"Цель за {w.counted_days} дн. — {n(w.week_goal_kcal)} · съедено "
            f"{n(w.consumed_kcal)}<br/>"
            f"Итог: <b>{_word(w.balance_kcal)}</b>, {verdict}<br/>"
            f"Записано {w.logged_days} из 7 дней.</p>"
        )
    tail = ""
    if w.floor_hit:
        tail = "<br/>Неделю уже не догнать — ешь по обычной цели, с понедельника новая."
    elif w.per_day_kcal is not None:
        tail = f"<br/>До воскресенья по {n(w.per_day_kcal)} — и неделя сойдётся."
    return f"<p>🗓 <b>Неделя</b> · {_word(w.balance_kcal)}{tail}</p>"


def _word(balance: float) -> str:
    """Слово вместо знака: минус рядом с «в плане» читается как плохо."""
    return (f"перебор {n(balance)} ккал" if balance > 0
            else f"в запасе {n(abs(balance))} ккал")
