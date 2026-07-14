"""ВРЕМЕННОЕ демо: 7 вариантов дизайна «Сегодня» на Rich Messages (Bot API 10.1), БЕЗ картинок.

Что заложено (по итогам ресёрча рынка — MacroFactor/Cronometer/MFP — и спеки Telegram):
* порядок Ккал → Б → Ж → У, и МАКРОСЫ ПЕРВЫМИ, съеденное — под <details> (за обратный порядок
  в 2026 сожгли новый MyFitnessPal: «дневник похоронен, макросы мелкие»);
* цифры И проценты одновременно + ЗНАКОВАЯ ДЕЛЬТА (−450 / +20) — семантика «сколько ещё»
  сохранена, слова «осталось» нет;
* ПЕРЕБОР ВИДЕН: старый бар клампился на 100% → 110% и 180% выглядели одинаково (дезинформация
  при приоритете точности). Здесь перебор меняет цвет/глиф, величину несут % и Δ;
* один смысл цвета на весь бот: недобор / в цели / перебор;
* строка-вердикт и «чем добрать» — превращают числа в действие.

Цвет в тексте добыт через СВОЙ набор custom emoji (бот создал набор, id ниже): сегмент-полоска
во всю ширину канваса → несколько подряд склеиваются в СПЛОШНОЙ цветной бар. Картинок нет.
Удалить после выбора варианта.
"""
from __future__ import annotations

from datetime import date
from html import escape
from zoneinfo import ZoneInfo

from app.application.entities import Product
from app.application.results import DailyStats, DayView
from app.domain.goals import Goal, in_goal

# --- свой набор custom emoji (kbju_by_Nutrition_calorie_bot) ---
EM_KCAL, EM_PROT, EM_FAT, EM_CARB = (
    "5382216152195833809",  # 🔵 василёк
    "5382357761562551012",  # 🔴 красный
    "5382253144749155046",  # 🟡 янтарь
    "5382178987843820107",  # 🟢 тил
)
SEG_OK, SEG_LOW, SEG_OVER, SEG_EMPTY = (
    "5382251469711910289",  # 🟩 в цели
    "5382022651034245448",  # 🟨 недобор
    "5384394684162419839",  # 🟥 перебор
    "5382140560771424183",  # ⬜ трек
)
_SEG = {"ok": (SEG_OK, "🟩"), "low": (SEG_LOW, "🟨"), "over": (SEG_OVER, "🟥")}
_DOT = {"ok": "🟢", "low": "🟡", "over": "🔴"}
_WD = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def _n(v: float) -> str:
    return str(round(v))


def _e(s: str) -> str:
    return escape(s.strip(), quote=False)


def _em(eid: str, fallback: str) -> str:
    """Кастом-эмодзи; fallback обязателен по спеке (показывается там, где кастом не рендерится)."""
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'


def _rows(st: DailyStats) -> list[tuple[str, str, str, float, float]]:
    """(emoji_id, fallback, подпись, съедено, цель) — порядок как у MacroFactor: ккал первым."""
    c, g = st.remaining.consumed, st.remaining.goal
    return [
        (EM_KCAL, "🔵", "Ккал", c.kcal, g.daily_kcal),
        (EM_PROT, "🔴", "Белок", c.protein, g.protein_g),
        (EM_FAT, "🟡", "Жир", c.fat, g.fat_g),
        (EM_CARB, "🟢", "Углеводы", c.carb, g.carb_g),
    ]


def _status(cons: float, goal: float, label: str) -> str:
    """недобор / в цели / перебор. Для белка недобор критичнее → порог выше."""
    if not goal:
        return "ok"
    r = cons / goal
    low_edge = 0.90 if label == "Белок" else 0.85
    if r > 1.05:
        return "over"
    return "ok" if r >= low_edge else "low"


def _pct(cons: float, goal: float) -> int:
    return round(100 * cons / goal) if goal else 0


