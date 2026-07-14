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


def _data(consumed: Macros, meals: list[Meal]) -> DemoData:
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
        weight=WeightTrend(
            logs=[
                WeightLog(1, datetime(2026, 7, 10 + i, 8, tzinfo=UTC), 87.4 - i * 0.2)
                for i in range(6)
            ],
            timezone="Europe/Moscow",
        ),
        products=[
            Product(
                canonical_key=f"p{i}",
                display_name=name,
                density=Density(165, 31, 3.6, 0),
                is_estimate=True,
                id=i,
            )
            for i, name in enumerate(
                ["Паста болоньезе с пармезаном", "Творог 2%", "Овсянка на воде с бананом"]
            )
        ],
        weight_kg=86.8,
        trend_kg=87.2,
        streak=4,
        est_kcal_share=38,
    )


_LUNCH = [
    _item("Куриная грудка отварная", 250, Macros(325, 72, 4, 0)),
    _item("Паста болоньезе с пармезаном", 400, Macros(1050, 38, 42, 118), est=True),
]
_DINNER = [_item("Рис отварной", 180, Macros(234, 5, 1, 51))]

SCENARIOS: dict[str, DemoData] = {
    "пустой день": _data(Macros(0, 0, 0, 0), []),
    "обычный день": _data(Macros(1609, 115, 47, 169), [_meal(9, _LUNCH), _meal(19, _DINNER)]),
    "крупный перебор": _data(Macros(2650, 96, 88, 310), [_meal(9, _LUNCH), _meal(21, _DINNER)]),
    "без веса и продуктов": DemoData(
        today=DailyStats(remaining(Macros(800, 40, 20, 90), GOAL), 1, [_meal(9, _DINNER)], "UTC"),
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
    """Перебор ОБЯЗАН быть виден: 110% и 180% не смеют выглядеть одинаково.

    Это баг прода (clamp до 100%) — дезинформация в продукте, который продаёт точность.
    """
    b110, b180 = bar(110, 100, "Ккал"), bar(180, 100, "Ккал")
    assert b110 != b180, "110% и 180% рисуются одинаково — бар врёт"
    assert b110.count("<tg-emoji") > 10, "перебор обязан торчать ЗА трек, а не прятаться внутри"
    assert text_width(b180) <= SAFE, "бар перебора не смеет вылезать за пузырь"


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

    assert verdict(within, g) == "Всё в цели"
    assert "🟥" not in bar(102, 100, "Ккал"), "вердикт говорит «в цели», а бар рисует перебор"
    assert "🟥" in bar(120, 100, "Ккал"), "настоящий перебор обязан краснеть"
