"""§6.21 / MA-INV-39: инструменты ассистента видят только своего человека.

Граница доверия. Проверяем не «мы фильтруем аргумент», а что чужие данные
недостижимы в принципе: пользователь зашит в объект при создании, а в схеме
инструментов его нет вовсе.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.application.coach_data import MAX_DAYS, CoachData
from app.application.entities import Meal, MealItem, WeightLog
from app.application.stats_service import StatsService
from app.domain.goals import Goal
from app.domain.value_objects import Macros
from tests.fakes import (
    InMemoryMealRepo,
    InMemoryProductRepo,
    InMemoryUserRepo,
    InMemoryWeightRepo,
)


def _meal(user_id: int, when: datetime, kcal: float, name: str, *,
          product_id: int | None = None, estimated: bool = False) -> Meal:
    m = Macros(kcal, kcal * 0.2, kcal * 0.03, kcal * 0.05)
    return Meal(
        user_id=user_id, eaten_at=when, input_type="text", raw_input=name,
        items=[MealItem(name_snapshot=name, grams=200.0, macros=m,
                        product_id=product_id if product_id is not None else user_id,
                        is_estimate=estimated)],
        source_confidence=1.0, totals=m,
    )


async def _setup():
    users, meals, weights = InMemoryUserRepo(), InMemoryMealRepo(), InMemoryWeightRepo()
    mine = await users.get_or_create(telegram_id=1, name="Я", locale="ru", timezone="UTC")
    other = await users.get_or_create(telegram_id=2, name="Не я", locale="ru", timezone="UTC")
    now = datetime.now(UTC)
    await meals.add(_meal(mine.id, now - timedelta(hours=2), 500, "моя куриная грудка"))
    await meals.add(_meal(other.id, now - timedelta(hours=2), 900, "ЧУЖОЙ борщ"))
    await weights.add(WeightLog(user_id=mine.id, weight_kg=91.5, measured_at=now))
    await weights.add(WeightLog(user_id=other.id, weight_kg=70.0, measured_at=now))
    stats = StatsService(meals, users, Goal(2000, 150, 65, 250))
    return CoachData(mine, stats, meals, weights), mine, other


async def test_приёмы_дня_только_свои():
    data, _, _ = await _setup()
    today = (await data.day_totals(1))[-1]["date"]
    day = await data.meals_on(today)
    names = [i["name"] for m in day["meals"] for i in m["items"]]
    assert "моя куриная грудка" in names
    assert not any("ЧУЖОЙ" in n for n in names), names


async def test_вклад_продуктов_только_свой():
    data, _, _ = await _setup()
    rows = await data.top_products(30, "kcal")
    assert any("моя" in r["name"] for r in rows)
    assert not any("ЧУЖОЙ" in r["name"] for r in rows), rows


async def test_вес_только_свой():
    data, _, _ = await _setup()
    kgs = [r["kg"] for r in await data.weight_log(10)]
    assert 91.5 in kgs and 70.0 not in kgs, kgs


async def test_сводка_по_дням_содержит_бжу():
    """Ради этого половина затеи: раньше в контекст шли только ккал и белок,
    и «почему не добираю жиры» отвечать было нечем."""
    data, _, _ = await _setup()
    row = (await data.day_totals(3))[-1]
    assert {"date", "kcal", "protein_g", "fat_g", "carb_g", "meals"} <= set(row)


@pytest.mark.parametrize("asked,expect", [(500, MAX_DAYS), (0, 1), ("мусор", 14), (None, 14)])
async def test_аргументы_от_модели_зажимаются(asked, expect):
    """Аргумент приходит от модели: это ввод, а не константа. Запрос на 500 дней
    втащил бы в промпт всё и съел бюджет месяца."""
    data, _, _ = await _setup()
    assert len(await data.day_totals(asked)) <= expect


async def test_день_вне_диапазона_отвечает_словами_а_не_падает():
    data, _, _ = await _setup()
    ahead = await data.meals_on("2099-01-01")
    assert ahead["meals"] == [] and "note" in ahead


async def test_доверие_к_цифрам_считает_долю_и_называет_продукты():
    """Доля без списка бесполезна: «у тебя 63 % оценок» не говорит, с чего начать."""
    from app.application.entities import Product
    from app.domain.value_objects import Density

    users, meals = InMemoryUserRepo(), InMemoryMealRepo()
    weights, products = InMemoryWeightRepo(), InMemoryProductRepo()
    me = await users.get_or_create(telegram_id=1, name="Я", locale="ru", timezone="UTC")
    guess = await products.create(Product(
        canonical_key="pho_bo", display_name="фо бо",
        density=Density(60, 4, 2, 6), source="llm", is_estimate=True))
    exact = await products.create(Product(
        canonical_key="tvorog", display_name="творожок с этикетки",
        density=Density(90, 9, 5, 2), source="label", is_estimate=False))

    now = datetime.now(UTC)
    await meals.add(_meal(me.id, now, 900, "фо бо",
                          product_id=guess.id, estimated=True))
    await meals.add(_meal(me.id, now, 100, "творожок с этикетки",
                          product_id=exact.id, estimated=False))

    stats = StatsService(meals, users, Goal(2000, 150, 65, 250))
    data = CoachData(me, stats, meals, weights, products)
    acc = await data.accuracy(30)

    assert acc["share_estimated"] == pytest.approx(0.9, abs=0.01)
    assert acc["kcal_estimated"] == 900 and acc["kcal_total"] == 1000
    names = [p["name"] for p in acc["estimated_products"]]
    assert "фо бо" in names
    # Выверенное в список «что чинить» попадать не должно — чинить там нечего.
    assert "творожок с этикетки" not in names, names


async def test_без_каталога_доля_всё_равно_считается():
    """Каталог может быть не подключён — доля не должна из-за этого пропадать."""
    users, meals = InMemoryUserRepo(), InMemoryMealRepo()
    me = await users.get_or_create(telegram_id=1, name="Я", locale="ru", timezone="UTC")
    await meals.add(_meal(me.id, datetime.now(UTC), 500, "еда", estimated=True))
    stats = StatsService(meals, users, Goal(2000, 150, 65, 250))
    acc = await CoachData(me, stats, meals, InMemoryWeightRepo()).accuracy(30)
    assert acc["share_estimated"] == 1.0
    assert "estimated_products" not in acc


async def test_приём_несёт_бжу_а_не_только_ккал():
    """§6.21.2: без Б/Ж/У модель отвечала «в базе только ккал», хотя они есть."""
    data, _, _ = await _setup()
    today = (await data.day_totals(1))[-1]["date"]
    meal = (await data.meals_on(today))["meals"][0]
    assert {"protein_g", "fat_g", "carb_g"} <= meal.keys()
    assert {"protein_g", "fat_g", "carb_g"} <= meal["items"][0].keys()


async def test_непонятная_дата_это_сегодня_человека():
    """Модель прислала мусор вместо даты — берём «сегодня» по поясу человека."""
    data, _, _ = await _setup()
    today = (await data.day_totals(1))[-1]["date"]
    assert (await data.meals_on("вчера вечером"))["date"] == today
