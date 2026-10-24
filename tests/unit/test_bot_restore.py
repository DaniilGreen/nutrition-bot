"""«⏪ Вернуть» после удаления в боте (FR-41, AC-101…AC-108, D-MA-23).

Держим: у каждого из четырёх путей удаления есть кнопка; возврат идёт тем же `restore_meal`, что у
аппки, и цифры до знака как до удаления (INV-3); двойной тап возвращает один раз; позже окна и
после нового удаления — отказ словами и снятая кнопка; сбой не съедает снимок; приём, который с
тех пор поправили или удалили, не откатывается и не задваивается; откаченное удаление «Вернуть»
не показывает.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageReplyMarkup, EditMessageText, SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.types import User as TgUser

from app.application.assistant import Assistant
from app.application.entities import Meal, MealItem
from app.application.meal_service import MealService
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.bot import handlers
from app.domain.goals import Goal
from app.domain.value_objects import Macros
from tests.fakes import InMemoryMealRepo, InMemoryProductRepo, InMemoryUserRepo, InMemoryWeightRepo

SET = "курица в лаваше"


class _NoLlm:
    async def parse(self, *a, **kw):
        raise AssertionError("LLM не должен вызываться")

    async def parse_samples(self, *a, **kw):
        raise AssertionError("LLM не должен вызываться")


@pytest.fixture(autouse=True)
def _fresh_trash():
    handlers._trash.clear()
    yield
    handlers._trash.clear()


@pytest.fixture
def edits(monkeypatch):
    """Rich-правка и правка текста — моки: смотрим, ЧТО легло в сообщение и с какими кнопками."""
    rich, plain = AsyncMock(), AsyncMock()
    monkeypatch.setattr(handlers, "_rich_edit", rich)
    monkeypatch.setattr(handlers, "_safe_edit", plain)
    return SimpleNamespace(rich=rich, plain=plain)


async def _world(clock=None):
    users, products, meals = InMemoryUserRepo(), InMemoryProductRepo(), InMemoryMealRepo()
    service = MealService(products, meals)
    extra = {"clock": clock} if clock else {}
    stats = StatsService(meals, users, Goal(2000, 150, 65, 250), **extra)
    assistant = Assistant(_NoLlm(), products, service, WeightService(InMemoryWeightRepo()), stats)
    user = await users.get_or_create(telegram_id=1, name="T", locale="ru", timezone="UTC")
    return SimpleNamespace(assistant=assistant, user=user, meals=meals, service=service)


def _wrap(uid: int, *, hours_ago: float = 1, name: str | None = SET, at: datetime | None = None,
          one: bool = False) -> Meal:
    items = [
        MealItem(name_snapshot="курица с мазиком", grams=120, macros=Macros(220.4, 22.1, 13.3, 2.2),
                 product_id=7, is_estimate=True),
        MealItem(name_snapshot="лаваш", grams=50, macros=Macros(137, 4.5, 0.6, 28)),
    ][: 1 if one else 2]
    return Meal(
        user_id=uid, eaten_at=at or datetime.now(UTC) - timedelta(hours=hours_ago),
        input_type="text", raw_input="курица с мазиком 120, лаваш 50", items=items,
        source_confidence=0.9, totals=Macros(357.4, 26.6, 13.9, 30.2), template_name=name,
    )


def _shape(meal: Meal) -> tuple:
    """Всё, что «Вернуть» обязан вернуть как было: время, снапшоты позиций, итог, имя набора."""
    return (
        meal.eaten_at, meal.raw_input, meal.totals, meal.template_name,
        [(i.name_snapshot, i.grams, i.macros, i.product_id, i.is_estimate) for i in meal.items],
    )


def _callback(data: str, *, text: str | None = None, markup=None) -> CallbackQuery:
    bot = AsyncMock()
    msg = Message.model_construct(
        message_id=5, date=None, chat=Chat.model_construct(id=1, type="private"),
        text=text, reply_markup=markup,
    ).as_(bot)
    cb = CallbackQuery.model_construct(
        id="1", from_user=TgUser.model_construct(id=1, is_bot=False, first_name="T"),
        chat_instance="ci", data=data, message=msg,
    )
    return cb.as_(bot)


def _answers(cb: CallbackQuery) -> list[str]:
    return [getattr(c.args[0], "text", None) or "" for c in cb.bot.await_args_list]


def _calls(cb: CallbackQuery, kind: type) -> list:
    return [c.args[0] for c in cb.bot.await_args_list if isinstance(c.args[0], kind)]


def _buttons(kb: InlineKeyboardMarkup) -> list[InlineKeyboardButton]:
    return [b for row in kb.inline_keyboard for b in row]


def _back(kb: InlineKeyboardMarkup) -> InlineKeyboardButton:
    """«⏪ Вернуть» — первой кнопкой: сразу после удаления нужна именно она."""
    first = _buttons(kb)[0]
    assert first.text.startswith("⏪ Вернуть: ") and first.callback_data.startswith("rst:")
    assert len(first.callback_data.encode()) <= 64
    return first


def _no_back(kb: InlineKeyboardMarkup | None) -> bool:
    return kb is None or all(not (b.callback_data or "").startswith("rst:") for b in _buttons(kb))


async def _do(handler, data: str, w, session=None, **kw) -> CallbackQuery:
    """Тап по кнопке: хендлер удаления или возврата, у всех одна подпись (сессия — для фиксации)."""
    cb = _callback(data, **kw)
    await handler(cb, w.user, w.assistant, session or AsyncMock())
    return cb


def _restore(data: str, w, session=None, **kw):
    return _do(handlers.on_restore, data, w, session, **kw)


# --- четыре пути удаления: у каждого «⏪ Вернуть», и цифры возвращаются до знака ------------


async def test_позиция_под_карточкой_возвращается_как_была(edits):
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id))
    before = _shape(src)
    await _do(handlers.on_item_del, f"item_del:{src.id}:{src.items[1].id}", w)
    assert [i.name_snapshot for i in w.meals._meals[0].items] == ["курица с мазиком"]
    _, html, kb = edits.rich.await_args.args
    assert "✏️ Поправил" in html  # MA-INV-70: правка, а не новая запись
    back = _back(kb)
    assert back.text == "⏪ Вернуть: лаваш" and back.callback_data.endswith(":c")

    session = AsyncMock()
    cb = await _restore(back.callback_data, w, session)
    [restored] = w.meals._meals
    assert _shape(restored) == before  # INV-3: КБЖУ, граммы, время, имя набора — как до удаления
    session.commit.assert_awaited_once()
    _, html, kb = edits.rich.await_args.args
    assert html.startswith("<h3>⏪ Вернул") and "лаваш" in html
    assert _no_back(kb) and any(b.text.startswith("🗑") for b in _buttons(kb))
    assert _answers(cb) == ["⏪ Вернул"]


async def test_отменить_всё_карточка_остаётся_подтверждением_и_приём_возвращается(edits):
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id))
    before = _shape(src)
    cb = await _do(handlers.on_meal_undo, f"meal_undo:{src.id}", w)
    assert w.meals._meals == []
    _, html, kb = edits.rich.await_args.args
    assert html.startswith("<h3>🗑 Отменил приём") and "<b>357</b> ккал" in html
    [back] = _buttons(kb)  # на отменённом приёме других кнопок нет — им не к чему относиться
    assert back.text == "⏪ Вернуть: курица с мазиком, лаваш"  # 23 знака — целиком
    assert not any(type(c.args[0]).__name__ == "DeleteMessage" for c in cb.bot.await_args_list)

    await _restore(back.callback_data, w)
    [restored] = w.meals._meals
    assert _shape(restored) == before and restored.id != src.id  # новым id, как в аппке
    assert edits.rich.await_args.args[1].startswith("<h3>⏪ Вернул")
    old = await _do(handlers.on_meal_undo, f"meal_undo:{src.id}", w)  # кнопка со старым id
    assert _answers(old) == ["Уже удалено."] and len(w.meals._meals) == 1


async def test_пикер_приём_целиком(edits):
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id))
    other = await w.meals.add(_wrap(w.user.id, hours_ago=3, name=None))
    before = _shape(src)
    await _do(handlers.on_meal_undo, f"meal_undo:{src.id}", w, text=handlers._PICKER_HEAD)
    _, text, kb = edits.plain.await_args.args
    assert text == f"🗑 Отменил: курица с мазиком, лаваш · 357 ккал\n\n{handlers._PICKER_HEAD}"
    back = _back(kb)
    assert back.callback_data.endswith(":p")
    assert any(b.callback_data == f"meal_undo:{other.id}" for b in _buttons(kb))  # список живой

    await _restore(back.callback_data, w, text=text)
    assert _shape(w.meals._meals[-1]) == before
    _, text, kb = edits.plain.await_args.args
    assert text.startswith("⏪ Вернул: курица с мазиком, лаваш · 357 ккал\n\n")
    assert _no_back(kb) and len([b for b in _buttons(kb) if b.text.startswith("↩️")]) == 2


async def test_пикер_позиция(edits):
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id))
    before = _shape(src)
    await _do(handlers.on_undo_item, f"undo_item:{src.id}:{src.items[0].id}", w,
              text=handlers._PICKER_HEAD)
    _, text, kb = edits.plain.await_args.args
    assert text.startswith("🗑 Убрал: курица с мазиком · 220 ккал")
    await _restore(_back(kb).callback_data, w)
    assert _shape(w.meals._meals[0]) == before and len(w.meals._meals) == 1


async def test_пикер_последняя_позиция_и_пустой_пикер(edits):
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id, one=True))
    before = _shape(src)
    await _do(handlers.on_undo_item, f"undo_item:{src.id}:{src.items[0].id}", w,
              text=handlers._PICKER_HEAD)
    assert w.meals._meals == []  # последняя позиция — приём удалён целиком
    _, text, kb = edits.plain.await_args.args
    assert "Больше нечего отменять" in text
    [back] = _buttons(kb)  # пикер опустел, а «Вернуть» осталась
    await _restore(back.callback_data, w)
    assert [_shape(m) for m in w.meals._meals] == [before]


async def test_экран_дня_перерисован_с_кнопкой_и_после_возврата(edits):
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id, hours_ago=0))
    before = _shape(src)
    await _do(handlers.on_day_del, f"day_del:0:{src.id}:{src.items[0].id}", w)
    _, html, kb = edits.rich.await_args.args
    back = _back(kb)
    today = datetime.now(UTC).date()
    assert back.callback_data.endswith(f":d{today:%Y%m%d}")  # дата, а не «дней назад»
    assert back.text == "⏪ Вернуть: курица с мазиком"
    assert _buttons(kb)[1].text == "← пред. день"  # навигация на месте, под кнопкой

    await _restore(back.callback_data, w)
    assert _shape(w.meals._meals[0]) == before
    _, html, kb = edits.rich.await_args.args
    assert "курица с мазиком" in html and _no_back(kb)


async def test_экран_дня_последняя_позиция_и_возврат(edits):
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id, hours_ago=0, one=True))
    before = _shape(src)
    await _do(handlers.on_day_del, f"day_del:0:{src.id}:{src.items[0].id}", w)
    assert w.meals._meals == []
    await _restore(_back(edits.rich.await_args.args[2]).callback_data, w)
    assert [_shape(m) for m in w.meals._meals] == [before]


async def test_после_полуночи_возвращает_на_тот_же_день(edits, monkeypatch):
    now = [datetime(2026, 10, 24, 23, 50, tzinfo=UTC)]
    w = await _world(clock=lambda: now[0])
    src = await w.meals.add(_wrap(w.user.id, at=now[0] - timedelta(minutes=20)))
    await _do(handlers.on_day_del, f"day_del:0:{src.id}:{src.items[0].id}", w)
    back = _back(edits.rich.await_args.args[2])
    assert back.callback_data.endswith(":d20261024")
    now[0] += timedelta(minutes=15)  # 00:05 следующего дня
    asked: list[int] = []
    real = w.assistant.day_card

    async def spy(user, days_ago=0):
        asked.append(days_ago)
        return await real(user, days_ago)

    monkeypatch.setattr(w.assistant, "day_card", spy)
    await _restore(back.callback_data, w)
    assert asked == [1]  # «вчера», а не сегодняшний пустой день


def test_callback_вернуть_в_лимите():
    from app.bot.keyboards import restore_button

    b = restore_button("A" * 8, "d20261024", "очень длинное имя позиции " * 4)
    assert len(b.callback_data.encode()) <= 64
    assert b.text == "⏪ Вернуть: очень длинное имя позиц…"  # 24 знака с «…»


async def test_fallback_правки_несёт_кнопку_вернуть():
    """Telegram правку не принял (MA-INV-70) — короткий текст, но «⏪ Вернуть» с ним."""
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id))
    cb = _callback(f"item_del:{src.id}:{src.items[1].id}")

    async def bot(method, *a, **kw):
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method=method, message="Bad Request: message can't be edited")

    cb.bot.side_effect = bot
    await handlers.on_item_del(cb, w.user, w.assistant, AsyncMock())
    [sent] = _calls(cb, SendMessage)
    assert sent.text.startswith("🗑 Убрал: лаваш · приём теперь 220 ккал")
    assert _back(sent.reply_markup).text == "⏪ Вернуть: лаваш"


# --- двойной тап, окно, второе удаление -------------------------------------------------------


async def test_двойной_тап_возвращает_один_раз(edits):
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id))
    await _do(handlers.on_meal_undo, f"meal_undo:{src.id}", w)
    data = _back(edits.rich.await_args.args[2]).callback_data
    await _restore(data, w)
    second = await _restore(data, w)
    assert len(w.meals._meals) == 1
    assert _answers(second) == ["Уже вернул."]  # только всплывашка: сообщение перерисовал первый


async def test_параллельные_тапы_возвращают_один_раз_второй_сообщение_не_трогает(
    edits, monkeypatch
):
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id))
    await _do(handlers.on_meal_undo, f"meal_undo:{src.id}", w)
    kb = edits.rich.await_args.args[2]
    data = _back(kb).callback_data
    real = w.assistant.restore_meal

    async def slow(*a, **kw):
        await asyncio.sleep(0.01)  # первый тап отдаёт управление посреди возврата
        return await real(*a, **kw)

    monkeypatch.setattr(w.assistant, "restore_meal", slow)
    first, second = _callback(data, markup=kb), _callback(data, markup=kb)
    await asyncio.gather(
        handlers.on_restore(first, w.user, w.assistant, AsyncMock()),
        handlers.on_restore(second, w.user, w.assistant, AsyncMock()),
    )
    assert len(w.meals._meals) == 1
    assert _answers(second) == ["Уже вернул."]
    assert _calls(second, EditMessageReplyMarkup) == []


async def test_позже_окна_отказ_и_снята_только_кнопка_вернуть(edits, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(handlers, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id, hours_ago=0))
    await _do(handlers.on_day_del, f"day_del:0:{src.id}:{src.items[0].id}", w)
    kb = edits.rich.await_args.args[2]
    clock[0] += handlers.UNDO_TTL_S + 60
    cb = await _restore(_back(kb).callback_data, w, markup=kb)
    assert [i.name_snapshot for i in w.meals._meals[0].items] == ["лаваш"]  # ничего не вернули
    assert _answers(cb)[0] == "Вернуть уже нельзя — прошло больше 10 минут."
    [edit] = _calls(cb, EditMessageReplyMarkup)
    left = _buttons(edit.reply_markup)
    assert _no_back(edit.reply_markup) and len(left) == len(_buttons(kb)) - 1  # навигация жива


async def test_внутри_окна_работает(edits, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(handlers, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id))
    await _do(handlers.on_meal_undo, f"meal_undo:{src.id}", w)
    clock[0] += handlers.UNDO_TTL_S - 1
    await _restore(_back(edits.rich.await_args.args[2]).callback_data, w)
    assert len(w.meals._meals) == 1


async def test_после_второго_удаления_вернуть_можно_только_последнее(edits):
    w = await _world()
    first = await w.meals.add(_wrap(w.user.id))
    second = await w.meals.add(_wrap(w.user.id, hours_ago=2, name=None))
    await _do(handlers.on_meal_undo, f"meal_undo:{first.id}", w)
    old = _back(edits.rich.await_args.args[2])
    await _do(handlers.on_meal_undo, f"meal_undo:{second.id}", w)
    new = _back(edits.rich.await_args.args[2])

    cb = await _restore(old.callback_data, w, markup=InlineKeyboardMarkup(inline_keyboard=[[old]]))
    assert w.meals._meals == []
    assert _answers(cb)[0] == "Вернуть можно только последнее удалённое."
    [edit] = _calls(cb, EditMessageReplyMarkup)
    assert edit.reply_markup is None  # кнопка снята, других не было
    await _restore(new.callback_data, w)
    assert [m.template_name for m in w.meals._meals] == [None]  # вернулся второй


# --- данные: правка после удаления, откат удаления, двойной тап по 🗑 -----------------------


async def test_поправили_словами_после_удаления_вернуть_нельзя(edits):
    """Правка «курица 100» после 🗑 лаваша: возврат снимка молча откатил бы её — отказ."""
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id))
    await _do(handlers.on_item_del, f"item_del:{src.id}:{src.items[1].id}", w)
    kb = edits.rich.await_args.args[2]
    assert await w.service.adjust_item_by_name(w.user, "курица", 100) is not None  # путь «слов»
    cb = await _restore(_back(kb).callback_data, w, markup=kb)
    assert _answers(cb)[0] == "Приём уже поправили — вернуть нельзя."
    [meal] = w.meals._meals
    assert [(i.name_snapshot, i.grams) for i in meal.items] == [("курица с мазиком", 100)]
    [edit] = _calls(cb, EditMessageReplyMarkup)
    assert _no_back(edit.reply_markup) and _buttons(edit.reply_markup)  # остальные кнопки живы
    again = await _restore(_back(kb).callback_data, w)
    assert _answers(again)[0] == "Приём уже поправили — вернуть нельзя."


async def test_удалили_в_аппке_обрезанный_приём_возврат_не_задваивает(edits):
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id))
    await _do(handlers.on_item_del, f"item_del:{src.id}:{src.items[1].id}", w)
    data = _back(edits.rich.await_args.args[2]).callback_data
    await w.assistant.delete_meal(w.user, src.id)  # тот же use-case, что у DELETE аппки
    cb = await _restore(data, w)
    assert _answers(cb)[0] == "Приём уже поправили — вернуть нельзя."
    assert w.meals._meals == []  # ни воскрешения, ни дубля


async def test_откаченное_удаление_не_показывает_вернуть_и_не_трогает_прежний_снимок(edits):
    w = await _world()
    first = await w.meals.add(_wrap(w.user.id))
    second = await w.meals.add(_wrap(w.user.id, hours_ago=2, name=None))
    await _do(handlers.on_meal_undo, f"meal_undo:{first.id}", w)
    old = _back(edits.rich.await_args.args[2])
    shown = edits.rich.await_count
    broken = AsyncMock()
    broken.commit.side_effect = RuntimeError("commit")
    with pytest.raises(RuntimeError):
        await _do(handlers.on_item_del, f"item_del:{second.id}:{second.items[0].id}", w, broken)
    assert edits.rich.await_count == shown  # ни «✏️ Поправил», ни кнопки для откаченного
    assert handlers._trash[w.user.id].token == old.callback_data.split(":")[1]  # снимок цел


async def test_двойной_тап_по_корзине_не_портит_снимок_и_лечит_карточку(edits):
    """Второй тап по 🗑 той же позиции: снимок первого цел, карточка перерисована с его кнопкой."""
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id))
    before = _shape(src)
    data = f"item_del:{src.id}:{src.items[1].id}"
    await _do(handlers.on_item_del, data, w)
    kb = edits.rich.await_args.args[2]
    back = _back(kb)
    again = await _do(handlers.on_item_del, data, w, markup=kb)
    assert _answers(again) == ["Уже убрано."]
    _, html, healed = edits.rich.await_args.args
    assert "лаваш" not in html and _back(healed).callback_data == back.callback_data
    await _restore(back.callback_data, w)
    assert _shape(w.meals._meals[0]) == before


async def test_уже_убрано_на_экране_дня_лечит_экран_и_держит_кнопку(edits):
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id, hours_ago=0))
    data = f"day_del:0:{src.id}:{src.items[0].id}"
    await _do(handlers.on_day_del, data, w)
    kb = edits.rich.await_args.args[2]
    again = await _do(handlers.on_day_del, data, w, markup=kb)
    assert _answers(again) == ["Уже убрано."]
    assert _back(edits.rich.await_args.args[2]).callback_data == _back(kb).callback_data


async def test_уже_убрано_в_пикере_держит_строку_и_кнопку(edits):
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id))
    data = f"undo_item:{src.id}:{src.items[0].id}"
    await _do(handlers.on_undo_item, data, w, text=handlers._PICKER_HEAD)
    _, text, kb = edits.plain.await_args.args
    again = await _do(handlers.on_undo_item, data, w, text=text, markup=kb)
    assert _answers(again) == ["Уже убрано."]
    _, text2, kb2 = edits.plain.await_args.args
    assert text2 == text and _back(kb2).callback_data == _back(kb).callback_data


async def test_отменить_всё_второй_раз_карточку_не_трогает(edits):
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id))
    await _do(handlers.on_meal_undo, f"meal_undo:{src.id}", w)
    again = await _do(handlers.on_meal_undo, f"meal_undo:{src.id}", w)
    assert _answers(again) == ["Уже удалено."]  # и никакого DeleteMessage — «Вернуть» цел
    assert edits.rich.await_count == 1


async def test_сбой_возврата_не_съедает_снимок(edits, monkeypatch):
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id))
    await _do(handlers.on_meal_undo, f"meal_undo:{src.id}", w)
    data = _back(edits.rich.await_args.args[2]).callback_data
    real = w.assistant.restore_meal
    monkeypatch.setattr(w.assistant, "restore_meal", AsyncMock(side_effect=RuntimeError("db")))
    with pytest.raises(RuntimeError):
        await _restore(data, w)
    monkeypatch.setattr(w.assistant, "restore_meal", real)
    await _restore(data, w)
    assert len(w.meals._meals) == 1


async def test_чужой_и_пропавший_снимок(edits):
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id))
    await _do(handlers.on_meal_undo, f"meal_undo:{src.id}", w)
    data = _back(edits.rich.await_args.args[2]).callback_data
    stranger = SimpleNamespace(**vars(w))
    stranger.user = SimpleNamespace(id=999, timezone="UTC")
    cb = await _restore(data, stranger)
    assert _answers(cb)[0] == "Вернуть уже нельзя." and w.meals._meals == []
    handlers._trash.clear()  # рестарт бота
    cb = await _restore(data, w)
    assert _answers(cb)[0] == "Вернуть уже нельзя." and w.meals._meals == []


async def test_двойной_тап_по_корзине_со_старой_клавиатурой_кнопку_не_стирает(edits):
    """AC-115: второй тап несёт клавиатуру ДО первого (без ⏪) и перерисовывает последним —
    «⏪ Вернуть» берётся из живого снимка, а не со старой клавиатуры."""
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id, hours_ago=0))
    old = [[InlineKeyboardButton(text="🔁", callback_data="x")]]
    card = InlineKeyboardMarkup(inline_keyboard=old)
    data = f"item_del:{src.id}:{src.items[1].id}"
    await _do(handlers.on_item_del, data, w, markup=card)
    back = _back(edits.rich.await_args.args[2])
    again = await _do(handlers.on_item_del, data, w, markup=card)  # старая клавиатура, без ⏪
    assert _answers(again) == ["Уже убрано."]
    assert _back(edits.rich.await_args.args[2]).callback_data == back.callback_data

    day = f"day_del:0:{src.id}:{src.items[0].id}"
    await _do(handlers.on_day_del, day, w)
    back = _back(edits.rich.await_args.args[2])
    await _do(handlers.on_day_del, day, w, markup=None)
    assert _back(edits.rich.await_args.args[2]).callback_data == back.callback_data


async def test_живой_снимок_не_про_этот_приём_или_истёк_кнопки_нет(edits, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(handlers, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id))
    await _do(handlers.on_item_del, f"item_del:{src.id}:{src.items[1].id}", w)
    assert handlers._live_restore(w.user, src.id + 1, "c") is None  # снимок про другой приём
    assert handlers._live_restore(w.user, src.id, "c") is not None
    clock[0] += handlers.UNDO_TTL_S + 1
    assert handlers._live_restore(w.user, src.id, "c") is None


async def test_сохранили_набор_после_корзины_вернуть_нельзя(edits):
    """AC-116: 🗑 позиция → «📌 Сохранить набор» на обрезанной карточке → «⏪ Вернуть»: снимок
    молча стёр бы имя набора — отказ, имя цело."""
    w = await _world()
    meal = _wrap(w.user.id, name=None)
    meal.items.append(MealItem(name_snapshot="огурец", grams=60, macros=Macros(9, 0.5, 0.1, 1.8)))
    src = await w.meals.add(meal)
    await _do(handlers.on_item_del, f"item_del:{src.id}:{src.items[2].id}", w)
    kb = edits.rich.await_args.args[2]
    assert await w.assistant.remember_dish(w.user, src.id, SET) is not None
    cb = await _restore(_back(kb).callback_data, w, markup=kb)
    assert _answers(cb)[0] == "Приём уже поправили — вернуть нельзя."
    [now] = w.meals._meals
    assert now.template_name == SET and len(now.items) == 2


async def test_параллельный_двойной_тап_по_корзине_уже_убираю_и_токен_первого(edits, monkeypatch):
    """Второй тап, пока первый ещё убирает, не читает приём до удаления и не кладёт свой снимок
    поверх: «Уже убираю…», один снимок, кнопка первого рабочая."""
    w = await _world()
    src = await w.meals.add(_wrap(w.user.id))
    before = _shape(src)
    real = w.assistant.remove_item

    async def slow(*a, **kw):
        await asyncio.sleep(0.01)  # первый тап отдаёт управление посреди удаления
        return await real(*a, **kw)

    monkeypatch.setattr(w.assistant, "remove_item", slow)
    data = f"item_del:{src.id}:{src.items[1].id}"
    first, second = _callback(data), _callback(data)
    await asyncio.gather(
        handlers.on_item_del(first, w.user, w.assistant, AsyncMock()),
        handlers.on_item_del(second, w.user, w.assistant, AsyncMock()),
    )
    assert _answers(second) == ["Уже убираю…"]
    back = _back(edits.rich.await_args.args[2])
    assert handlers._trash[w.user.id].token == back.callback_data.split(":")[1]
    assert handlers._removing == set()  # ключ отпущен
    await _restore(back.callback_data, w)
    assert _shape(w.meals._meals[0]) == before
