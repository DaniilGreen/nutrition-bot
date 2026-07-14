"""ВРЕМЕННОЕ демо: два финалиста дизайна «Сегодня» на Rich Messages, БЕЗ картинок.

Цвет добыт своим набором custom emoji: сегмент-полоска во всю ширину канваса → несколько подряд
склеиваются в СПЛОШНОЙ цветной бар (палитра наша, растра нет).

ВЁРСТКА — уроки первого захода (ширина на телефоне ≈ один экран, лишнего нет):
* НЕТ строки-шапки <th> — она задавала минимальную ширину колонок и выдавливала подписи;
* бар и % — В РАЗНЫХ ячейках (в одной колонка сжималась по бару, процент уезжал на 2-ю строку);
* никаких <br> внутри ячеек;
* бар 5 сегментов — оставляет зазор, чтобы «113%» не липло к полосе;
* дельты (−790/+8) — в <footer>, чтобы не жрать ширину таблицы.

СМЫСЛ (по ресёрчу рынка):
* порядок Ккал → Б → Ж → У; макросы ПЕРВЫМИ, съеденное под <details>;
* цифры И проценты одновременно; слова «осталось» нет — есть знаковая дельта;
* перебор ВИДЕН (бар краснеет целиком) — раньше 110% и 180% выглядели одинаково;
* «чем добрать» превращает число в действие.
Удалить после выбора.
"""
from __future__ import annotations

from html import escape
from zoneinfo import ZoneInfo

from app.application.results import DailyStats

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
_BAR_W = 5  # шире — и «113%» липнет к полосе


def _n(v: float) -> str:
    return str(round(v))


def _e(s: str) -> str:
    return escape(s.strip(), quote=False)


def _em(eid: str, fallback: str) -> str:
    """Кастом-эмодзи; fallback обязателен по спеке (там, где кастом не рендерится)."""
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'


def _rows(st: DailyStats) -> list[tuple[str, str, str, float, float]]:
    """(emoji_id, fallback, подпись, съедено, цель). Порядок как у MacroFactor: ккал первым."""
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


def _bar(cons: float, goal: float, label: str) -> str:
    """Сплошной цветной бар. Перебор — весь красный (величину несут % и Δ)."""
    stt = _status(cons, goal, label)
    eid, fb = _SEG[stt]
    if stt == "over":
        return _em(eid, fb) * _BAR_W
    ratio = max(0.0, min(1.0, cons / goal)) if goal else 0.0
    n = round(_BAR_W * ratio)
    return _em(eid, fb) * n + _em(SEG_EMPTY, "⬜") * (_BAR_W - n)


# ---------- общие блоки ----------

def _verdict(st: DailyStats) -> str:
    low = [lab for _, _, lab, c, g in _rows(st) if _status(c, g, lab) == "low"]
    over = [lab for _, _, lab, c, g in _rows(st) if _status(c, g, lab) == "over"]
    if not low and not over:
        return "Всё в цели"
    parts = []
    if low:
        parts.append(f"Недобор — {', '.join(low).lower()}")
    if over:
        parts.append(f"Перебор — {', '.join(over).lower()}")
    return " · ".join(parts)


def _deltas_footer(st: DailyStats) -> str:
    """Дельты живут в подвале — не воруют ширину у таблицы."""
    parts = [f"{lab.lower()} {_delta(c, g)}" for _, _, lab, c, g in _rows(st)]
    return f"<footer>Δ до цели: {' · '.join(parts)}</footer>"


def _topup(st: DailyStats) -> str:
    """«Чем добрать» — превращает число в действие. Творог 3%: ~18 г белка на 100 г."""
    c, g = st.remaining.consumed, st.remaining.goal
    need = g.protein_g - c.protein
    if need < 15:
        return ""
    grams = round(need / 0.18 / 10) * 10
    return f"<p>💡 добери <b>{_n(need)} г белка</b> — это ≈ {grams} г творога</p>"


def _eaten(st: DailyStats) -> str:
    """Съеденное — ПОД КАТ. Время через <tg-time>: локализуется у зрителя."""
    if not st.meals:
        return ""
    tz = ZoneInfo(st.timezone)
    items = []
    for m in st.meals:
        ts = int(m.eaten_at.timestamp())
        when = f'<tg-time unix="{ts}" format="t">{m.eaten_at.astimezone(tz):%H:%M}</tg-time>'
        for it in m.items:
            items.append(f"<li>{when} · {_e(it.name_snapshot)} — {_n(it.macros.kcal)} ккал</li>")
    word = "приём" if st.meals_count == 1 else "приёма" if st.meals_count < 5 else "приёмов"
    body = "<ul>" + "".join(items[:12]) + "</ul>"
    return f"<details><summary>🍽 Съедено · {st.meals_count} {word}</summary>{body}</details>"


def _macro_row(eid: str, fb: str, lab: str, c: float, g: float) -> str:
    """Строка: [точка+подпись] | [бар, под ним %] | [факт].

    Ключ к ровности — ДВЕ вещи:
    1) % живёт ПОД БАРОМ, в его же ячейке → колонка факта требует всего «1410/2200» (9 симв.)
       и больше не сжимается; у бара остаётся зазор, «113%» ни к чему не липнет.
    2) valign="middle" на подписи и факте → они центрируются по высоте двухстрочной ячейки
       с баром. Без этого ячейки разной высоты разъезжались по вертикали — это и было «криво».
    """
    return (
        f'<tr><td valign="middle">{_em(eid, fb)} <b>{lab}</b></td>'
        f'<td valign="middle">{_bar(c, g, lab)}<br><b>{_pct(c, g)}%</b></td>'
        f'<td align="right" valign="middle">{_n(c)}/{_n(g)}</td></tr>'
    )


# ---------- ФИНАЛИСТЫ ----------

def v1_dashboard(st: DailyStats) -> str:
    """1 · Дашборд: все четыре макроса ровной таблицей."""
    rows = "".join(_macro_row(*r) for r in _rows(st))
    return (
        "<h3>1 · Дашборд</h3>"
        f"<p><i>{_verdict(st)}</i></p>"
        f"<table striped>{rows}</table>"
        + _deltas_footer(st)
        + _topup(st)
        + _eaten(st)
    )


def v5_hero(st: DailyStats) -> str:
    """5 · Герой-вердикт: калории крупной выносной цитатой, ниже — Б/Ж/У."""
    c, g = st.remaining.consumed, st.remaining.goal
    rows = "".join(_macro_row(*r) for r in _rows(st)[1:])  # ккал уже в герое
    return (
        "<h3>5 · Герой-вердикт</h3>"
        f"<aside>{_verdict(st)}"
        f"<cite>{_n(c.kcal)} / {_n(g.daily_kcal)} ккал · {_pct(c.kcal, g.daily_kcal)}%</cite>"
        "</aside>"
        f"<table striped>{rows}</table>"
        + _deltas_footer(st)
        + _topup(st)
        + _eaten(st)
    )


def build_variants(st: DailyStats) -> list[str]:
    """Два финалиста — сравнить ровность глазами."""
    return [v1_dashboard(st), v5_hero(st)]
