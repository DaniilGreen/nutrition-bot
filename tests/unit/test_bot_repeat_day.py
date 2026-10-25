"""Повтор с прошлого дня в сегодня из «🗒 История» бота (FR-42, AC-109…AC-117, D-MA-24).

Держим: 🔁 только на прошлом дне и только в режиме повтора (🗑 и 🔁 на разных экранах); тап пишет
в сегодня тем же `repeat_item` / `repeat_meal`, что аппка, — снапшот КБЖУ и граммы один-в-один
(INV-3), без модели; человек остаётся на листаемом дне, строка под днём — след записи; двойной тап
пишет один раз; устаревшая кнопка не пишет; «⏪ Вернуть» (FR-41) — только живая, своего дня и в
режиме 🗑; повтор старой версии набора не откатывает набор.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message
from aiogram.types import User as TgUser

from app.application.assistant import Assistant
from app.application.entities import Meal, MealItem
from app.application.meal_service import MealService
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.bot import handlers
from app.bot.keyboards import day_nav_keyboard, restore_button
from app.domain.goals import Goal
from app.domain.value_objects import Macros
from tests.fakes import InMemoryMealRepo, InMemoryProductRepo, InMemoryUserRepo, InMemoryWeightRepo

HINT = "запишу в сегодня тем же весом"


class _NoLlm:
    async def parse(self, *a, **kw):
        raise AssertionError("LLM не должен вызываться")

    async def parse_samples(self, *a, **kw):
        raise AssertionError("LLM не должен вызываться")


@pytest.fixture(autouse=True)
def _fresh():
    handlers._taps.clear()
    handlers._trash.clear()
    yield
    handlers._taps.clear()
    handlers._trash.clear()


@pytest.fixture
def rich(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(handlers, "_rich_edit", mock)
    return mock


async def _world():
    users, products, meals = InMemoryUserRepo(), InMemoryProductRepo(), InMemoryMealRepo()
    service = MealService(products, meals)
    assistant = Assistant(
        _NoLlm(), products, service, WeightService(InMemoryWeightRepo()),
        StatsService(meals, users, Goal(2000, 150, 65, 250)),
    )
    user = await users.get_or_create(telegram_id=1, name="T", locale="ru", timezone="UTC")
    return SimpleNamespace(assistant=assistant, user=user, meals=meals, service=service)


def _past(uid: int, *, two: bool = True, days: int = 1, bread: float = 84,
          name: str | None = None) -> Meal:
    """Прошлый приём: хлеб по этикетке (снапшот, которого в каталоге нет) + сыр."""
    k = bread / 84
    items = [
        MealItem(name_snapshot="хлеб", grams=bread, macros=Macros(219.7 * k, 7.1, 2.9, 41.3),
                 product_id=11, is_estimate=False),
        MealItem(name_snapshot="сыр", grams=30, macros=Macros(108.3, 7.5, 8.7, 0)),
    ][: 2 if two else 1]
    kcal = sum(i.macros.kcal for i in items)
    return Meal(
        user_id=uid, eaten_at=datetime.now(UTC) - timedelta(days=days), input_type="text",
        raw_input="хлеб, сыр", items=items, source_confidence=0.9,
        totals=Macros(kcal, 14.6, 11.6, 41.3), template_name=name,
    )


def _d(days: int = 1) -> str:
    return f"d{datetime.now(UTC).date() - timedelta(days=days):%Y%m%d}"


def _callback(data: str, markup=None) -> CallbackQuery:
    bot = AsyncMock()
    msg = Message.model_construct(
        message_id=5, date=None, chat=Chat.model_construct(id=1, type="private"),
        reply_markup=markup,
    ).as_(bot)
    cb = CallbackQuery.model_construct(
        id="1", from_user=TgUser.model_construct(id=1, is_bot=False, first_name="T"),
        chat_instance="ci", data=data, message=msg,
    )
    return cb.as_(bot)


def _answers(cb: CallbackQuery) -> list[str]:
    return [getattr(c.args[0], "text", None) or "" for c in cb.bot.await_args_list]


def _buttons(kb: InlineKeyboardMarkup):
    return [b for row in kb.inline_keyboard for b in row]


def _texts(kb: InlineKeyboardMarkup) -> list[str]:
    return [b.text for b in _buttons(kb)]


def _has_back(kb: InlineKeyboardMarkup) -> bool:
    return any(b.callback_data.startswith("rst:") for b in _buttons(kb))


async def _repeat(w, data: str, session=None) -> CallbackQuery:
    cb = _callback(data)
    await handlers.on_day_repeat(cb, w.user, w.assistant, session or AsyncMock())
    return cb


async def _nav(w, data: str, markup=None) -> CallbackQuery:
    cb = _callback(data, markup)
    await handlers.on_day_nav(cb, w.user, w.assistant)
    return cb


# --- клавиатура ---------------------------------------------------------------


async def test_сегодня_без_повтора_вчера_с_переключателем():
    w = await _world()
    src = await w.meals.add(_past(w.user.id))
    when, day = src.eaten_at.strftime("%H:%M"), src.eaten_at.date()
    today = day_nav_keyboard(0, [src], repeat=True, day=day)  # сегодня режима нет, даже если просят
    assert _texts(today) == ["← пред. день", f"🗑 {when} · хлеб", f"🗑 {when} · сыр"]
    past = day_nav_keyboard(1, [src], day=day)
    assert [len(r) for r in past.inline_keyboard] == [2, 1, 1, 1]  # навигация, режим, 🗑, 🗑
    toggle = past.inline_keyboard[1][0]
    assert toggle.text == "🔁 Повторить в сегодня…" and toggle.callback_data == "day_nav:1:r"
    assert not any(b.callback_data.startswith("day_rep:") for b in _buttons(past))
    assert not any("🔁" in t for t in _texts(day_nav_keyboard(1, [], day=day)))  # пустой день


async def test_режим_повтора_строки_без_корзины_и_в_лимите():
    w = await _world()
    src = await w.meals.add(_past(w.user.id))
    single = await w.meals.add(_past(w.user.id, two=False))
    single.items[0].name_snapshot = "очень длинное имя продукта из этикетки"
    stated = await w.meals.add(_past(w.user.id, two=False))
    stated.items[0].grams = 0  # свои цифры, без веса
    day = src.eaten_at.date()
    back = restore_button("A" * 8, f"d{day:%Y%m%d}", "сыр")
    kb = day_nav_keyboard(2, [src, single, stated], restore=back, repeat=True, day=day)
    when = src.eaten_at.strftime("%H:%M")
    assert _texts(kb) == [
        "← пред. день", "след. день →", "✅ Готово",
        f"🔁 {when} · вся запись · 328 ккал", "↳ 🔁 хлеб · 84 г", "↳ 🔁 сыр · 30 г",
        f"🔁 {when} · очень длинное имя проду… · 84 г",  # режется имя (24 знака), граммы целы
        f"🔁 {when} · хлеб · 220 ккал",
    ]  # и «⏪ Вернуть» нет: она про удаление, а 🗑 в этом режиме нет
    nav = kb.inline_keyboard[0]
    assert [b.callback_data for b in nav] == ["day_nav:3:r", "day_nav:1:r"]  # листание держит режим
    assert kb.inline_keyboard[1][0].callback_data == "day_nav:2"  # «Готово» — назад к 🗑
    assert _buttons(kb)[3].callback_data == f"day_rep:d{day:%Y%m%d}:{src.id}"  # датой, не «назад»
    assert _buttons(kb)[4].callback_data == f"day_rep:d{day:%Y%m%d}:{src.id}:{src.items[0].id}"
    big = Meal(**{**vars(src), "id": 10**12})
    big.items = [MealItem(**{**vars(i), "id": 10**12 + n}) for n, i in enumerate(src.items)]
    data = [b.callback_data for b in _buttons(day_nav_keyboard(30, [big], repeat=True, day=day))]
    assert all(len(d.encode()) <= 64 for d in data)


# --- тап ----------------------------------------------------------------------


async def test_переключатель_перерисовывает_тот_же_день_с_подсказкой(rich):
    w = await _world()
    await w.meals.add(_past(w.user.id))
    await _nav(w, "day_nav:1:r")
    _, html, kb = rich.await_args.args
    assert HINT in html and "хлеб" in html
    assert "✅ Готово" in _texts(kb) and not any(t.startswith("🗑") for t in _texts(kb))
    await _nav(w, "day_nav:1")
    _, html, kb = rich.await_args.args
    assert HINT not in html and "🔁 Повторить в сегодня…" in _texts(kb)


async def test_позиция_пишется_в_сегодня_снапшотом_след_под_днём(rich):
    w = await _world()
    src = await w.meals.add(_past(w.user.id))
    session = AsyncMock()
    cb = await _repeat(w, f"day_rep:{_d()}:{src.id}:{src.items[0].id}", session)
    [_, new] = w.meals._meals
    assert new.eaten_at.date() == datetime.now(UTC).date()  # в СЕГОДНЯ, не во вчера
    [it] = new.items
    old = src.items[0]
    assert (it.name_snapshot, it.grams, it.macros, it.product_id, it.is_estimate) == (
        old.name_snapshot, old.grams, old.macros, old.product_id, old.is_estimate
    )  # INV-3: снапшот один-в-один, граммы те же
    assert [i.name_snapshot for i in src.items] == ["хлеб", "сыр"]  # вчера не изменилось
    session.commit.assert_awaited_once()
    assert _answers(cb) == ["🔁 Записал в сегодня: хлеб · 84 г · 220 ккал"]
    _, html, kb = rich.await_args.args  # тот же день, тот же режим, подсказка стала следом
    assert "сыр" in html  # на экране всё ещё вчерашний день целиком
    assert "🔁 Записал в сегодня: хлеб · 84 г — сегодня 220 из 2000 ккал" in html
    assert HINT not in html and "✅ Готово" in _texts(kb)


async def test_приём_целиком_и_длинные_имена_во_всплывашке(rich):
    w = await _world()
    src = await w.meals.add(_past(w.user.id))
    cb = await _repeat(w, f"day_rep:{_d()}:{src.id}")
    new = w.meals._meals[-1]
    assert new.eaten_at.date() == datetime.now(UTC).date()
    assert [(i.name_snapshot, i.grams, i.macros) for i in new.items] == [
        (i.name_snapshot, i.grams, i.macros) for i in src.items
    ]
    assert _answers(cb) == ["🔁 Записал в сегодня: хлеб, сыр · 328 ккал"]
    for i in src.items:
        i.name_snapshot = "очень длинное название позиции из голосового " * 3
    handlers._taps.clear()  # не двойной тап: это отдельная проверка подписи
    cb = await _repeat(w, f"day_rep:{_d()}:{src.id}")
    [popup] = _answers(cb)
    assert len(popup) <= 200 and "…" in popup


async def test_после_полуночи_пишет_и_перерисовывает_тот_же_день(rich, monkeypatch):
    w = await _world()
    src = await w.meals.add(_past(w.user.id, days=2))
    asked: list[int] = []
    real = w.assistant.day_card

    async def spy(user, days_ago=0):
        asked.append(days_ago)
        return await real(user, days_ago)

    monkeypatch.setattr(w.assistant, "day_card", spy)
    # кнопку нарисовали «вчера», когда этот день был days_ago=1; сейчас он — 2 дня назад
    await _repeat(w, f"day_rep:{_d(2)}:{src.id}:{src.items[0].id}")
    assert asked == [2] and len(w.meals._meals) == 2


async def test_двойной_тап_пишет_один_раз(rich, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(handlers, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    w = await _world()
    src = await w.meals.add(_past(w.user.id))
    data = f"day_rep:{_d()}:{src.id}:{src.items[0].id}"
    await _repeat(w, data)
    clock[0] += 1
    shown = rich.await_count
    second = await _repeat(w, data)
    assert len(w.meals._meals) == 2 and rich.await_count == shown  # след уже на экране
    assert _answers(second)[0].startswith("Уже записал.")
    clock[0] += handlers.TAP_WINDOW_S + 1  # позже окна — вторая порция законна
    await _repeat(w, data)
    assert len(w.meals._meals) == 3


async def test_пока_первый_пишет_второй_слышит_уже_записываю(rich, monkeypatch):
    w = await _world()
    src = await w.meals.add(_past(w.user.id))
    real = w.assistant.repeat_item

    async def slow(*a, **kw):
        await asyncio.sleep(0.01)
        return await real(*a, **kw)

    monkeypatch.setattr(w.assistant, "repeat_item", slow)
    data = f"day_rep:{_d()}:{src.id}:{src.items[0].id}"
    first, second = _callback(data), _callback(data)
    await asyncio.gather(
        handlers.on_day_repeat(first, w.user, w.assistant, AsyncMock()),
        handlers.on_day_repeat(second, w.user, w.assistant, AsyncMock()),
    )
    assert len(w.meals._meals) == 2
    assert _answers(second) == ["Уже записываю…"]


async def test_сбой_фиксации_отвечает_и_не_держит_окно(rich):
    w = await _world()
    src = await w.meals.add(_past(w.user.id))
    data = f"day_rep:{_d()}:{src.id}:{src.items[0].id}"
    broken = AsyncMock()
    broken.commit.side_effect = RuntimeError("db down")
    cb = _callback(data)
    with pytest.raises(RuntimeError):
        await handlers.on_day_repeat(cb, w.user, w.assistant, broken)
    assert _answers(cb) == ["Не записал — попробуй ещё раз."]  # без вечного спиннера
    retry = await _repeat(w, data)
    assert _answers(retry)[0].startswith("🔁 Записал в сегодня")


async def test_устаревшая_и_кривая_кнопка_не_пишут(rich):
    w = await _world()
    src = await w.meals.add(_past(w.user.id))
    gone = src.items[1].id
    await w.assistant.remove_item(w.user, src.id, gone)  # убрали в аппке
    cb = await _repeat(w, f"day_rep:{_d()}:{src.id}:{gone}")
    assert len(w.meals._meals) == 1
    assert _answers(cb) == ["Этой записи уже нет — ничего не записал."]
    _, html, kb = rich.await_args.args
    assert "сыр" not in html and any(t.endswith("хлеб · 84 г") for t in _texts(kb))  # тот же режим
    await w.assistant.delete_meal(w.user, src.id)
    cb = await _repeat(w, f"day_rep:{_d()}:{src.id}")
    assert _answers(cb) == ["Этой записи уже нет — ничего не записал."]
    assert w.meals._meals == []
    for bad in ("day_rep:dxx:1:2", "day_rep:1:1:2", "day_rep:d20261020", "day_rep:d20261020:a",
                "day_rep:d20261020:1:2:3"):
        assert _answers(await _repeat(w, bad)) == ["Кнопка устарела — открой 🗒 История заново."]


# --- «⏪ Вернуть» на экране дня: только живая, своего дня, в режиме 🗑 ---------------


async def test_вернуть_только_живая_своего_дня_и_в_режиме_удаления(rich, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(handlers, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    w = await _world()
    src = await w.meals.add(_past(w.user.id))
    await handlers.on_day_del(
        _callback(f"day_del:1:{src.id}:{src.items[1].id}"), w.user, w.assistant, AsyncMock()
    )
    kb = rich.await_args.args[2]
    back = _buttons(kb)[0]
    assert back.text == "⏪ Вернуть: сыр" and "🔁 Повторить в сегодня…" in _texts(kb)
    await _nav(w, "day_nav:1:r", kb)
    assert not _has_back(rich.await_args.args[2])  # в режиме повтора — нет
    await _nav(w, "day_nav:1", rich.await_args.args[2])
    assert _buttons(rich.await_args.args[2])[0] == back  # назад к 🗑 — снова есть (из снимка)
    await _nav(w, "day_nav:2", kb)  # листнули на чужой день со старой клавиатурой
    assert not _has_back(rich.await_args.args[2])
    await _nav(w, "day_nav:1")
    assert _buttons(rich.await_args.args[2])[0] == back
    clock[0] += handlers.UNDO_TTL_S + 1  # истекла — не показываем
    await _nav(w, "day_nav:1", kb)
    assert not _has_back(rich.await_args.args[2])


async def test_израсходованная_или_вытесненная_вернуть_не_всплывает(rich):
    w = await _world()
    src = await w.meals.add(_past(w.user.id))
    await handlers.on_day_del(
        _callback(f"day_del:1:{src.id}:{src.items[1].id}"), w.user, w.assistant, AsyncMock()
    )
    kb = rich.await_args.args[2]
    await handlers.on_restore(
        _callback(_buttons(kb)[0].callback_data, kb), w.user, w.assistant, AsyncMock()
    )
    await _nav(w, "day_nav:1", kb)  # старая клавиатура с ⏪, а снимок уже вернули
    assert not _has_back(rich.await_args.args[2])
    other = await w.meals.add(_past(w.user.id, days=0))
    await w.assistant.delete_meal(w.user, other.id)
    handlers._remember(w.user, other, None, "хлеб, сыр")  # новое удаление не с экрана дня
    await _nav(w, "day_nav:1", kb)
    assert not _has_back(rich.await_args.args[2])


# --- набор: повтор старой версии его не откатывает (AC-117) ----------------------------------


async def test_повтор_старой_версии_набора_не_откатывает_набор(rich):
    w = await _world()
    mon = await w.meals.add(_past(w.user.id, days=5, bread=200, name="бутер"))
    await w.meals.add(_past(w.user.id, days=3, bread=150, name="бутер"))
    await _repeat(w, f"day_rep:{_d(5)}:{mon.id}")  # понедельник из истории — в сегодня
    copy_ = w.meals._meals[-1]
    assert copy_.items[0].grams == 200 and copy_.template_name is None  # повтор — да, набор — нет
    [current] = [m for m in await w.assistant.sets(w.user) if m.template_name == "бутер"]
    assert current.items[0].grams == 150  # «⚡ Частое» по-прежнему 150 г
    again = await w.assistant.repeat_meal(w.user, current.id)  # повтор самого набора — с именем
    assert again is not None and again.meal.template_name == "бутер"
    assert again.meal.items[0].grams == 150


async def test_повтор_набора_вдвое_не_делает_набором_двойную_порцию():
    """AC-119: «бутер ×2» пишет 300 г, но набор остаётся 150 г — иначе дальше всё вдвое."""
    w = await _world()
    await w.meals.add(_past(w.user.id, days=3, bread=150, name="бутер"))
    double = await w.service.repeat_template(w.user, "бутер", times=2)
    assert double is not None and double.items[0].grams == 300 and double.template_name is None
    once = await w.service.repeat_template(w.user, "бутер")
    assert once is not None and once.items[0].grams == 150 and once.template_name == "бутер"
    [current] = [m for m in await w.assistant.sets(w.user) if m.template_name == "бутер"]
    assert current.items[0].grams == 150
