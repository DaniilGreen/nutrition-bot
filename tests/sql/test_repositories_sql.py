"""Проверка запросов, которые фейк не воспроизводит."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.application.entities import Meal, MealItem, Product, User
from app.domain.value_objects import Density, Macros
from app.infrastructure.db.repositories import SqlMealRepo, SqlProductRepo, SqlUserRepo

pytestmark = pytest.mark.asyncio


async def _user(session, tg: int) -> User:
    return await SqlUserRepo(session).get_or_create(
        telegram_id=tg, name="тест", locale="ru", timezone="Europe/Moscow"
    )


def _density(kcal: float) -> Density:
    return Density(kcal_per_100g=kcal, protein_per_100g=kcal * 0.19,
                   fat_per_100g=kcal * 0.02, carb_per_100g=0.0)


async def _product(session, key: str, name: str, kcal: float = 100) -> Product:
    return await SqlProductRepo(session).create(
        Product(canonical_key=key, display_name=name, density=_density(kcal), source="llm")
    )


async def _meal(session, user: User, product: Product, grams: float, days_ago=0) -> Meal:
    m = Macros(kcal=grams, protein=grams * 0.2, fat=grams * 0.02, carb=0)
    return await SqlMealRepo(session).add(
        Meal(
            user_id=user.id,
            eaten_at=datetime.now(UTC) - timedelta(days=days_ago, hours=1),
            input_type="text", raw_input=product.display_name,
            items=[MealItem(name_snapshot=product.display_name, grams=grams,
                            macros=m, product_id=product.id)],
            source_confidence=1.0, totals=m,
        )
    )


async def test_процент_в_поиске_не_становится_маской(session):
    """«100%» обязан искаться буквально, а не как «что угодно» (AC-MA-28).

    Фейк делал подстрочный `in` и этот баг не поймал бы никогда.
    """
    repo = SqlProductRepo(session)
    await _product(session, "sql_milk_pct", "СКЛ молоко 3.2%")
    await _product(session, "sql_bread_x", "СКЛ хлеб")
    only_pct, total_pct = await repo.list_page(offset=0, limit=50, query="%")
    names = [p.display_name for p in only_pct if p.display_name.startswith("СКЛ")]
    assert names == ["СКЛ молоко 3.2%"], names
    assert total_pct >= 1


async def test_подчёркивание_в_поиске_не_джокер(session):
    repo = SqlProductRepo(session)
    await _product(session, "sql_a_b", "СКЛ_подчерк")
    await _product(session, "sql_axb", "СКЛxподчерк")
    rows, _ = await repo.list_page(offset=0, limit=50, query="СКЛ_")
    assert [p.display_name for p in rows] == ["СКЛ_подчерк"]


async def test_счётчик_использований_фильтруется_по_юзеру(session):
    """Один пользователь не должен видеть частоту другого."""
    repo = SqlProductRepo(session)
    a, b = await _user(session, 900001), await _user(session, 900002)
    p = await _product(session, "sql_uses", "СКЛ продукт")
    await _meal(session, a, p, 100)
    await _meal(session, a, p, 120)
    await _meal(session, b, p, 100)
    assert (await repo.usage_counts([p.id], user_id=a.id))[p.id] == 2
    assert (await repo.usage_counts([p.id], user_id=b.id))[p.id] == 1
    assert (await repo.usage_counts([p.id]))[p.id] == 3   # служебный путь: общий вес


async def test_слияние_перепривязывает_историю_не_трогая_снапшот(session):
    """INV-3: съеденное вчера не двигается от правки каталога (AC-MA-26)."""
    repo = SqlProductRepo(session)
    u = await _user(session, 900003)
    keep = await _product(session, "sql_keep", "СКЛ творог 5%", kcal=120)
    drop = await _product(session, "sql_drop", "СКЛ творог 5% дубль", kcal=105)
    meal = await _meal(session, u, drop, 200)
    before = meal.items[0].macros.kcal

    moved = await repo.merge(keep.id, [drop.id])
    assert moved == 1
    assert await repo.get_by_id(drop.id) is None
    after = await SqlMealRepo(session).get(u.id, meal.id)
    assert after.items[0].product_id == keep.id      # связь переехала
    assert after.items[0].macros.kcal == before      # снапшот не тронут


async def test_медиана_граммовки_а_не_среднее(session):
    """percentile_cont: одна ошибка «2000 г» не должна двигать подсказку."""
    u = await _user(session, 900004)
    p = await _product(session, "sql_median", "СКЛ грудка")
    for g in (200, 210, 190, 2000, 205):
        await _meal(session, u, p, g)
    rows = await SqlMealRepo(session).frequent_items(u.id, days=30, limit=5)
    mine = [r for r in rows if r[1] == "СКЛ грудка"]
    assert mine and mine[0][2] == 205, mine


async def test_частое_группируется_по_продукту(session):
    """AC-MA-33: после слияния дублей чипы должны склеиться."""
    repo = SqlProductRepo(session)
    u = await _user(session, 900005)
    a = await _product(session, "sql_g_a", "СКЛ Творог")
    b = await _product(session, "sql_g_b", "СКЛ творог")
    await _meal(session, u, a, 100)
    await _meal(session, u, b, 100)
    before = [r for r in await SqlMealRepo(session).frequent_items(u.id, days=30, limit=10)
              if r[1].startswith("СКЛ")]
    assert len(before) == 2
    await repo.merge(a.id, [b.id])
    after = [r for r in await SqlMealRepo(session).frequent_items(u.id, days=30, limit=10)
             if r[1].startswith("СКЛ")]
    assert len(after) == 1
    assert after[0][3] == 2


async def test_доля_оценок_считается_по_калориям(session):
    u = await _user(session, 900006)
    p = await _product(session, "sql_acc", "СКЛ борщ")
    m = await _meal(session, u, p, 800)
    m.items[0].is_estimate = True
    await session.flush()
    est, total = await SqlMealRepo(session).estimate_share(u.id, days=30)
    assert total >= 800


async def test_вклад_по_белку_на_живой_базе(session):
    """Фейк считает по атрибутам доменного объекта, SQL — по колонкам таблицы.
    Имя колонки я уже перепутал однажды (`protein_g` вместо `protein`), и
    фейковые тесты этого не увидели: там объект, а не схема."""
    u = await _user(session, 900007)
    p = await _product(session, "sql_prot", "СКЛ творог", kcal=120)
    await _meal(session, u, p, 200)
    rows = await SqlMealRepo(session).contributions(u.id, days=30, limit=5, by="protein")
    mine = [r for r in rows if r[1] == "СКЛ творог"]
    assert mine, rows
    assert mine[0][2] > 0                       # белок посчитался
    by_kcal = await SqlMealRepo(session).contributions(u.id, days=30, limit=5)
    kcal_row = [r for r in by_kcal if r[1] == "СКЛ творог"][0]
    assert mine[0][2] != kcal_row[2]            # это разные колонки, а не одна
