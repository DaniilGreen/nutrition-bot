"""«⚡ Частое» в боте (FR-40, AC-96…AC-100).

Держим четыре вещи: клавиатура (наборы первыми, ≤ 10, callback ≤ 64 байт), тап пишет тем же
use-case, что и аппка, с граммами с кнопки и без модели, пустое состояние объясняет, двойной тап
не пишет второй раз молча.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TgUser

from app.application.assistant import Assistant
from app.application.entities import Meal, MealItem, Product, User
from app.application.meal_service import MealService
from app.application.results import MealLogged
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.bot import handlers
from app.bot.keyboards import (
    BTN_FREQUENT,
    FREQ_PRODUCTS_MAX,
    FREQ_SETS_MAX,
    MENU_BUTTONS,
    frequent_keyboard,
    main_menu_keyboard,
    set_key,
)
from app.domain.goals import Goal
from app.domain.value_objects import Density, Macros
from tests.fakes import InMemoryMealRepo, InMemoryProductRepo, InMemoryUserRepo, InMemoryWeightRepo

SET = "курица в лаваше"


class _NoLlm:
    """Любой поход в модель — провал теста: тап по известному обязан обходиться без неё."""

    async def parse(self, *a, **kw):
        raise AssertionError("LLM не должен вызываться")

    async def parse_samples(self, *a, **kw):
        raise AssertionError("LLM не должен вызываться")


@pytest.fixture(autouse=True)
def _fresh_taps():
    handlers._taps.clear()
    yield
    handlers._taps.clear()


async def _world():
    users, products, meals = InMemoryUserRepo(), InMemoryProductRepo(), InMemoryMealRepo()
    assistant = Assistant(
        _NoLlm(), products, MealService(products, meals), WeightService(InMemoryWeightRepo()),
        StatsService(meals, users, Goal(2000, 150, 65, 250)),
    )
    user = await users.get_or_create(telegram_id=1, name="T", locale="ru", timezone="UTC")
    return assistant, user, products, meals


async def _tvorog(products: InMemoryProductRepo) -> Product:
    return await products.create(Product(
        canonical_key="tvorog_5", display_name="творог 5%", density=Density(121, 17, 5, 1.8),
        source="curated", is_estimate=False,
    ))


def _wrap(uid: int, name: str = SET, *, chicken: float = 120, hours_ago: float = 2) -> Meal:
    k = chicken / 120
    items = [
        MealItem(name_snapshot="курица с мазиком", grams=chicken,
                 macros=Macros(220 * k, 22 * k, 13 * k, 2 * k)),
        MealItem(name_snapshot="лаваш", grams=50, macros=Macros(137, 4.5, 0.6, 28)),
    ]
    return Meal(
        user_id=uid, eaten_at=datetime.now(UTC) - timedelta(hours=hours_ago), input_type="text",
        raw_input=f"курица с мазиком {chicken:g}, лаваш 50", items=items, source_confidence=0.9,
        totals=Macros(220 * k + 137, 22 * k + 4.5, 13 * k + 0.6, 2 * k + 28), template_name=name,
    )


def _callback(data: str) -> CallbackQuery:
    bot = AsyncMock()
    msg = Message.model_construct(
        message_id=5, date=None, chat=Chat.model_construct(id=1, type="private")
    ).as_(bot)
    cb = CallbackQuery.model_construct(
        id="1", from_user=TgUser.model_construct(id=1, is_bot=False, first_name="T"),
        chat_instance="ci", data=data, message=msg,
    )
    return cb.as_(bot)


def _sent(cb: CallbackQuery) -> list[str]:
    """Тексты всего, что ушло в Telegram: всплывашки колбэка и сообщения."""
    return [getattr(c.args[0], "text", None) or "" for c in cb.bot.await_args_list]


def _buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


# --- клавиатура ---------------------------------------------------------------


def test_кнопка_в_меню_третьим_рядом_и_старые_ряды_на_месте():
    rows = [[b.text for b in row] for row in main_menu_keyboard().keyboard]
    assert BTN_FREQUENT in MENU_BUTTONS
    assert rows[0] == ["📊 Сегодня", "📈 Неделя", "🗒 История"]
    assert rows[1] == ["⚖️ Вес", "🎯 Цель", "⚙️ Профиль"]
    assert rows[2] == ["⚡ Частое", "↩️ Отменить", "🐛 Баг"]


def test_потолки_раздельные_по_одной_в_ряд_и_callback_в_лимите():
    sets = [_wrap(1, f"набор {i}") for i in range(5)]
    sets[0].template_name = "очень длинное имя набора " * 3  # кириллица: имя целиком не влезло бы
    rows = [(None, "незакреплённое", 100.0, 9)] + [
        (i, f"продукт {i}", 152.5, 5) for i in range(1, 13)
    ]
    kb = frequent_keyboard(sets, rows)
    buttons = _buttons(kb)
    assert len(buttons) == FREQ_SETS_MAX + FREQ_PRODUCTS_MAX  # наборы продукты не вытеснили
    assert all(len(r) == 1 for r in kb.inline_keyboard)
    assert [b.text.startswith("📌") for b in buttons] == [True] * 3 + [False] * 8
    assert buttons[0].callback_data == f"fq:s:{set_key(sets[0].template_name)}"
    assert buttons[0].text == "📌 очень длинное имя набор… · 357 ккал"  # режется имя, не число
    assert buttons[3].callback_data == "fq:p:1:152.5"  # граммы с кнопки = записанные
    assert buttons[3].text == "продукт 1 · 152.5 г"
    assert all("незакреплённое" not in b.text for b in buttons)
    assert all(len((b.callback_data or "").encode()) <= 64 for b in buttons)


def test_одинаковые_после_обрезки_имена_различимы():
    rows = [(1, "куриное филе гриль на углях с травами", 150.0, 3),
            (2, "куриное филе гриль на углях без соуса", 150.0, 3)]
    texts = [b.text for b in _buttons(frequent_keyboard([], rows))]
    assert len(set(texts)) == 2 and all(t.endswith("· 150 г") for t in texts)
    assert all(len(t.removesuffix(" · 150 г")) <= 24 for t in texts)


def test_граммы_фильтруются_после_округления():
    kb = frequent_keyboard([], [(1, "соль", 0.04, 9), (2, "сахар", 5.0, 3)])
    assert [b.text for b in _buttons(kb)] == ["сахар · 5 г"]


def test_наборы_склеиваются_без_регистра_свежий_побеждает():
    fresh, old = _wrap(1, "Курица в лаваше", hours_ago=1), _wrap(1, "курица в лаваше")
    kb = frequent_keyboard([fresh, old], [])
    assert [b.callback_data for b in _buttons(kb)] == [f"fq:s:{set_key('Курица в лаваше')}"]


def test_нечего_показать_клавиатуры_нет():
    assert frequent_keyboard([], []) is None
    assert frequent_keyboard([], [(None, "что-то", 100.0, 3)]) is None


# --- экран «⚡ Частое» ---------------------------------------------------------


async def test_пусто_две_короткие_строки_без_клавиатуры():
    assistant, user, _, _ = await _world()
    msg = AsyncMock()
    await handlers.btn_frequent(msg, user, assistant, AsyncMock())
    text = msg.answer.await_args.args[0]
    assert "Пока пусто" in text and text.count("\n") == 1
    assert "reply_markup" not in msg.answer.await_args.kwargs


async def test_экран_набор_первым_имя_продукта_из_каталога():
    assistant, user, products, meals = await _world()
    p = await _tvorog(products)
    for _ in range(3):
        await assistant.log_quick(user, p.id, 180)
    p.display_name = "творог 5% Савушкин"  # переименовали в каталоге — кнопка называется так же
    await meals.add(_wrap(user.id))
    msg = AsyncMock()
    await handlers.btn_frequent(msg, user, assistant, AsyncMock())
    head = msg.answer.await_args.args[0]
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert "📌 — наборы" in head
    assert [b.text for b in _buttons(kb)] == [
        f"📌 {SET} · 357 ккал", "творог 5% Савушкин · 180 г"
    ]


async def test_шапка_без_строки_про_наборы_когда_их_нет():
    assistant, user, products, _ = await _world()
    p = await _tvorog(products)
    await assistant.log_quick(user, p.id, 180)
    msg = AsyncMock()
    await handlers.btn_frequent(msg, user, assistant, AsyncMock())
    assert "📌" not in msg.answer.await_args.args[0]


# --- тап ----------------------------------------------------------------------


async def test_тап_по_продукту_пишет_граммы_с_кнопки_без_модели(monkeypatch):
    card = AsyncMock()
    monkeypatch.setattr(handlers, "_send_meal_card", card)
    assistant, user, products, meals = await _world()
    p = await _tvorog(products)
    session = AsyncMock()
    await handlers.on_frequent_tap(_callback(f"fq:p:{p.id}:180"), user, assistant, session)
    [meal] = meals._meals
    [item] = meal.items
    assert item.product_id == p.id and item.grams == 180
    assert item.macros.kcal == pytest.approx(121 * 1.8)  # запиннутая плотность × граммы (INV-2)
    assert meal.eaten_at.date() == datetime.now(UTC).date()
    session.commit.assert_awaited_once()
    result = card.await_args.args[1]
    assert isinstance(result, MealLogged) and result.meal is meal


async def test_фиксация_раньше_карточки(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(handlers, "_send_meal_card",
                        AsyncMock(side_effect=lambda *a, **k: order.append("card")))
    assistant, user, products, _ = await _world()
    p = await _tvorog(products)
    session = AsyncMock()
    session.commit.side_effect = lambda: order.append("commit")
    await handlers.on_frequent_tap(_callback(f"fq:p:{p.id}:180"), user, assistant, session)
    assert order == ["commit", "card"]


async def test_тап_по_набору_повторяет_снапшот_и_ккал_как_на_кнопке(monkeypatch):
    monkeypatch.setattr(handlers, "_send_meal_card", AsyncMock())
    assistant, user, _, meals = await _world()
    src = await meals.add(_wrap(user.id))
    msg = AsyncMock()
    await handlers.btn_frequent(msg, user, assistant, AsyncMock())
    button = _buttons(msg.answer.await_args.kwargs["reply_markup"])[0]
    await handlers.on_frequent_tap(_callback(button.callback_data), user, assistant, AsyncMock())
    new = meals._meals[-1]
    assert new.id != src.id and new.template_name == SET  # имя едет с повтором
    assert [(i.name_snapshot, i.grams, i.macros) for i in new.items] == [
        (i.name_snapshot, i.grams, i.macros) for i in src.items
    ]  # INV-3: снапшот один-в-один, без пересчёта
    assert button.text.endswith(f"· {round(new.totals.kcal)} ккал")


async def test_удалили_свежий_приём_набора_повторяется_предыдущий(monkeypatch):
    monkeypatch.setattr(handlers, "_send_meal_card", AsyncMock())
    assistant, user, _, meals = await _world()
    await meals.add(_wrap(user.id, chicken=120, hours_ago=5))
    fresh = await meals.add(_wrap(user.id, chicken=180, hours_ago=1))
    await assistant.delete_meal(user, fresh.id)
    await handlers.on_frequent_tap(
        _callback(f"fq:s:{set_key(SET)}"), user, assistant, AsyncMock()
    )
    assert meals._meals[-1].items[0].grams == 120


async def test_двойной_тап_пишет_один_раз_и_говорит_об_этом(monkeypatch):
    monkeypatch.setattr(handlers, "_send_meal_card", AsyncMock())
    clock = [100.0]
    monkeypatch.setattr(handlers, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    assistant, user, products, meals = await _world()
    p = await _tvorog(products)
    data = f"fq:p:{p.id}:180"
    await handlers.on_frequent_tap(_callback(data), user, assistant, AsyncMock())
    clock[0] += 2
    second = _callback(data)
    await handlers.on_frequent_tap(second, user, assistant, AsyncMock())
    assert len(meals._meals) == 1
    assert "Уже записал" in _sent(second)[0]
    clock[0] += handlers.TAP_WINDOW_S + 1  # позже окна — это вторая порция, она законна
    await handlers.on_frequent_tap(_callback(data), user, assistant, AsyncMock())
    assert len(meals._meals) == 2


async def test_пока_первый_пишет_второй_слышит_уже_записываю(monkeypatch):
    monkeypatch.setattr(handlers, "_send_meal_card", AsyncMock())
    assistant, user, products, meals = await _world()
    p = await _tvorog(products)
    real = assistant.log_quick

    async def slow(*a, **kw):
        await asyncio.sleep(0.01)  # первый тап реально отдаёт управление посреди записи
        return await real(*a, **kw)

    monkeypatch.setattr(assistant, "log_quick", slow)
    data = f"fq:p:{p.id}:180"
    first, second = _callback(data), _callback(data)
    await asyncio.gather(
        handlers.on_frequent_tap(first, user, assistant, AsyncMock()),
        handlers.on_frequent_tap(second, user, assistant, AsyncMock()),
    )
    assert len(meals._meals) == 1
    assert _sent(second) == ["Уже записываю…"]


async def test_другого_человека_окно_не_касается(monkeypatch):
    monkeypatch.setattr(handlers, "_send_meal_card", AsyncMock())
    assistant, user, products, meals = await _world()
    other = User(id=2, telegram_id=2, name="B", locale="ru", timezone="UTC")
    p = await _tvorog(products)
    data = f"fq:p:{p.id}:180"
    await handlers.on_frequent_tap(_callback(data), user, assistant, AsyncMock())
    await handlers.on_frequent_tap(_callback(data), other, assistant, AsyncMock())
    assert sorted(m.user_id for m in meals._meals) == [1, 2]


async def test_сбой_фиксации_не_держит_повтор(monkeypatch):
    card = AsyncMock()
    monkeypatch.setattr(handlers, "_send_meal_card", card)
    assistant, user, products, _ = await _world()
    p = await _tvorog(products)
    data = f"fq:p:{p.id}:180"
    broken = AsyncMock()
    broken.commit.side_effect = RuntimeError("db down")
    with pytest.raises(RuntimeError):
        await handlers.on_frequent_tap(_callback(data), user, assistant, broken)
    assert card.await_count == 0
    retry, session = _callback(data), AsyncMock()
    await handlers.on_frequent_tap(retry, user, assistant, session)
    session.commit.assert_awaited_once()
    card.assert_awaited_once()
    assert not any("Уже" in t for t in _sent(retry))


async def test_устаревшая_кнопка_не_пишет_и_не_блокирует_повтор(monkeypatch):
    card = AsyncMock()
    monkeypatch.setattr(handlers, "_send_meal_card", card)
    assistant, user, _, meals = await _world()
    for data in ("fq:p:999:180", "fq:s:0000000000", "fq:p:999:180"):
        cb = _callback(data)
        await handlers.on_frequent_tap(cb, user, assistant, AsyncMock())
        assert "открой ⚡ Частое заново" in _sent(cb)[-1]
    assert meals._meals == [] and card.await_count == 0


@pytest.mark.parametrize("grams", ["0", "nan", "inf", "99999", "abc"])
async def test_кривые_граммы_не_пишут(grams, monkeypatch):
    monkeypatch.setattr(handlers, "_send_meal_card", AsyncMock())
    assistant, user, products, meals = await _world()
    p = await _tvorog(products)
    await handlers.on_frequent_tap(_callback(f"fq:p:{p.id}:{grams}"), user, assistant, AsyncMock())
    assert meals._meals == []
