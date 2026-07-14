"""Гарантия «ничего не съедет»: КАЖДАЯ визуальная строка КАЖДОГО экрана влезает в ширину пузыря.

Четыре итерации вёрстки уехали ровно потому, что ширину проверяли глазами по скриншоту. Модель
ширины (kit.text_width) откалибрована по реальным скриншотам iPhone: пузырь ≈ 760 у.е., кастом-
эмодзи ≈ 46 у.е. (двойная ширина — они и топят вёрстку), кириллица ≈ 18, цифра ≈ 16.

Данные СИНТЕТИЧЕСКИЕ и намеренно злые: перебор, длинные названия блюд, пустой день, полное
отсутствие веса и продуктов. Не влезло здесь — не влезет и в жизни.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime

import pytest

from app.application.entities import Meal, MealItem, Product, WeightLog
from app.application.results import DailyStats, MonthReport, WeightTrend
from app.bot.demo import DESIGNS
from app.bot.demo.data import DemoData
from app.bot.demo.kit import BUBBLE, SAFE, bar, lines_of, text_width
from app.bot.demo.kit import macro_line as kit_macro_line
from app.domain.goals import Goal, remaining
from app.domain.value_objects import Density, Macros

GOAL = Goal(daily_kcal=2200, protein_g=165, fat_g=61, carb_g=248)
_WEEK_KCAL = (2050.0, 0.0, 1980.0, 2410.0, 1720.0, 2200.0)


def _item(name: str, grams: float, m: Macros, *, est: bool = False) -> MealItem:
    return MealItem(name_snapshot=name, grams=grams, macros=m, is_estimate=est, id=1)


def _meal(hour: int, items: list[MealItem]) -> Meal:
    totals = Macros(
        sum(i.macros.kcal for i in items),
        sum(i.macros.protein for i in items),
        sum(i.macros.fat for i in items),
        sum(i.macros.carb for i in items),
    )
    return Meal(
        user_id=1,
        eaten_at=datetime(2026, 7, 15, hour, 30, tzinfo=UTC),
        input_type="text",
        raw_input="—",
        items=items,
        source_confidence=0.9,
        totals=totals,
        id=hour,
    )


def _data(meals: list[Meal], *, hour: int = 20) -> DemoData:
    """consumed СЧИТАЕМ ИЗ meals. Если задавать его отдельно, экран показывает два разных итога
    дня («Лента: 800 ккал» в шапке и «всего за день: 234» в ленте), а σ считается по чужим
    позициям. Демо, где числа не бьются, оценивать бессмысленно."""
    consumed = Macros(
        sum(i.macros.kcal for m in meals for i in m.items),
        sum(i.macros.protein for m in meals for i in m.items),
        sum(i.macros.fat for m in meals for i in m.items),
        sum(i.macros.carb for m in meals for i in m.items),
    )
    days = [(date(2026, 7, 9 + i), kcal) for i, kcal in enumerate(_WEEK_KCAL)]
    days.append((date(2026, 7, 15), consumed.kcal))
    avg = Macros(2060, 140, 70, 210)
    return DemoData(
        today=DailyStats(
            remaining=remaining(consumed, GOAL),
            meals_count=len(meals),
            meals=meals,
            timezone="Europe/Moscow",
        ),
        days=days,
        goal=GOAL,
        week7=MonthReport(7, 6, 4, avg, -0.4),
        month=MonthReport(30, 24, 17, avg, -1.6),
        # logs — NEWEST-FIRST, как отдаёт прод (repositories: order_by(measured_at.desc()))
        weight=WeightTrend(
            logs=[
                WeightLog(1, datetime(2026, 7, 15 - i, 8, tzinfo=UTC), 86.4 + i * 0.2)
                for i in range(6)
            ],
            timezone="Europe/Moscow",
        ),
        products=[
            Product(canonical_key=f"p{i}", display_name=nm, density=den, is_estimate=True, id=i)
            for i, (nm, den) in enumerate(
                [
                    ("Паста болоньезе с пармезаном", Density(320, 11, 12, 40)),
                    ("Творог 2%", Density(103, 18, 2, 3.3)),
                    ("Овсянка на воде с бананом", Density(88, 3, 1.5, 16)),
                ]
            )
        ],
        weight_kg=86.8,
        trend_kg=87.2,
        streak=4,
        est_kcal_share=38,
        local_hour=hour,
    )


_LUNCH = [
    _item("Куриная грудка отварная", 250, Macros(325, 72, 4, 0)),
    _item("Паста болоньезе с пармезаном", 400, Macros(1050, 38, 42, 118), est=True),
]
_DINNER = [_item("Рис отварной", 180, Macros(234, 5, 1, 51))]

_HUGE = [_item("Пицца пепперони", 800, Macros(2100, 84, 96, 220), est=True)]

SCENARIOS: dict[str, DemoData] = {
    "пустой день": _data([]),
    "обычный день": _data([_meal(9, _LUNCH), _meal(19, _DINNER)]),
    "крупный перебор": _data([_meal(9, _LUNCH), _meal(21, _HUGE)]),
    "утро": _data([_meal(9, _DINNER)], hour=9),  # совет добора не смеет тут сработать
    "без веса и продуктов": DemoData(
        today=DailyStats(remaining(Macros(234, 5, 1, 51), GOAL), 1, [_meal(9, _DINNER)], "UTC"),
        days=[(date(2026, 7, 9 + i), 0.0) for i in range(7)],
        goal=GOAL,
        week7=MonthReport(7, 0, 0, Macros(0, 0, 0, 0), None),
        month=MonthReport(30, 0, 0, Macros(0, 0, 0, 0), None),
        weight=WeightTrend(logs=[], timezone="UTC"),
        products=[],
        weight_kg=None,
        trend_kg=None,
        streak=0,
        est_kcal_share=0,
        local_hour=20,
    ),
}


@pytest.mark.parametrize("design", sorted(DESIGNS))
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_bar_lines_never_wrap(design: str, scenario: str) -> None:
    """СТРОКА С БАРОМ обязана влезать целиком. Перенос бара = он разрывается пополам — «уехало».

    Обычный абзац переноситься МОЖЕТ и должен: это типографика, а не дефект. Жёсткое требование
    только к тому, что переносом ломается, — к бару и к строкам таблицы (см. соседний тест).
    """
    _, build = DESIGNS[design]
    for title, html in build(SCENARIOS[scenario]):
        for line in lines_of(html):
            if "<tg-emoji" not in line:
                continue  # текст пусть переносится, ему можно
            width = text_width(line)
            assert width <= SAFE, (
                f"{design}/{scenario}/{title}: бар {width:.0f} у.е. > {SAFE} — разорвётся"
            )


@pytest.mark.parametrize("design", sorted(DESIGNS))
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_table_rows_fit(design: str, scenario: str) -> None:
    """СТРОКА ТАБЛИЦЫ обязана влезать: не влезла — колонки сжимаются и содержимое переносится
    вразнобой. Ровно это и было «опять криво» на всех четырёх скриншотах."""
    _, build = DESIGNS[design]
    for title, html in build(SCENARIOS[scenario]):
        for tr in re.findall(r"<tr>(.*?)</tr>", html, re.S):
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)
            width = sum(text_width(c) for c in cells) + 46 * len(cells)  # +паддинги ячеек
            assert width <= BUBBLE, (
                f"{design}/{scenario}/{title}: строка таблицы {width:.0f} у.е. > {BUBBLE}\n"
                f"  {' | '.join(re.sub('<[^>]+>', '', c) for c in cells)}"
            )


@pytest.mark.parametrize("design", sorted(DESIGNS))
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_no_bar_inside_table_cell(design: str, scenario: str) -> None:
    """ПРАВИЛО 1 кита: бар в ячейке таблицы — источник всех четырёх «уехало». Запрещено навсегда."""
    _, build = DESIGNS[design]
    for title, html in build(SCENARIOS[scenario]):
        for cell in re.findall(r"<td[^>]*>(.*?)</td>", html, re.S):
            assert "<tg-emoji" not in cell, f"{design}/{scenario}/{title}: бар внутри <td>"


@pytest.mark.parametrize("design", sorted(DESIGNS))
def test_all_sections_render(design: str) -> None:
    """Все разделы существуют и непусты — «полное демо» значит полное."""
    _, build = DESIGNS[design]
    out = build(SCENARIOS["обычный день"])
    assert len(out) == 10, f"{design}: разделов {len(out)}, ожидалось 10"
    for title, html in out:
        assert html.strip(), f"{design}/{title}: пустой экран"


def test_overflow_bar_shows_magnitude() -> None:
    """Перебор различим в пределах шкалы, а за её краем — ЧЕСТНО ПОМЕЧЕН, а не замолчан.

    Бар фиксированной ширины физически не может отличить 180% от 300% — это свойство любой шкалы,
    а не баг. Ложью было МОЛЧАЛИВОЕ насыщение: прошлая версия рисовала 140%, 180% и 300% одинаково
    и ничем этого не выдавала, а прошлый тест сравнивал только 110 vs 180 и потому был зелёным.
    Кламп, объявленный дезинформацией, просто переехал со 100% на 140%.

    Контракт теперь такой: в пределах шкалы бар пропорционален; за краем — ставит «…»; точную
    величину ВСЕГДА несёт процент рядом (см. kit.macro_line).
    """
    assert bar(110, 100, "Ккал") != bar(140, 100, "Ккал"), "в пределах шкалы бар обязан расти"
    assert not bar(140, 100, "Ккал").endswith("…"), "140% в пределах шкалы — метки быть не должно"
    assert bar(180, 100, "Ккал").endswith("…"), "за краем шкалы бар обязан признаться, что насыщен"
    assert bar(300, 100, "Ккал").endswith("…")
    for p in (110, 140, 180, 250, 300):
        assert text_width(bar(p, 100, "Ккал")) <= SAFE, f"бар при {p}% вылезает за пузырь"
    assert "300%" in kit_macro_line("Ккал", 300, 100), "точную величину обязан нести процент"


def test_overflow_bar_is_not_green() -> None:
    """Перебор не смеет краситься зелёным.

    Был баг: первые 10 сегментов рисовались зелёными («цель же взята!»), и при 144% жира 71%
    полосы был ЗЕЛЁНЫМ — провал выглядел успехом. Цвет = наполнение, как и просил владелец.
    """
    assert "🟩" not in bar(144, 100, "Жир"), "перелил — вся полоса красная, без зелёного"
    assert "🟥" in bar(144, 100, "Жир")


def test_protein_overshoot_is_not_punished() -> None:
    """У БЕЛКА перебора не бывает: превышение безвредно. Красить 120% белка красным — значит
    ругать человека за единственное, что он сделал правильно."""
    assert "🟥" not in bar(120, 100, "Белок"), "перебор белка покрашен как провал"
    assert "🟥" in bar(120, 100, "Ккал"), "а вот перебор калорий краснеть обязан"


def test_bar_agrees_with_verdict() -> None:
    """Бар и вердикт обязаны жить по ОДНОМУ порогу перебора.

    Был баг: при 102% вердикт писал «Всё в цели» (допуск 5%), а бар уже рисовал красный хвост —
    экран спорил сам с собой. Хуже прямой ошибки: пользователь перестаёт верить обоим.
    """
    from app.domain.goals import Goal
    from app.domain.value_objects import Macros

    g = Goal(daily_kcal=100, protein_g=100, fat_g=100, carb_g=100)
    within = Macros(kcal=102, protein=102, fat=102, carb=102)  # +2% — это ещё «в цели»
    from app.bot.demo.kit import verdict

    assert verdict(within, g, final=True) == "Всё в цели"
    assert "🟥" not in bar(102, 100, "Ккал"), "вердикт говорит «в цели», а бар рисует перебор"
    assert "🟥" in bar(120, 100, "Ккал"), "настоящий перебор обязан краснеть"


def test_no_advice_in_the_morning() -> None:
    """Утром совет добора НЕ ДОЛЖЕН срабатывать.

    Был реальный абсурд: в 09:30, после одного приёма, бот писал «Добери 125 г белка — это 400 г
    куриной грудки». Впереди ещё два приёма, разрыв закроется сам. Совет уместен, когда день почти
    прожит, — иначе фича превращается в шум и её перестают читать.
    """
    from app.bot.demo import blocks

    morning, evening = SCENARIOS["утро"], SCENARIOS["обычный день"]
    assert not morning.advice_due, "в 9 утра советовать добор нельзя"
    assert blocks.advice(morning) == "", "утром бот обязан молчать про добор"
    assert evening.advice_due, "вечером совет обязан быть доступен"


def test_no_verdict_of_undereating_during_the_day() -> None:
    """Днём бот не смеет писать «Недобор».

    В обед человек съел 1600 из 2200 — он просто ещё не ужинал. «Недобор — ккал, белок, жир,
    углеводы» в 13:00 обвиняет его в четырёх провалах на пустом месте. Перебор — другое дело:
    он уже случился и сам не рассосётся.
    """
    from app.bot.demo.kit import verdict

    half = Macros(1100, 60, 25, 120)  # половина дня
    assert "Недобор" not in verdict(half, GOAL, final=False)
    assert "Недобор" in verdict(half, GOAL, final=True), "вечером — обязан сказать честно"
    blown = Macros(2600, 60, 90, 300)
    assert "Перебор" in verdict(blown, GOAL, final=False), "перебор виден в любое время суток"