def _delta(cons: float, goal: float) -> str:
    """Знаковая дельта вместо слова «осталось»: −450 / +20."""
    d = round(cons - goal)
    return f"+{d}" if d > 0 else (f"−{abs(d)}" if d else "0")


def _fill(cons: float, goal: float, width: int) -> int:
    ratio = max(0.0, min(1.0, cons / goal)) if goal else 0.0
    return round(width * ratio)


# ---------- бары ----------

def _bar_emoji(cons: float, goal: float, label: str, width: int = 6) -> str:
    """Цветной бар из сегментов-полосок (склеиваются в сплошную линию). Перебор — весь красный."""
    stt = _status(cons, goal, label)
    eid, fb = _SEG[stt]
    n = width if stt == "over" else _fill(cons, goal, width)
    empty = width - n
    return _em(eid, fb) * n + _em(SEG_EMPTY, "⬜") * empty


def _bar_mark(cons: float, goal: float, width: int = 10) -> str:
    """Нативная подсветка на пробелах — узко, один акцентный цвет темы."""
    f = _fill(cons, goal, width)
    fill = f"<mark>{'&nbsp;' * (f * 2)}</mark>" if f else ""
    return fill + "&nbsp;" * ((width - f) * 2)


def _bar_ascii(cons: float, goal: float, label: str, width: int = 10) -> str:
    """Моноширинный бар. Перебор — другой глиф (▓), чтобы 110% и 180% не выглядели одинаково."""
    stt = _status(cons, goal, label)
    if stt == "over":
        return "▓" * width
    f = _fill(cons, goal, width)
    return "█" * f + "░" * (width - f)


# ---------- общие блоки ----------

def _verdict(st: DailyStats) -> str:
    """Одна фраза: что чинить, не читая таблицу."""
    low = [lab for _, _, lab, c, g in _rows(st) if _status(c, g, lab) == "low"]
    over = [lab for _, _, lab, c, g in _rows(st) if _status(c, g, lab) == "over"]
    if not low and not over:
        return "Всё в цели"
    parts = []
    if low:
        parts.append(f"{', '.join(low).lower()} — недобор")
    if over:
        parts.append(f"{', '.join(over).lower()} — перебор")
    return " · ".join(parts)


def _topup(st: DailyStats) -> str:
    """«Чем добрать» — превращает число в действие. Творог 3%: ~18 г белка на 100 г."""
    c, g = st.remaining.consumed, st.remaining.goal
    need = g.protein_g - c.protein
    if need < 15:
        return ""
    grams = round(need / 0.18 / 10) * 10
    return f"<p>💡 добери <b>{_n(need)} г белка</b> — это ≈ {grams} г творога</p>"


def _eaten(st: DailyStats) -> str:
    """Съеденное — ПОД КАТ (не первым!). Время — <tg-time>: локализуется у зрителя."""
    if not st.meals:
        return ""
    tz = ZoneInfo(st.timezone)
    items = []
    for m in st.meals:
        ts = int(m.eaten_at.timestamp())
        when = f'<tg-time unix="{ts}" format="t">{m.eaten_at.astimezone(tz):%H:%M}</tg-time>'
        for it in m.items:
            items.append(f"<li>{when} · {_e(it.name_snapshot)} — {_n(it.macros.kcal)} ккал</li>")
    body = "<ul>" + "".join(items[:12]) + "</ul>"
    return f"<details><summary>🍽 Съедено · {st.meals_count} приёмов</summary>{body}</details>"


def _head(st: DailyStats, title: str) -> str:
    return f"<h3>{title}</h3><p><i>{_verdict(st)}</i></p>"


# ---------- ВАРИАНТЫ ----------

