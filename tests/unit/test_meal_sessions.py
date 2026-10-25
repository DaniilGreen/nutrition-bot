"""«Приём пищи» (FR-47): группировка при чтении и «Сохранить приём пищи набором».

Часы зафиксированы: граница дня — главный риск правила, и тест, зависящий от того, в котором
часу его запустили, проверял бы её через раз.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.application.assistant import Assistant
from app.application.entities import Meal, MealItem, User
from app.application.meal_service import MealService, SessionRejected, meal_fingerprint
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.domain.goals import Goal
from app.domain.sessions import GAP, group
from app.domain.value_objects import Macros, sum_macros
from tests.fakes import InMemoryMealRepo, InMemoryProductRepo, InMemoryUserRepo, InMemoryWeightRepo

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 25, 15, 0, tzinfo=MSK).astimezone(UTC)


def _user(uid: int = 1, **prefs) -> User:
    return User(telegram_id=uid, name="т", locale="ru", timezone="Europe/Moscow", id=uid,
                ui_prefs=dict(prefs))


def _at(hh: int, mm: int, *, days: int = 0) -> datetime:
    return (datetime(2026, 10, 25, hh, mm, tzinfo=MSK) + timedelta(days=days)).astimezone(UTC)


def _meal(uid: int, when: datetime, *items: tuple[str, float, float], raw: str = "x",
          kind: str = "text", template: str | None = None) -> Meal:
    ms = [MealItem(name_snapshot=n, grams=g, macros=Macros(k, k / 20, k / 40, k / 10))
          for n, g, k in items]
    return Meal(user_id=uid, eaten_at=when, input_type=kind, raw_input=raw, items=ms,
                source_confidence=0.9, totals=sum_macros(i.macros for i in ms),
                template_name=template)


@pytest.fixture
def repo() -> InMemoryMealRepo:
    return InMemoryMealRepo()


@pytest.fixture
def svc(repo) -> MealService:
    return MealService(InMemoryProductRepo(), repo, clock=lambda: NOW)


def _assistant(repo: InMemoryMealRepo, svc: MealService) -> Assistant:
    stats = StatsService(repo, InMemoryUserRepo(), Goal(2000, 120, 70, 200), clock=lambda: NOW)
    return Assistant(None, InMemoryProductRepo(), svc, WeightService(InMemoryWeightRepo()),
                     stats)


async def _burger(repo: InMemoryMealRepo, uid: int = 1) -> list[Meal]:
    """Бургер пятью записями по ~10 минут (сыр, сыр, лаваш, помидор, курица)."""
    parts = [("сыр", 20, 70), ("сыр", 20, 70), ("лаваш", 50, 137), ("помидор", 40, 8),
             ("курица", 120, 200)]
    return [await repo.add(_meal(uid, _at(12, 10 * k), (n, g, kc), raw=f"{n} {g}"))
            for k, (n, g, kc) in enumerate(parts)]


# --- домен ---------------------------------------------------------------------------------

def test_разрыв_считается_от_предыдущей_записи():
    """AC-176: 12:00, 12:20, 12:45 — один приём пищи, хотя от первой до третьей 45 минут."""
    t = [_at(12, 0), _at(12, 20), _at(12, 45), _at(13, 20)]
    assert group(t) == [[0, 1, 2], [3]]


def test_ровно_тридцать_минут_ещё_тот_же():
    """AC-177."""
    assert GAP == timedelta(minutes=30)
    assert group([_at(12, 0), _at(12, 30)]) == [[0, 1]]
    assert group([_at(12, 0), _at(12, 31)]) == [[0], [1]]


def test_порядок_входа_не_важен():
    assert group([_at(13, 20), _at(12, 0), _at(12, 10)]) == [[1, 2], [0]]


# --- чтение -------------------------------------------------------------------------------

async def test_день_отдаёт_приёмы_пищи_не_трогая_итогов(repo, svc):
    """AC-180: группы — сумма записей, итоги дня те же."""
    u = _user()
    burger = await _burger(repo)
    tea = await repo.add(_meal(1, _at(14, 0), ("чай", 200, 2)))
    a = _assistant(repo, svc)
    remaining, view = await a.day_card(u)

    assert [s.meal_ids for s in view.sessions] == [[m.id for m in burger], [tea.id]]
    assert view.sessions[0].totals == sum_macros(m.totals for m in burger)
    assert remaining.consumed == sum_macros(m.totals for m in [*burger, tea])
    assert view.meals == sorted(view.meals, key=lambda m: m.eaten_at)  # лента та же


async def test_приём_пищи_записи_для_карточки(repo, svc):
    u = _user()
    burger = await _burger(repo)
    s = await _assistant(repo, svc).session_of(u, burger[2].id)
    assert s is not None and s.meal_ids == [m.id for m in burger]
    assert s.start == burger[0].eaten_at and s.end == burger[-1].eaten_at
    assert await svc.session_of(u, 999) is None
    assert await svc.session_of(_user(2), burger[0].id) is None  # чужая запись — не видна


async def test_граница_дня_режет_приём_пищи(repo, svc):
    """AC-178: 23:50 и 00:10 — при полуночи разные дни, при начале суток в 04:00 — один."""
    late = await repo.add(_meal(1, _at(23, 50, days=-1), ("кефир", 200, 100)))
    night = await repo.add(_meal(1, _at(0, 10), ("печенье", 30, 130)))

    assert (await svc.session_of(_user(), night.id)).meal_ids == [night.id]
    assert (await svc.session_of(_user(), late.id)).meal_ids == [late.id]
    at4 = await svc.session_of(_user(day_start=4), night.id)
    assert at4.meal_ids == [late.id, night.id]


async def test_бэкдейт_не_липнет_к_сегодняшним(repo, svc):
    """AC-179: «вчера ужин» записан в 12:05 рядом с обедом 12:00 — но живёт во вчера."""
    lunch = await repo.add(_meal(1, _at(12, 0), ("суп", 300, 150)))
    dinner = await repo.add(_meal(1, _at(12, 5, days=-1), ("плов", 300, 500)))
    assert (await svc.session_of(_user(), lunch.id)).meal_ids == [lunch.id]
    assert (await svc.session_of(_user(), dinner.id)).meal_ids == [dinner.id]


# --- «Сохранить приём пищи набором» -------------------------------------------------------

async def test_слияние_в_новую_запись(repo, svc):
    """AC-181: одна НОВАЯ запись с пятью позициями (id/снапшоты позиций те же), все пять прежних
    удалены, время и created_at — самой ранней, итоги дня до знака прежние."""
    u = _user()
    burger = await _burger(repo)
    burger[0].created_at = _at(12, 1)
    old_ids = [m.id for m in burger]
    items = [(i.id, i.grams, i.macros) for m in burger for i in m.items]
    day_before = sum(m.totals.kcal for m in burger)

    meal = await svc.save_session_as_set(u, old_ids[::-1], " бургер ")

    assert meal.id not in old_ids
    assert (meal.eaten_at, meal.created_at) == (burger[0].eaten_at, _at(12, 1))
    assert meal.template_name == "бургер" and meal.template_at == NOW
    assert [(i.id, i.grams, i.macros) for i in meal.items] == items  # INV-3
    assert meal.totals.kcal == day_before
    assert meal.raw_input == "сыр 20, сыр 20, лаваш 50, помидор 40, курица 120"
    left = [m.id for m in await repo.list_between(1, _at(0, 0), _at(23, 59))]
    assert left == [meal.id]
    assert [s.template_name for s in await svc.sets(u)] == ["бургер"]


async def test_старые_кнопки_прежних_записей_отказывают(repo, svc):
    """AC-197: «🔁», «↩️ Отменить всё», «📌», «🗑 позиция», «✏️ Вес» по прежнему id — ничего, а не
    весь набор (+485 ккал на повтор). Самая ранняя — тоже: её id больше не существует."""
    u = _user()
    burger = await _burger(repo)
    first, item = burger[0], burger[0].items[0]
    await svc.save_session_as_set(u, [m.id for m in burger], "бургер")
    before = len(repo._meals)

    assert await svc.repeat(u, first.id) is None
    assert await svc.repeat_item(u, first.id, item.id) is None
    assert await svc.delete(u, first.id) is None
    assert await svc.save_template(u, first.id, "сыр") is None
    assert await svc.remove_item(u, first.id, item.id) is None
    assert await svc.adjust_item(u, first.id, item.id, 40) is None
    assert len(repo._meals) == before and repo._meals[0].template_name == "бургер"


async def test_итог_набора_сумма_итогов_записей_без_округления(repo, svc):
    """AC-198: позиции по 1,75 г жира → итог записи 1,8; пять записей → 9,0, а не 8,8."""
    for k in range(5):
        it = MealItem(name_snapshot=f"п{k}", grams=10, macros=Macros(10.05, 0.25, 1.75, 0.45))
        await repo.add(Meal(user_id=1, eaten_at=_at(12, k), input_type="text", raw_input="x",
                            items=[it], source_confidence=1, totals=sum_macros([it.macros])))
    day = sum_macros(m.totals for m in repo._meals)
    meal = await svc.save_session_as_set(_user(), [m.id for m in repo._meals], "п")
    assert sum_macros([meal.totals]) == day
    assert meal.totals.fat == pytest.approx(9.0) and round(5 * 1.75, 1) == 8.8


async def test_тип_ввода_самой_ранней_записи(repo, svc):
    """AC-200: фото первым — у набора «photo»."""
    a = await repo.add(_meal(1, _at(9, 0), ("паста", 300, 450), kind="photo"))
    b = await repo.add(_meal(1, _at(9, 5), ("сок", 200, 90)))
    meal = await svc.save_session_as_set(_user(), [a.id, b.id], "обед")
    assert meal.input_type == "photo"


async def test_равное_время_по_id_а_не_по_вставке(repo, svc):
    """AC-201: id не в порядке вставки — группа и «самая ранняя» по (eaten_at, id)."""
    a = await repo.add(_meal(1, _at(9, 0), ("паста", 300, 450), kind="photo"))
    b = await repo.add(_meal(1, _at(9, 0), ("сок", 200, 90)))
    a.id, b.id = 101, 100
    s = await svc.session_of(_user(), 101)
    assert s.meal_ids == [100, 101]
    meal = await svc.save_session_as_set(_user(), [101, 100], "обед")
    assert meal.input_type == "text" and [i.name_snapshot for i in meal.items] == ["сок", "паста"]


async def test_вчерашний_состав_становится_набором(repo, svc):
    """AC-199: набор «бургер» есть сегодня; вчерашний приём пищи сохранили «бургером» — набор он,
    и «⏪ Вернуть» сегодняшней старой версии набор не откатывает."""
    u = _user()
    today = await repo.add(_meal(1, _at(13, 0), ("котлета", 100, 250), ("булка", 60, 160),
                                 template="бургер"))
    y1 = await repo.add(_meal(1, _at(12, 0, days=-1), ("котлета", 100, 250)))
    y2 = await repo.add(_meal(1, _at(12, 5, days=-1), ("булка", 60, 160), ("сыр", 20, 70)))
    merged = await svc.save_session_as_set(u, [y1.id, y2.id], "Бургер")
    assert (await repo.latest_by_template(1, "бургер")).id == merged.id

    snap = await svc.delete(u, today.id)
    await svc.restore(snap)
    assert (await repo.latest_by_template(1, "бургер")).id == merged.id


async def test_отказ_несёт_текущий_приём_пищи(repo, svc):
    """MA-INV-79: на `not_a_session` — приём пищи первой живой из присланных."""
    burger = await _burger(repo)
    with pytest.raises(SessionRejected) as e:
        await svc.save_session_as_set(_user(), [999, burger[1].id], "бургер")
    assert e.value.session.meal_ids == [m.id for m in burger]


async def test_потолок_два_часа_от_начала(repo, svc):
    """AC-196: паузы по 25 минут, но 20:05 — уже новый приём пищи."""
    ids = [(await repo.add(_meal(1, _at(h, m), ("орехи", 10, 60)))).id
           for h, m in [(18, 0), (18, 25), (18, 50), (19, 15), (19, 40), (20, 5)]]
    assert (await svc.session_of(_user(), ids[0])).meal_ids == ids[:5]
    assert (await svc.session_of(_user(), ids[5])).meal_ids == ids[5:]


async def test_вернуть_сверяет_отпечаток_под_блокировкой(repo, svc):
    """MA-INV-80 в use-case: совпал — вернул; не совпал — `SnapshotStale`, ничего не записано."""
    from copy import deepcopy

    from app.application.meal_service import SnapshotStale

    u = _user()
    a = _assistant(repo, svc)
    meal = await repo.add(_meal(1, _at(12, 0), ("гречка", 200, 220), ("котлета", 100, 250)))
    snap = deepcopy(meal)
    after = await svc.remove_item(u, meal.id, meal.items[1].id)
    fp = meal_fingerprint(after)
    await svc.adjust_item(u, meal.id, meal.items[0].id, 100)  # правка после удаления
    with pytest.raises(SnapshotStale):
        await a.restore_meal(u, snap, expect=fp)
    assert len(repo._meals) == 1 and len(repo._meals[0].items) == 1

    ok = await a.restore_meal(u, snap, expect=meal_fingerprint(repo._meals[0]))
    assert ok is not None and len(ok.meal.items) == 2 and len(repo._meals) == 1


@pytest.mark.parametrize("pick", ["missing", "extra", "foreign", "empty"])
async def test_id_не_совпали_с_приёмом_пищи(repo, svc, pick):
    """AC-182: лишняя, недостающая, чужая запись — отказ, ничего не тронуто."""
    burger = await _burger(repo)
    later = await repo.add(_meal(1, _at(18, 0), ("яблоко", 150, 80)))
    foreign = await repo.add(_meal(2, _at(12, 5), ("чужое", 100, 100)))
    ids = [m.id for m in burger]
    ids = {"missing": ids[:-1], "extra": [*ids, later.id], "foreign": [*ids, foreign.id],
           "empty": []}[pick]
    with pytest.raises(SessionRejected) as e:
        await svc.save_session_as_set(_user(), ids, "бургер")
    assert e.value.code == "not_a_session"
    assert len(await repo.list_between(1, _at(0, 0), _at(23, 59))) == 6
    assert all(m.template_name is None for m in burger)


async def test_повтор_после_слияния_отказ(repo, svc):
    """AC-183: второй запрос (двойной тап без ключа) не сливает дважды."""
    ids = [m.id for m in await _burger(repo)]
    await svc.save_session_as_set(_user(), ids, "бургер")
    with pytest.raises(SessionRejected) as e:
        await svc.save_session_as_set(_user(), ids, "бургер")
    assert e.value.code == "not_a_session"


async def test_позиция_без_веса_не_даёт_набора(repo, svc):
    """AC-184."""
    a = await repo.add(_meal(1, _at(12, 0), ("бургер", 250, 600)))
    b = await repo.add(_meal(1, _at(12, 5), ("соус", 0, 90)))
    with pytest.raises(SessionRejected) as e:
        await svc.save_session_as_set(_user(), [a.id, b.id], "бургер")
    assert e.value.code == "bad_set"
    assert await repo.get(1, b.id) is not None


async def test_одна_запись_из_одной_позиции_не_набор(repo, svc):
    a = await repo.add(_meal(1, _at(12, 0), ("бургер", 250, 600)))
    with pytest.raises(SessionRejected) as e:
        await svc.save_session_as_set(_user(), [a.id], "бургер")
    assert e.value.code == "bad_set"


async def test_одна_запись_обычный_набор_без_слияния(repo, svc):
    """AC-185: приём пищи из одной записи — прежний «Сохранить набор»."""
    a = await repo.add(_meal(1, _at(12, 0), ("курица", 120, 220), ("лаваш", 50, 137)))
    meal = await svc.save_session_as_set(_user(), [a.id], "курица в лаваше")
    assert meal.id == a.id and meal.template_name == "курица в лаваше"
    assert len(meal.items) == 2


async def test_новое_имя_побеждает_прежний_набор(repo, svc):
    """AC-186."""
    a = await repo.add(_meal(1, _at(12, 0), ("котлета", 100, 250), ("булка", 60, 160),
                             template="бургер"))
    b = await repo.add(_meal(1, _at(12, 10), ("соус", 20, 90)))
    meal = await svc.save_session_as_set(_user(), [a.id, b.id], "бургер с соусом")
    assert meal.template_name == "бургер с соусом" and len(meal.items) == 3


async def test_пустое_имя_отказ(repo, svc):
    ids = [m.id for m in await _burger(repo)]
    with pytest.raises(SessionRejected) as e:
        await svc.save_session_as_set(_user(), ids, "   ")
    assert e.value.code == "bad_name"


async def test_отпечатки_слитых_записей_меняются(repo, svc):
    """AC-187 (основа): висящий снимок «Вернуть» любой из записей после слияния не совпадает."""
    burger = await _burger(repo)
    before = {m.id: meal_fingerprint(m) for m in burger}
    await svc.save_session_as_set(_user(), [m.id for m in burger], "бургер")
    for mid, fp in before.items():
        assert meal_fingerprint(await repo.get(1, mid)) != fp
    assert meal_fingerprint(None) == "gone"
