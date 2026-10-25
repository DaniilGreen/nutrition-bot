"""«📈 Неделя» в боте — «Итоги как в аппке» (FR-48, D-39, AC-222…AC-232). Только фейки, без БД.

Главное правило, которое тут стережём: какой день «вошёл», решает баланс (§6.23) — полоса,
«в цели K из M» и средние считают ровно его дни, а не свои.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.bot.ui import blocks, kit, screens
from app.web.schemas import week_json
from tests.unit.test_ui_widths import CTXS, WEEK, week_ctx

NB = kit.NB


def _html(name: str) -> str:
    return screens.week(WEEK[name])


def _strip(html: str) -> str:
    """Полоса дней фолбэками: первый абзац ровно из семи кастом-эмодзи."""
    row = re.findall(r"<p>((?:<tg-emoji[^>]*>.*?</tg-emoji>){7})</p>", html)[0]
    return "".join(re.findall(r">(.)</tg-emoji>", row))


def _macro(html: str, label: str) -> str:
    return re.findall(rf"<b>{label}</b> · ([^<]*)", html)[0]


def test_пятница_в_плане() -> None:
    """AC-222: пятница перебрала цель — вошла в баланс, значит красная и в счёте, и в среднем."""
    html = _html("пятница в плане")
    assert "<h3>📈 Неделя · 14–20 сен</h3>" in html
    assert f"<h2>1{NB}124 ккал в запасе</h2>" in html
    assert "в плане ✅ · <i>записано 4 из 4 прошедших дней</i>" in html
    assert _strip(html) == "🟩🟩🟩🟩🟥⬜⬜"
    assert "Пн → Вс · в цели 4 из 5" in html
    assert "<b>В среднем за 5 дн.</b>" in html
    w = WEEK["пятница в плане"].week_bal
    assert w is not None and round(w.average_kcal) == 1951
    assert _macro(html, "Ккал").startswith("1951 / 2176")  # число аппки average_kcal
    assert "<b>Жир</b>" not in html and "<b>Углеводы</b>" not in html  # без баров
    assert "🟢 Жир" in html and "🟡 Углеводы" in html
    assert f"<footer>До воскресенья по 2{NB}738 — неделя сойдётся.</footer>" in html
    assert "<table" not in html and "Цель за" not in html


def test_средние_только_по_вошедшим_дням() -> None:
    """Среднее — по дням баланса; сегодняшний неполный день его не тянет (как average_kcal)."""
    d = WEEK["утро среды"]  # пн 2000, вт 2100, сегодня 400 — не вошло
    html = screens.week(d)
    assert "<b>В среднем за 2 дн.</b>" in html
    assert _macro(html, "Ккал").startswith("2050 /")  # (2000 + 2100) / 2, без 400
    assert _macro(html, "Белок").startswith(f"{round(2050 * 0.07)} /")  # те же дни, из КБЖУ дней
    assert d.week7.avg_macros.kcal < 2000  # week7 тянул бы сегодняшние 400 вниз — им не пользуемся


def test_утро_сегодня_не_судим() -> None:
    """AC-228: сегодня 400 из 2176 — не жёлтое и вне «K из M»."""
    html = _html("утро среды")
    assert _strip(html) == "🟩🟩⬜⬜⬜⬜⬜"
    assert "Пн → Вс · в цели 2 из 2" in html


def test_сегодня_перебор_красное_и_в_счёте() -> None:
    """Сегодня уже выше цели — вошло в баланс: красное, в M, в среднем (AC-222, п.5 ревью)."""
    html = _html("перебор с числом")  # пн 2600, вт 2700, ср (сегодня) 2500 > 2176
    assert _strip(html) == "🟥🟥🟥⬜⬜⬜⬜"
    assert "в цели 0 из 3" in html and "<b>В среднем за 3 дн.</b>" in html
    assert f"<h2>перебор 1{NB}272 ккал</h2>" in html and "в плане" not in html
    assert f"До воскресенья по 1{NB}858 — неделя сойдётся." in html


def test_воскресенье_сегодня_можно_до() -> None:
    """AC-223: в воскресенье — тот же экран, подвал про сегодня; воскресного итога нет."""
    html = _html("полная неделя · вс")
    assert "506 ккал в запасе" in html and "записано 6 из 6 прошедших дней" in html
    assert f"Сегодня можно до 2{NB}682 — неделя сойдётся." in html
    assert "Итог" not in html and "Цель за" not in html
    # чт 2200 и сб 2300 — выше цели 2176: красные ровно как WeekDay.over, не зелёные по /streak
    assert _strip(html) == "🟩🟩🟩🟥🟩🟥⬜"
    assert "в цели 4 из 6" in html


def test_мало_записей_в_воскресенье() -> None:
    """AC-223: воскресенье, «мало записей» — режим 7 дней, подвал называет неделю баланса."""
    html = _html("мало записей · вс")
    assert "ккал в день</h2>" in html and "в запасе" not in html
    assert "записано 2 из 6 дней" in html  # прошедшие дни окна, без сегодня
    assert "<footer>Баланс за 14–20 сен: мало записей.</footer>" in html


def test_суббота_вошла_в_баланс_завтра() -> None:
    assert "Завтра можно до" in _html("суббота вошла")


def test_понедельник_семь_дней_без_сегодня() -> None:
    """AC-224: числа недели нет — героем среднее прошлых дней, окно скользящее и с датами."""
    html = _html("понедельник")  # прошлая неделя по 2000, сегодня 1500 — не вошло
    assert f"<h2>2{NB}000 ккал в день</h2>" in html
    assert f"цель 2{NB}176 · <i>записано 6 из 6 дней</i>" in html
    assert "<h3>📈 Неделя · 8–14 сен</h3>" in html
    assert _strip(html).endswith("⬜") and "Вт → сегодня · в цели 6 из 6" in html
    assert "<b>В среднем за 6 дн. · 8–13 сен</b>" in html
    assert "<footer>Один день — шум. Управляй средней.</footer>" in html


def test_перебор_ниже_пола() -> None:
    """AC-225: ниже пола — «не догнать», числа нет; и в пятницу, и в субботу."""
    for name in ("перебор ниже пола", "пол в субботу"):
        html = _html(name)
        assert "<h2>перебор" in html and "в плане" not in html
        assert "<footer>Неделю не догнать — ешь по обычной цели.</footer>" in html
        assert "можно до" not in html and "🔴 Жир" in html


def test_мало_записей_и_пусто() -> None:
    """AC-226: «мало записей» — и почему; пусто; только сегодняшний завтрак."""
    html = _html("мало записей")
    assert "записано 1 из 6 дней" in html and "В среднем за 1 дн. · 14 сен" in html
    assert "<footer>Баланс за 14–20 сен: мало записей.</footer>" in html
    assert "За 7 дней записей нет." in _html("пусто") and "<h2>" not in _html("пусто")
    only = _html("только сегодня")
    assert "За прошлые дни записей нет.<br>Среднее появится завтра." in only


def test_смена_цели_посреди_недели() -> None:
    """AC-229: полоса красит по цели ТОГО дня — как баланс и столбики аппки."""
    d = WEEK["смена цели"]
    w = d.week_bal
    assert w is not None and w.balance_kcal == -100
    assert [x.over for x in w.days[:3]] == [False, False, True]
    html = screens.week(d)
    assert _strip(html) == "🟩🟩🟥⬜⬜⬜⬜"  # пн, вт ниже своей 2500; ср выше своей 2000
    assert "в цели 2 из 3" in html
    # бар ккал — к средней цели тех же дней (2500+2500+2000)/3, а не к сегодняшней 2000
    assert _macro(html, "Ккал").startswith("2300 / 2333") and "<b>99%</b>" in html


def test_регистрация_посреди_недели_бот_и_аппка_совпадают() -> None:
    """AC-230: «из M прошедших» — с регистрации; то же число отдаёт /api/week, и аппка берёт его."""
    d = WEEK["регистрация в среду"]
    html = screens.week(d)
    assert "записано 2 из 2 прошедших дней" in html  # не «из 4»
    assert _strip(html) == "⬜⬜🟩🟩⬜⬜⬜"
    assert d.week_bal is not None and week_json(d.week_bal)["past_days"] == 2
    js = Path("web/app.js").read_text(encoding="utf-8")
    assert "var past = w.past_days;" in js  # аппка не считает «прошедшие» сама


def test_ровно_ноль() -> None:
    """AC-231: баланс 0 — «ровно по плану» без бейджа, и в вечернем итоге так же."""
    d = WEEK["ровно ноль"]
    html = screens.week(d)
    assert "<h2>ровно по плану</h2>" in html and "в плане ✅" not in html
    assert "<p><i>записано 2 из 2 прошедших дней</i></p>" in html
    assert "🗓 <b>Неделя</b> · ровно по плану" in blocks.week_balance(d)


def test_числа_одним_видом_в_итоге_и_на_неделе() -> None:
    """AC-232: 1 124 и 2 738 — с узким пробелом и в вечернем итоге, и на «Неделе»."""
    d = WEEK["пятница в плане"]
    ev, wk = blocks.week_balance(d), screens.week(d)
    for num in (f"1{NB}124", f"2{NB}738"):
        assert num in ev and num in wk


def test_через_границу_месяца() -> None:
    assert "<h3>📈 Неделя · 26 окт – 1 ноя</h3>" in _html("через месяц")


def test_без_недельного_баланса() -> None:
    """week_bal не посчитан (None) — режим 7 дней, экран не падает."""
    html = screens.week(CTXS["обычный день"])
    assert "ккал в день</h2>" in html and "Чт → сегодня" in html


def test_один_прошедший_день_по_русски() -> None:
    html = screens.week(week_ctx([2000, 1900, None, None, None, None, None], 1))
    assert "записано 1 из 1 прошедшего дня" in html


def test_кружок_по_тем_же_порогам_что_бар() -> None:
    """Кружок жира красный ровно тогда, когда бар был бы красным (kit.status, 105%)."""
    assert screens._dot("Жир", 76, 72).startswith("🔴")  # 105.6%
    assert screens._dot("Жир", 75, 72).startswith("🟢")  # 104.2%
    assert kit.status(76, 72, "Жир") == "over"
