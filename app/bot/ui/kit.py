"""Общий кит для трёх демо-дизайнов: физика вёрстки Rich Messages + примитивы.

ФИЗИКА, ДОБЫТАЯ БОЛЬЮ (четыре кривых итерации). Нарушать — значит опять получить «уехало»:

  ПРАВИЛО 1. Эмодзи-бар НИКОГДА не делит строку с текстом — он живёт на своей.
    Почему: таблица раздаёт лишнюю ширину ПЕРВОЙ колонке (подпись «Углеводы» просит 180 у.е.,
    получает 275) → перед баром зияет мёртвый зазор, а прижать его нечем: атрибута ширины
    у ячеек в Rich HTML нет. Бар на своей строке выравнивается сам — по левому краю пузыря.

  ПРАВИЛО 2. Таблица — только для узкой числовой сетки (дни × макросы), где в ячейках
    короткие числа и НЕТ бара. Там колонки работают как надо.

  ПРАВИЛО 3. Никакого valign="middle" рядом с многострочной ячейкой: он центрирует
    одностроч­ную соседку по высоте многострочной → число повисает между строк.

  ПРАВИЛО 4. Ширина проверяется тестом (tests/unit/test_demo_widths.py), а не глазами.
    Модель откалибрована по реальным скриншотам iPhone: пузырь ≈ 760 у.е., кастом-эмодзи
    ≈ 46 у.е. (двойная ширина!), кириллица ≈ 18, цифра ≈ 16.

  ПРАВИЛО 5. Цвет = СТАТУС и только статус (недобор/в цели/перебор). Красить по
    идентичности макроса нельзя: два смысла одного красного на экране — и глаз спотыкается.

  ПРАВИЛО 6. skip_entity_detection=True на отправке, иначе Telegram примет «1860/2200»
    за номер карты и подчеркнёт.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from html import escape
from zoneinfo import ZoneInfo

from app.domain.goals import Goal
from app.domain.value_objects import Macros

# --- свой набор custom emoji (kbju_by_Nutrition_calorie_bot) ---
# Полоска во всю ширину канваса 100×100 → несколько подряд склеиваются в СПЛОШНОЙ бар.
SEG_OK, SEG_LOW, SEG_OVER, SEG_EMPTY = (
    "5382251469711910289",  # 🟩 в цели
    "5382022651034245448",  # 🟨 недобор
    "5384394684162419839",  # 🟥 перебор
    "5382140560771424183",  # ⬜ пустой трек
)
_SEG = {"ok": (SEG_OK, "🟩"), "low": (SEG_LOW, "🟨"), "over": (SEG_OVER, "🟥")}
_DOT = {"ok": "🟢", "low": "🟡", "over": "🔴"}  # компактный статус для таблиц (там бару нельзя)

BAR_W = 10  # трек = ЦЕЛЬ (100%). Перебор дорисовывается ЗА треком, см. bar()
MAX_OVER = 4  # максимум сегментов перебора: 10+4=14 × 46 ≈ 644 у.е. — всегда влезает в 700

# --- модель ширины: страховка от «уехало» (у.е. = экранные px iPhone при пузыре 760) ---
BUBBLE = 760
SAFE = 700  # потолок строки: ~8% запаса на кернинг и разные шрифты блоков
_W_EMOJI, _W_CYR, _W_DIGIT, _W_SPACE, _W_THIN = 46, 18, 16, 7, 9

WD = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
MON = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def n(v: float) -> str:
    return str(round(v))


def e(s: str) -> str:
    return escape(s.strip(), quote=False)


def em(eid: str, fallback: str) -> str:
    """Кастом-эмодзи; fallback обязателен по спеке — там, где кастом не рендерится."""
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'


def status(cons: float, goal: float, label: str) -> str:
    """недобор / в цели / перебор.

    У БЕЛКА ПЕРЕБОРА НЕ БЫВАЕТ. Это единственный макрос, превышение которого безвредно (лишний
    белок просто окисляется, а сытость и термический эффект у него выше всех). Красить 120% белка
    красным — значит ругать человека за единственное, что он сделал правильно. Порог недобора у
    белка, наоборот, строже: при дефиците его нехватка бьёт по мышцам.
    """
    if not goal:
        return "ok"
    r = cons / goal
    if label.startswith("Белок"):
        return "ok" if r >= 0.90 else "low"
    if r > 1.05:
        return "over"
    return "ok" if r >= 0.85 else "low"


def dot(cons: float, goal: float, label: str) -> str:
    """Точка-статус для таблиц: бар в ячейку нельзя (Правило 1), а один кружок — можно."""
    return _DOT[status(cons, goal, label)]


def pct(cons: float, goal: float) -> int:
    return round(100 * cons / goal) if goal else 0


def bar(cons: float, goal: float, label: str, w: int = BAR_W) -> str:
    """Бар, где ТРЕК = ЦЕЛЬ, а перебор ТОРЧИТ ЗА ТРЕКОМ.

    Так и только так виден масштаб перебора. Две предыдущие версии врали по-разному:
      * clamp(ratio, 1.0) — 110% и 180% рисовались одинаково (дезинформация);
      * «весь бар краснеет» — тоже одинаково, только громче.
    Теперь 119% = 10 зелёных (цель ВЗЯТА — это правда) + 2 красных ЗА границей. Виден и факт
    достижения цели, и величина превышения.

    Перебор режем на MAX_OVER сегментов: при 300% бар иначе уехал бы на вторую строку. Точную
    величину несёт %, бар отвечает только за «на сколько примерно».
    """
    if not goal:
        return em(SEG_EMPTY, "⬜") * w
    ratio = max(0.0, cons / goal)
    stt = status(cons, goal, label)
    if stt != "over":
        # Порог перебора один и тот же у бара и у вердикта — иначе при 102% вердикт говорит
        # «Всё в цели», а бар уже рисует красный хвост, и экран спорит сам с собой.
        eid, fb = _SEG[stt]
        filled = round(w * min(1.0, ratio))
        return em(eid, fb) * filled + em(SEG_EMPTY, "⬜") * (w - filled)

    # ПЕРЕБОР — ВЕСЬ БАР КРАСНЫЙ, а не «зелёный с красным хвостиком».
    # Была ошибка: первые 10 сегментов красились зелёным («цель же взята!») — и при 144% жира
    # 71% полосы был зелёным, то есть провал выглядел успехом. Цвет = наполнение (как и просил
    # владелец): перелил — вся полоса красная. Длина сверх трека несёт величину.
    raw_over = round(w * (ratio - 1.0))
    over = min(MAX_OVER, max(1, raw_over))
    # «…» — честная метка насыщения: дальше бар физически не растёт (уедет на вторую строку),
    # и без неё 140% и 300% выглядели БЫ одинаково. Точную величину всегда несёт %.
    saturated = "…" if raw_over > MAX_OVER else ""
    return em(SEG_OVER, "🟥") * (w + over) + saturated


def sigma_kcal(items: list[tuple[float, bool]]) -> float:
    """Погрешность дня по калориям: σ = √(Σ (kcal_i × rel_i)²), rel = 25% оценке / 5% точному весу.

    В КВАДРАТУРЕ, а не линейно: ошибки позиций независимы. Линейная сумма завышает интервал вдвое
    и делает его бесполезным («1820 ± 460» — с таким разбросом не о чем говорить).
    """
    var = sum((kcal * (0.25 if est else 0.05)) ** 2 for kcal, est in items)
    return float(var**0.5)


def tg_time(dt: datetime, tzname: str) -> str:
    """<tg-time> локализуется на клиенте зрителя; fallback — наш рендер в его TZ."""
    ts = int(dt.timestamp())
    local = dt.astimezone(ZoneInfo(tzname))
    return f'<tg-time unix="{ts}" format="t">{local:%H:%M}</tg-time>'


def day_ru(d: date) -> str:
    return f"{WD[d.weekday()]} {d.day} {MON[d.month - 1]}"


def plural(k: int, one: str, few: str, many: str) -> str:
    if k % 10 == 1 and k % 100 != 11:
        return one
    if 2 <= k % 10 <= 4 and not 12 <= k % 100 <= 14:
        return few
    return many


def macros_rows(cons: Macros, goal: Goal) -> list[tuple[str, float, float]]:
    """(подпись, съедено, цель). Порядок Ккал → Б → Ж → У — как просил владелец."""
    return [
        ("Ккал", cons.kcal, goal.daily_kcal),
        ("Белок", cons.protein, goal.protein_g),
        ("Жир", cons.fat, goal.fat_g),
        ("Углеводы", cons.carb, goal.carb_g),
    ]


def macro_line(label: str, cons: float, goal: float) -> str:
    """Строка «подпись · факт · %» — та, над которой ляжет бар. Единый вид во всех дизайнах."""
    return f"<b>{label}</b> · {n(cons)} / {n(goal)} · <b>{pct(cons, goal)}%</b>"


def verdict(cons: Macros, goal: Goal, *, final: bool = False) -> str:
    """Одна честная фраза: где недобор, где перебор.

    «НЕДОБОР» — ПОНЯТИЕ КОНЦА ДНЯ, И ДНЁМ ЕГО ГОВОРИТЬ НЕЛЬЗЯ. В обед человек съел 1600 из 2200 —
    разумеется, он «недобрал» всё подряд. Он просто ещё не ужинал. Бот, который в 13:00 объявляет
    «Недобор — ккал, белок, жир, углеводы», обвиняет человека в четырёх провалах на пустом месте.
    Поэтому днём (final=False) сообщаем ТОЛЬКО перебор: он-то правда уже случился и не рассосётся.
    """
    rows = macros_rows(cons, goal)
    over = [lab for lab, c, g in rows if status(c, g, lab) == "over"]
    parts = []
    if final:
        low = [lab for lab, c, g in rows if status(c, g, lab) == "low"]
        if not low and not over:
            return "Всё в цели"
        if low:
            parts.append(f"Недобор — {', '.join(low).lower()}")
    if over:
        parts.append(f"Перебор — {', '.join(over).lower()}")
    return " · ".join(parts)


# ---------- модель ширины ----------

_TAG = re.compile(r"<[^>]+>")
_EMOJI_TAG = re.compile(r"<tg-emoji\b[^>]*>.*?</tg-emoji>", re.S)
_UNICODE_EMOJI = re.compile("[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f2ff]")


def text_width(s: str) -> float:
    """Ширина строки в у.е. Эмодзи (и кастом, и юникод) — ДВОЙНОЙ ширины: это и топит вёрстку."""
    count_em = len(_EMOJI_TAG.findall(s))
    plain = _TAG.sub("", _EMOJI_TAG.sub("", s))
    count_em += len(_UNICODE_EMOJI.findall(plain))
    plain = _UNICODE_EMOJI.sub("", plain)
    w = float(count_em * _W_EMOJI)
    for ch in plain:
        if ch.isdigit():
            w += _W_DIGIT
        elif ch == " ":
            w += _W_SPACE
        elif ch in "·—–/%+-−.,:()≈":
            w += _W_THIN
        else:
            w += _W_CYR
    return w


def lines_of(html: str) -> list[str]:
    """Разбить готовый HTML на визуальные строки: <br> и границы блоков.

    Строка таблицы = сумма ячеек: так тест ловит и распухшую ячейку, и распухший <tr> целиком.
    """
    rows: list[str] = []
    for tr in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        rows.append(" ".join(re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)))
    body = re.sub(r"<table\b.*?</table>", "", html, flags=re.S)
    blocks = re.split(
        r"</?(?:p|h[1-6]|li|summary|footer|aside|cite|blockquote|details|ul|ol|hr\s*/?)[^>]*>",
        body,
    )
    for chunk in blocks:
        rows.extend(part for part in chunk.split("<br>") if part.strip())
    return [r for r in rows if _TAG.sub("", r).strip()]
