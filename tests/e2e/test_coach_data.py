"""§6.21 / MA-INV-39: инструменты ассистента видят только своего человека.

Граница доверия. Проверяем не «мы фильтруем аргумент», а что чужие данные
недостижимы в принципе: пользователь зашит в объект при создании, а в схеме
инструментов его нет вовсе.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.application.coach_data import MAX_DAYS, CoachData
from app.application.entities import Meal, MealItem, User, WeightLog
from app.application.stats_service import StatsService
from app.domain.goals import Goal
from app.domain.value_objects import Macros
from tests.fakes import (
    InMemoryMealRepo,
    InMemoryUserRepo,
    InMemoryWeightRepo,
)


def _meal(user_id: int, when: datetime, kcal: float, name: str) -> Meal:
    m = Macros(kcal, kcal * 0.2, kcal * 0.03, kcal * 0.05)
    return Meal(
        user_id=user_id, eaten_at=when, input_type="text", raw_input=name,
        items=[MealItem(name_snapshot=name, grams=200.0, macros=m, product_id=user_id)],
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