def v1_emoji_bar(st: DailyStats) -> str:
    """★ Цветной бар из custom emoji — настоящая палитра, без картинок."""
    out = [_head(st, "1 · Цветной бар (свои эмодзи)"),
           "<table><tr><th></th><th>Прогресс</th><th>Факт</th></tr>"]
    for eid, fb, lab, c, g in _rows(st):
        stt = _status(c, g, lab)
        out.append(
            f"<tr><td>{_em(eid, fb)} <b>{lab}</b></td>"
            f"<td>{_bar_emoji(c, g, lab)} {_pct(c, g)}%</td>"
            f'<td align="right">{_n(c)}/{_n(g)}<br>{_delta(c, g)} {_DOT[stt]}</td></tr>'
        )
    out.append("</table>")
    return "".join(out) + _topup(st) + _eaten(st)


def v2_mark_dot(st: DailyStats) -> str:
    """Нативная подсветка + цветная точка статуса (палитра в точке, бар — акцент темы)."""
    out = [_head(st, "2 · Подсветка + статус-точка"),
           "<table><tr><th></th><th>Прогресс</th><th>%</th><th>Δ</th></tr>"]
    for _eid, _fb, lab, c, g in _rows(st):
        stt = _status(c, g, lab)
        out.append(
            f"<tr><td>{_DOT[stt]} <b>{lab}</b></td><td>{_bar_mark(c, g)}</td>"
            f'<td align="right">{_pct(c, g)}%</td>'
            f'<td align="right">{_delta(c, g)}</td></tr>'
        )
    out.append("</table>")
    out.append(f"<footer>{' · '.join(f'{_n(c)}/{_n(g)}' for *_, c, g in _rows(st))}</footer>")
    return "".join(out) + _topup(st) + _eaten(st)


def v3_mono(st: DailyStats) -> str:
    """Моноширинный «терминал»: латиница + цифры моноширинны → сетка держится железно."""
    lines = [f"{'':<5}{'FACT/GOAL':>12}{'':>13}{'%':>5}{'Δ':>7}"]
    lat = {"Ккал": "KCAL", "Белок": "PROT", "Жир": "FAT", "Углеводы": "CARB"}
    for _eid, _fb, lab, c, g in _rows(st):
        fact = f"{_n(c)}/{_n(g)}"
        lines.append(
            f"{lat[lab]:<5}{fact:>12} {_bar_ascii(c, g, lab)} "
            f"{_pct(c, g):>4}% {_delta(c, g):>6}"
        )
    body = escape("\n".join(lines), quote=False)
    return (
        _head(st, "3 · Моноширинный «терминал»")
        + f"<pre><code>{body}</code></pre>"
        + _topup(st)
        + _eaten(st)
    )


def v4_checklist(st: DailyStats) -> str:
    """Цели дня галочками — <input type=checkbox>. Достигнута = ✓."""
    out = [_head(st, "4 · Чеклист целей"), "<ul>"]
    for _eid, _fb, lab, c, g in _rows(st):
        stt = _status(c, g, lab)
        chk = " checked" if stt == "ok" else ""
        out.append(
            f'<li><input type="checkbox"{chk}><b>{lab}</b> — {_n(c)}/{_n(g)} '
            f"· {_pct(c, g)}% · {_delta(c, g)} {_DOT[stt]}</li>"
        )
    out.append("</ul>")
    return "".join(out) + _topup(st) + _eaten(st)


def v5_hero(st: DailyStats) -> str:
    """Герой-вердикт крупной выносной цитатой (<aside>) + компактная таблица."""
    c, g = st.remaining.consumed, st.remaining.goal
    out = [
        "<h3>5 · Герой-вердикт</h3>",
        f"<aside>{_verdict(st)}<cite>{_n(c.kcal)} / {_n(g.daily_kcal)} ккал · "
        f"{_pct(c.kcal, g.daily_kcal)}%</cite></aside>",
        "<table striped>",
    ]
    for eid, fb, lab, cc, gg in _rows(st)[1:]:  # ккал уже в герое
        out.append(
            f"<tr><td>{_em(eid, fb)} <b>{lab}</b></td>"
            f"<td>{_bar_emoji(cc, gg, lab)}</td>"
            f'<td align="right">{_pct(cc, gg)}% · {_delta(cc, gg)}</td></tr>'
        )
    out.append("</table>")
    return "".join(out) + _topup(st) + _eaten(st)


