"""Геометрия единого дизайна: КАЖДАЯ строка КАЖДОГО экрана влезает в ширину пузыря.

Четыре итерации вёрстки уехали потому, что ширину проверяли глазами. Модель kit.text_width
откалибрована по скриншотам iPhone (пузырь ≈ 760 у.е., кастом-эмодзи ≈ 46 — двойная ширина).
Данные синтетические и злые: перебор, длинные названия, пустой день, утро, отсутствие веса.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime

import pytest

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.entities import Meal, MealItem, Product, WeightLog
from app.application.results import (
    DailyStats,
    DayView,
    ItemAdjusted,
    ItemRemoved,
    LabelRead,
    MealLogged,
    MonthReport,
    WeightTrend,
)
from app.bot.ui import screens
from app.bot.ui.context import DayContext
from app.bot.ui.kit import BUBBLE, SAFE, bar, lines_of, macro_line, text_width, verdict
from app.domain.goals import Goal, remaining
from app.domain.value_objects import Density, Macros

GOAL = Goal(daily_kcal=2200, protein_g=165, fat_g=61, carb_g=248)
_WEEK = (2050.0, 0.0, 1980.0, 2410.0, 1720.0, 2200.0)


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


_PRODUCTS = [
    Product(canonical_key=f"p{i}", display_name=nm, density=den, is_estimate=True, id=i)
    for i, (nm, den) in enumerate(
        [
            ("Паста болоньезе с пармезаном", Density(320, 11, 12, 40)),
            ("Творог 2%", Density(103, 18, 2, 3.3)),
            ("Овсянка на воде с бананом", Density(88, 3, 1.5, 16)),
        ]
    )
]


def _ctx(meals: list[Meal], *, hour: int = 20, has_weight: bool = True) -> DayContext:
    consumed = Macros(
        sum(i.macros.kcal for m in meals for i in m.items),
        sum(i.macros.protein for m in meals for i in m.items),
        sum(i.macros.fat for m in meals for i in m.items),
        sum(i.macros.carb for m in meals for i in m.items),
    )
    days = [(date(2026, 7, 9 + i), kcal) for i, kcal in enumerate(_WEEK)]
    days.append((date(2026, 7, 15), consumed.kcal))
    avg = Macros(2060, 140, 70, 210)
    logs = (
        [WeightLog(1, datetime(2026, 7, 15 - i, 8, tzinfo=UTC), 86.4 + i * 0.2) for i in range(6)]
        if has_weight
        else []
    )
    return DayContext(
        today=DailyStats(remaining(consumed, GOAL), len(meals), meals, "Europe/Moscow"),
        days=days,
        goal=GOAL,
        week7=MonthReport(7, 6, 4, avg, -0.4),
        month=MonthReport(30, 24, 17, avg, -1.6),
        weight=WeightTrend(logs, "Europe/Moscow"),
        products=_PRODUCTS if has_weight else [],
        weight_kg=86.8 if has_weight else None,
        trend_kg=87.2 if has_weight else None,
        local_hour=hour,
        est_kcal_share=38,
    )


_LUNCH = [
    _item("Куриная грудка отварная", 250, Macros(325, 72, 4, 0)),
    _item("Паста болоньезе с пармезаном", 400, Macros(1050, 38, 42, 118), est=True),
]
_DINNER = [_item("Рис отварной", 180, Macros(234, 5, 1, 51))]
_HUGE = [_item("Пицца пепперони", 800, Macros(2100, 84, 96, 220), est=True)]

CTXS: dict[str, DayContext] = {
    "пустой день": _ctx([]),
    "обычный день": _ctx([_meal(9, _LUNCH), _meal(19, _DINNER)]),
    "крупный перебор": _ctx([_meal(9, _LUNCH), _meal(21, _HUGE)]),
    "утро": _ctx([_meal(9, _DINNER)], hour=9),
    "без веса": _ctx([_meal(9, _DINNER)], has_weight=False),
}


def _label() -> LabelRead:
    it = ParsedItem(
        name="Творог 5%",
        canonical_key="tvorog",
        unit=Unit.GRAM,
        kcal_per_100g=121,
        protein_per_100g=16,
        fat_per_100g=5,
        carb_per_100g=3,
        unit_weight_g=200,
    )
    draft = ParsedMessage(intent=Intent.MEAL, items=[it])
    return LabelRead(draft=draft, item_index=0, atwater_warning=True)


def _dayview(meals: list[Meal]) -> DayView:
    return DayView(meals=meals, timezone="Europe/Moscow", days_ago=1, day=date(2026, 7, 14))


def _all_screens(ctx: DayContext) -> list[tuple[str, str]]:
    """Каждый экран единого дизайна на данном контексте — то, что реально уходит в Telegram."""
    rem = ctx.today.remaining
    out = [
        ("today", screens.today(ctx)),
        ("evening", screens.evening(ctx)),
        ("week", screens.week(ctx)),
        ("month", screens.month(ctx)),
        ("weight", screens.weight(ctx)),
        ("goal", screens.goal_screen(ctx.goal, ctx.weight_kg)),
        ("products", screens.products(ctx.products)),
        ("streak", screens.streak(ctx.days, ctx.goal.daily_kcal)),
        ("day", screens.day(_dayview(ctx.today.meals), rem)),
        ("label", screens.label(_label())),
        ("start", screens.start(onboarded=True)),
        ("help", screens.help_()),
        ("item_removed", screens.item_removed(ItemRemoved(items=_DINNER, remaining=rem))),
    ]
    if ctx.today.meals:
        mlog = MealLogged(meal=ctx.today.meals[-1], remaining=rem)
        out.append(("meal_card", screens.meal_card(ctx, mlog)))
        it = ctx.today.meals[-1].items[0]
        adj = ItemAdjusted(item=it, old_grams=250, remaining=rem)
        out.append(("item_adjusted", screens.item_adjusted(adj)))
    return out


@pytest.mark.parametrize("scenario", sorted(CTXS))
def test_bar_lines_never_wrap(scenario: str) -> None:
    """Строка С БАРОМ обязана влезать целиком: перенос бара = он разрывается пополам («уехало»)."""
    for name, html in _all_screens(CTXS[scenario]):
        for line in lines_of(html):
            if "<tg-emoji" not in line:
                continue  # текст переносится — это типографика, а не дефект
            width = text_width(line)
            assert width <= SAFE, f"{scenario}/{name}: бар {width:.0f} у.е. > {SAFE}\n{line}"


@pytest.mark.parametrize("scenario", sorted(CTXS))
def test_table_rows_fit(scenario: str) -> None:
    """Строка таблицы обязана влезать: не влезла — колонки сжимаются, содержимое едет вразнобой."""
    for name, html in _all_screens(CTXS[scenario]):
        for tr in re.findall(r"<tr>(.*?)</tr>", html, re.S):
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)
            width = sum(text_width(c) for c in cells) + 46 * len(cells)
            assert width <= BUBBLE, f"{scenario}/{name}: строка таблицы {width:.0f} > {BUBBLE}"


@pytest.mark.parametrize("scenario", sorted(CTXS))
def test_no_bar_inside_table_cell(scenario: str) -> None:
    """ПРАВИЛО 1 кита: бар в ячейке таблицы — источник всех «уехало». Запрещён навсегда."""
    for name, html in _all_screens(CTXS[scenario]):
        for cell in re.findall(r"<td[^>]*>(.*?)</td>", html, re.S):
            assert "<tg-emoji" not in cell, f"{scenario}/{name}: бар внутри <td>"


@pytest.mark.parametrize("scenario", sorted(CTXS))
def test_every_screen_nonempty(scenario: str) -> None:
    for name, html in _all_screens(CTXS[scenario]):
        assert html.strip(), f"{scenario}/{name}: пустой экран"


# ---------- поведение (то, что чинили по ревью) ----------


def test_overflow_bar_shows_magnitude() -> None:
    """Перебор различим в пределах шкалы, за краем — помечен «…», а не замолчан."""
    assert bar(110, 100, "Ккал") != bar(140, 100, "Ккал")
    assert not bar(140, 100, "Ккал").endswith("…")
    assert bar(180, 100, "Ккал").endswith("…")
    for p in (110, 140, 180, 300):
        assert text_width(bar(p, 100, "Ккал")) <= SAFE
    assert "300%" in macro_line("Ккал", 300, 100)


def test_overflow_is_red_not_green() -> None:
    """Перелил → вся полоса красная (был баг: зелёный трек с красным хвостиком)."""
    assert "🟩" not in bar(144, 100, "Жир")
    assert "🟥" in bar(144, 100, "Жир")


def test_protein_overshoot_not_punished() -> None:
    """У белка перебора не бывает — 120% белка не смеет краснеть."""
    assert "🟥" not in bar(120, 100, "Белок")
    assert "🟥" in bar(120, 100, "Ккал")


def test_no_verdict_of_undereating_during_day() -> None:
    """Днём «Недобор» не выдумываем — человек ещё не поужинал."""
    half = Macros(1100, 60, 25, 120)
    assert "Недобор" not in verdict(half, GOAL, final=False)
    assert "Недобор" in verdict(half, GOAL, final=True)
    assert "Перебор" in verdict(Macros(2600, 60, 90, 300), GOAL, final=False)


def test_no_advice_in_the_morning() -> None:
    """Утром совет добора молчит — впереди ещё приёмы, разрыв закроется сам."""
    from app.bot.ui import blocks

    assert not CTXS["утро"].advice_due
    assert blocks.advice(CTXS["утро"]) == ""
    assert CTXS["обычный день"].advice_due


# --- подписи кнопок-вариантов: отличие должно быть ВИДНО, а не уехать в «…» ---


def test_variant_labels_show_the_difference_not_the_common_prefix() -> None:
    """Живой баг: «ржаные сухарики без в…» / «со вк…» / «с сыр…» — все кнопки на вид одинаковы.

    Резали МЫ (22 символа), а не Telegram. Варианты от LLM — полные имена (D-19), поэтому
    отличаются хвостом, и обрезка съедала ровно его. Продукт назван в вопросе над кнопками.
    """
    from app.bot.keyboards import _distinct

    labels = _distinct(
        [
            "ржаные сухарики без вкусовых добавок",
            "ржаные сухарики со вкусом бекона",
            "ржаные сухарики с сыром",
        ]
    )
    assert labels == ["без вкусовых добавок", "со вкусом бекона", "с сыром"]
    assert len(set(labels)) == 3  # главное: кнопки различимы
    assert all(len(x) <= 34 for x in labels)  # и влезают без «…»


def test_variant_labels_keep_a_word_when_one_option_is_the_prefix() -> None:
    """«кофе» / «кофе с молоком»: общее начало — весь первый вариант.

    Срезать его целиком нельзя — кнопка станет пустой.
    """
    from app.bot.keyboards import _distinct

    assert _distinct(["кофе", "кофе с молоком"]) == ["кофе", "кофе с молоком"]
    assert _distinct(["борщ", "солянка"]) == ["борщ", "солянка"]  # нет общего — не трогаем
    assert _distinct(["молоко 1.5%", "молоко 3.2%"]) == ["1.5%", "3.2%"]


def test_variant_keyboard_preserves_index_order() -> None:
    """Косметика не смеет сдвинуть индексы.

    Callback несёт номер, а в БД уходит ПОЛНОЕ имя из черновика — не подпись кнопки.
    """
    from app.bot.keyboards import variant_keyboard

    opts = ["тофу мягкий (силкен)", "тофу плотный (фирм, обычный)"]
    rows = variant_keyboard(opts).inline_keyboard
    assert [b.callback_data for r in rows for b in r][:2] == ["variant:0", "variant:1"]
    assert [b.text for r in rows for b in r][:2] == ["мягкий (силкен)", "плотный (фирм, обычный)"]
