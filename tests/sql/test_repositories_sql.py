"""Проверка запросов, которые фейк не воспроизводит."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update

from app.application.entities import Meal, MealItem, Product, User
from app.domain.body_scan import BodyScan
from app.domain.goals import Goal
from app.domain.value_objects import Density, Macros
from app.infrastructure.db.models import MealORM
from app.infrastructure.db.repositories import (
    SqlBodyScanRepo,
    SqlMealRepo,
    SqlProductRepo,
    SqlUserRepo,
)

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


async def test_замер_состава_upsert_по_времени_и_удаление(session):
    """MA-INV-51: второй снимок того же бланка обновляет, а не дублирует."""
    u = await _user(session, 900010)
    repo = SqlBodyScanRepo(session)
    at = datetime(2026, 5, 3, 8, 44, tzinfo=UTC)
    scan = BodyScan(measured_at=at, weight_kg=91.8, body_fat_kg=19.8, fat_free_kg=72.0,
                    user_id=u.id, sex="male", age=30, device="InBody270")
    first, updated = await repo.upsert(scan, ["body_fat_kg"])
    assert not updated and first.id
    again, updated = await repo.upsert(replace(scan, weight_kg=91.9))
    assert updated and again.id == first.id and again.weight_kg == 91.9
    assert [s.id for s in await repo.list_for(u.id)] == [first.id]
    assert (await repo.get_at(u.id, at)).device == "InBody270"
    assert (await repo.list_for(u.id))[0].id == first.id
    assert await repo.delete(u.id, first.id) and not await repo.delete(u.id, first.id)


async def test_сегменты_бланка_ходят_через_jsonb(session):
    """§6.24: десять чисел лежат одной колонкой, а в домене они плоские."""
    u = await _user(session, 900020)
    repo = SqlBodyScanRepo(session)
    at = datetime(2026, 9, 22, 8, 26, tzinfo=UTC)
    scan = BodyScan(
        measured_at=at, weight_kg=91.6, body_fat_kg=20.9, fat_free_kg=70.7,
        user_id=u.id, smi=9.2, inbody_score=82,
        lean_arm_l_kg=4.47, lean_arm_r_kg=4.29, lean_trunk_kg=32.6,
        lean_leg_l_kg=10.62, lean_leg_r_kg=10.57,
        fat_arm_l_kg=1.1, fat_arm_r_kg=1.1, fat_trunk_kg=12.0,
        fat_leg_l_kg=2.7, fat_leg_r_kg=2.7,
    )
    saved, _ = await repo.upsert(scan)
    back = await repo.get_at(u.id, at)
    assert (back.smi, back.inbody_score) == (9.2, 82)
    assert (back.lean_arm_l_kg, back.fat_trunk_kg) == (4.47, 12.0)

    # Повторный бланк заменяет блок целиком, а не сливает ключи.
    thin = replace(scan, lean_arm_l_kg=None, lean_arm_r_kg=None, lean_trunk_kg=None,
                   lean_leg_l_kg=None, lean_leg_r_kg=None, fat_arm_l_kg=None,
                   fat_arm_r_kg=None, fat_trunk_kg=None, fat_leg_l_kg=None,
                   fat_leg_r_kg=None)
    await repo.upsert(thin)
    back = await repo.get_at(u.id, at)
    assert back.lean_arm_l_kg is None and back.fat_trunk_kg is None
    assert back.smi == 9.2                      # скаляры живут отдельно от блока

    # Замер до миграции 0012: пустая колонка читается без падения.
    old_scan, _ = await repo.upsert(
        BodyScan(measured_at=datetime(2026, 5, 3, 8, 44, tzinfo=UTC), weight_kg=91.8,
                 body_fat_kg=19.8, fat_free_kg=72.0, user_id=u.id)
    )
    assert old_scan.lean_trunk_kg is None and old_scan.smi is None
    assert saved.id != old_scan.id


async def test_отметка_напоминания_о_замере(session):
    """MA-INV-63: дедуп живёт в БД, а не в памяти процесса."""
    u = await _user(session, 900021)
    users = SqlUserRepo(session)
    at = datetime(2026, 9, 22, 13, 0, tzinfo=UTC)
    await users.mark_scan_nudged(u.id, at)
    again = await users.get_or_create(
        telegram_id=u.telegram_id, name=u.name, locale="ru", timezone=u.timezone
    )
    assert again.scan_nudged_at == at


async def test_история_целей_пишется_при_смене(session):
    u = await _user(session, 900011)
    users = SqlUserRepo(session)
    await users.set_goal(u.id, Goal(2176, 194, 72, 188, "cut"))
    await users.set_goal(u.id, Goal(2250, 173, 75, 230, "cut"), "scan")
    hist = await users.goal_history(u.id)
    assert [k for _, k in hist] == [2176, 2250]
    assert (await users.get_goal(u.id)).daily_kcal == 2250


async def test_часовая_сводка_со_сдвигом_суток(session):
    """§6.5: при начале суток в 4:00 перекус в 00:30 — это час 20 вчерашнего дня."""
    u = await _user(session, 900012)
    p = await _product(session, "sql_night", "СКЛ ночной сыр")
    night = datetime.now(UTC).replace(hour=17, minute=30, second=0, microsecond=0)  # 00:30 по +7
    meal = await _meal(session, u, p, 100)
    await session.execute(update(MealORM).where(MealORM.id == meal.id).values(eaten_at=night))
    rows = await SqlMealRepo(session).hourly_kcal(u.id, days=2, tz="Etc/GMT-7", day_start=4)
    local = night.astimezone(ZoneInfo("Etc/GMT-7")) - timedelta(hours=4)
    assert (local.date().isoformat(), 20) in {(d, h) for d, h, _ in rows}, rows


async def test_потолок_трат_хранится_в_настройках_инсталляции(session):
    """MA-INV-65: пусто — значение из окружения; задан — он; повторная запись обновляет."""
    from app.infrastructure.db.repositories import SqlSpendRepo

    spend = SqlSpendRepo(session)
    assert await spend.month_limit(15.0) == 15.0
    await spend.set_month_limit(20.0)
    assert await spend.month_limit(15.0) == 20.0
    await spend.set_month_limit(25.0)
    assert await spend.month_limit(15.0) == 25.0


async def test_недавние_и_частые_только_свои_и_последний_раз(session):
    """§6.25: порядок по последнему разу или частоте, поиск по имени, чужие приёмы не видны."""
    me, other = await _user(session, 9301), await _user(session, 9302)
    bread = await _product(session, "rx_bread", "Хлеб 100% зерно")
    yog = await _product(session, "rx_yog", "йогурт_этикетка")
    beer = await _product(session, "rx_beer", "пиво соседа")
    await _meal(session, me, yog, 60, days_ago=3)
    await _meal(session, me, yog, 70, days_ago=2)
    await _meal(session, me, bread, 84, days_ago=1)
    await _meal(session, other, beer, 500, days_ago=0)
    repo = SqlMealRepo(session)

    recent, total = await repo.product_usage(me.id, by="recent", query=None, offset=0, limit=10)
    assert [pid for pid, _ in recent] == [bread.id, yog.id] and total == 2
    frequent, _ = await repo.product_usage(me.id, by="frequent", query=None, offset=0, limit=10)
    assert frequent == [(yog.id, 2), (bread.id, 1)]
    # «%» и «_» в поиске — буквы, а не маски
    found, n = await repo.product_usage(me.id, by="recent", query="100%", offset=0, limit=10)
    assert [pid for pid, _ in found] == [bread.id] and n == 1

    last = await repo.last_use(me.id, [yog.id, bread.id, beer.id])
    assert last[yog.id][1] == 70 and last[bread.id][1] == 84
    assert beer.id not in last  # пиво — чужое


async def test_свои_цифры_по_имени_последний_раз_только_свои(session):
    me, other = await _user(session, 9401), await _user(session, 9402)
    repo = SqlMealRepo(session)

    async def stated(user, name, kcal, days_ago):
        m = Macros(kcal, 10, 5, 20)
        return await repo.add(Meal(
            user_id=user.id, eaten_at=datetime.now(UTC) - timedelta(days=days_ago, hours=1),
            input_type="menu", raw_input=name,
            items=[MealItem(name_snapshot=name, grams=0, macros=m, product_id=None)],
            source_confidence=1.0, totals=m))

    await stated(me, "Chicken Caesar Salad", 410, 2)
    last = await stated(me, "chicken caesar salad", 400, 0)
    await stated(other, "Чужой салат 100%", 300, 0)
    rows = await repo.stated_usage(me.id, query=None)
    assert len(rows) == 1 and rows[0].uses == 2
    assert rows[0].item_id == last.items[0].id and rows[0].macros.kcal == 400
    assert await repo.stated_usage(me.id, query="100%") == []


# ---------- FR-39.7: состав рецепта на живом Postgres ----------

async def _recipe_product(session, key: str, name: str) -> Product:
    return await SqlProductRepo(session).create(
        Product(canonical_key=key, display_name=name, density=_density(180), source="recipe")
    )


async def test_recipe_roundtrip_and_replace(session):
    from app.application.entities import Recipe, RecipeItem

    repo = SqlProductRepo(session)
    a = await _product(session, "sql_r_a", "курица")
    b = await _product(session, "sql_r_b", "майонез", kcal=680)
    rec = await _recipe_product(session, "sql_r_rec", "курица с мазиком")
    await repo.set_recipe(rec.id, Recipe(items=[
        RecipeItem("курица", 1100, a.density, a.id, True),
        RecipeItem("майонез", 200, b.density, b.id, False, alcohol=False),
    ], yield_g=1250))

    got = await repo.get_recipe(rec.id)
    assert [(i.name, i.grams, i.product_id) for i in got.items] == [
        ("курица", 1100, a.id), ("майонез", 200, b.id),
    ]
    assert got.yield_g == 1250

    await repo.set_recipe(rec.id, Recipe(items=[RecipeItem("курица", 900, a.density, a.id)]))
    again = await repo.get_recipe(rec.id)
    assert len(again.items) == 1 and again.items[0].grams == 900 and again.yield_g is None


async def test_merge_repoints_recipe_ingredients(session):
    """Слили дубль ингредиента — состав ссылается на выжившего, а не на удалённый id."""
    from app.application.entities import Recipe, RecipeItem

    repo = SqlProductRepo(session)
    loser = await _product(session, "sql_m_loser", "майонез домашний", kcal=680)
    winner = await _product(session, "sql_m_winner", "майонез", kcal=620)
    rec = await _recipe_product(session, "sql_m_rec", "салат")
    await repo.set_recipe(rec.id, Recipe(items=[RecipeItem("майонез домашний", 100,
                                                           loser.density, loser.id)]))

    await repo.merge(winner.id, [loser.id])
    session.expire_all()

    assert (await repo.get_recipe(rec.id)).items[0].product_id == winner.id


async def test_merge_into_the_recipe_itself_does_not_self_reference(session):
    """Ингредиент слили в сам рецепт — ссылка обнуляется (ON DELETE SET NULL), а не
    указывает на рецепт: иначе каждое сохранение раздувало бы плотность."""
    from app.application.entities import Recipe, RecipeItem

    repo = SqlProductRepo(session)
    ing = await _product(session, "sql_s_ing", "курица")
    rec = await _recipe_product(session, "sql_s_rec", "курица")
    await repo.set_recipe(rec.id, Recipe(items=[RecipeItem("курица", 500, ing.density, ing.id)]))

    await repo.merge(rec.id, [ing.id])
    session.expire_all()

    item = (await repo.get_recipe(rec.id)).items[0]
    assert item.product_id is None
    assert item.density.kcal_per_100g == ing.density.kcal_per_100g  # снимок цел


async def test_замена_позиции_в_том_же_приёме(session):
    """§6.27 «поправить словами»: старая строка уходит, новая встаёт, итоги пересчитаны."""
    me = await _user(session, 9501)
    p = await _product(session, "rx_buckwheat", "гречка", 110)
    meal = await _meal(session, me, p, 250)
    repo = SqlMealRepo(session)
    new = MealItem(name_snapshot="булгур отварной", grams=250,
                   macros=Macros(207.5, 7.8, 0.5, 46.5), product_id=None)
    out = await repo.replace_item(me.id, meal.id, meal.items[0].id, [new])
    assert [i.name_snapshot for i in out.items] == ["булгур отварной"]
    assert out.totals.kcal == 207.5 and out.id == meal.id
    again = await repo.get(me.id, meal.id)
    assert [i.name_snapshot for i in again.items] == ["булгур отварной"]
    assert await repo.replace_item(me.id + 1, meal.id, again.items[0].id, [new]) is None


async def _loose(session, user: User, name: str, grams: float, days_ago=0) -> Meal:
    """Позиция без продукта — как запись, которую LLM не запиннил."""
    m = Macros(kcal=grams, protein=0, fat=0, carb=0)
    return await SqlMealRepo(session).add(
        Meal(
            user_id=user.id, eaten_at=datetime.now(UTC) - timedelta(days=days_ago, hours=1),
            input_type="text", raw_input=name,
            items=[MealItem(name_snapshot=name, grams=grams, macros=m)],
            source_confidence=1.0, totals=m,
        )
    )


async def test_незакреплённое_группируется_по_имени_без_регистра(session):
    """FR-40: coalesce(product_id, -1) склеивал ВСЕ записи без продукта в одну строку."""
    u = await _user(session, 900040)
    await _loose(session, u, "СКЛ Суп дня", 300)
    await _loose(session, u, "скл суп дня", 300)
    await _loose(session, u, "СКЛ салат", 150)
    rows = [r for r in await SqlMealRepo(session).frequent_items(u.id, days=30, limit=10)
            if r[1].casefold().startswith("скл")]
    assert sorted((r[1].casefold(), r[3]) for r in rows) == [("скл салат", 1), ("скл суп дня", 2)]
    assert all(r[0] is None for r in rows)


async def test_ничья_по_частоте_свежее_выше(session):
    """FR-40: при равных счётчиках порядок детерминирован — свежее съеденное первым."""
    u = await _user(session, 900041)
    old = await _product(session, "sql_tie_old", "СКЛ старое")
    new = await _product(session, "sql_tie_new", "СКЛ новое")
    await _meal(session, u, old, 100, days_ago=5)
    await _meal(session, u, new, 100, days_ago=1)
    rows = [r for r in await SqlMealRepo(session).frequent_items(u.id, days=30, limit=10)
            if r[1].startswith("СКЛ")]
    assert [r[0] for r in rows] == [new.id, old.id]