def v6_lines(st: DailyStats) -> str:
    """Без таблицы — строками. Максимум воздуха, нечему съезжать."""
    out = [_head(st, "6 · Компакт-строки")]
    for eid, fb, lab, c, g in _rows(st):
        stt = _status(c, g, lab)
        out.append(
            f"<p>{_em(eid, fb)} <b>{lab}</b> · {_n(c)}/{_n(g)} · "
            f"<b>{_pct(c, g)}%</b> · {_delta(c, g)} {_DOT[stt]}<br>{_bar_emoji(c, g, lab, 8)}</p>"
        )
    return "".join(out) + _topup(st) + _eaten(st)


def v7_anchors(
    st: DailyStats,
    week: list[tuple[date, float]],
    goal: Goal,
    dv: DayView,
    products: list[Product],
) -> str:
    """Всё в ОДНОМ сообщении + оглавление на якорях: тап → прыжок к разделу."""
    out = [
        "<h3>7 · Одно сообщение + навигация</h3>",
        '<p><a href="#today">Сегодня</a> · <a href="#week">Неделя</a> · '
        '<a href="#hist">История</a> · <a href="#prod">Продукты</a></p>',
        '<a name="today"></a><h4>📊 Сегодня</h4>',
        f"<p><i>{_verdict(st)}</i></p><table>",
    ]
    for eid, fb, lab, c, g in _rows(st):
        out.append(
            f"<tr><td>{_em(eid, fb)} <b>{lab}</b></td><td>{_bar_emoji(c, g, lab)}</td>"
            f'<td align="right">{_pct(c, g)}% · {_delta(c, g)}</td></tr>'
        )
    out.append("</table>")

    out.append('<hr/><a name="week"></a><h4>📈 Неделя</h4><table striped>')
    for d, kcal in week:
        mark = "—" if kcal <= 0 else ("🟢" if in_goal(kcal, goal.daily_kcal) else "🔴")
        out.append(
            f"<tr><td>{_WD[d.weekday()]} {d.day}.{d.month:02d}</td>"
            f'<td align="right">{_n(kcal)}</td><td>{mark}</td></tr>'
        )
    out.append("</table>")

    tz = ZoneInfo(dv.timezone)
    out.append('<hr/><a name="hist"></a><h4>🗒 История</h4><table striped>')
    rows = 0
    for m in dv.meals:
        when = m.eaten_at.astimezone(tz).strftime("%H:%M")
        for it in m.items:
            out.append(
                f"<tr><td>{when}</td><td>{_e(it.name_snapshot)}</td>"
                f'<td align="right">{_n(it.macros.kcal)}</td></tr>'
            )
            rows += 1
            if rows >= 8:
                break
    out.append("</table>")

    if products:
        out.append('<hr/><a name="prod"></a><h4>🛠 Продукты</h4><table striped>')
        for p in products[:6]:
            out.append(
                f"<tr><td>{_e(p.display_name)}</td>"
                f'<td align="right">{_n(p.density.kcal_per_100g)}</td><td>≈<sup>1</sup></td></tr>'
            )
        out.append("</table>")
        out.append('<tg-reference name="n1">≈ — я прикинул КБЖУ сам, можно поправить '
                   "в /products</tg-reference>")
    return "".join(out)


def build_variants(
    st: DailyStats,
    week: list[tuple[date, float]],
    goal: Goal,
    dv: DayView,
    products: list[Product],
) -> list[str]:
    """Все варианты — каждый отдельным сообщением, чтобы сравнить глазами."""
    return [
        v1_emoji_bar(st),
        v2_mark_dot(st),
        v3_mono(st),
        v4_checklist(st),
        v5_hero(st),
        v6_lines(st),
        v7_anchors(st, week, goal, dv, products),
    ]
