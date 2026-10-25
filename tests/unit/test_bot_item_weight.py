"""«✏️ Вес» на карточке приёма в боте (FR-43, AC-120…AC-127, D-MA-25).

Держим: кнопка есть, только если есть позиция с весом; одна такая позиция — сразу пресеты,
несколько — выбор; пресеты — числа чипов аппки, и запись идёт тем же `adjust_item`, что шторка
аппки: граммы и КБЖУ снапшота пропорционально, без модели (INV-1, INV-3); «✍️ Своё» понимает
«180 г» и «0,2 кг» и переспрашивает вне 1…2000 г; «← Назад» ничего не пишет; двойной тап правит
один раз; устаревшая кнопка не пишет; висящий «⏪ Вернуть» после правки веса отказывает (FR-41).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageReplyMarkup, SendMessage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message
from aiogram.types import User as TgUser

from app.application.assistant import Assistant
from app.application.entities import Meal, MealItem
from app.application.meal_service import MealService
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.bot import handlers
from app.bot.keyboards import (
    item_pick_keyboard,
    item_weight_keyboard,
    meal_actions_keyboard,
    weight_presets,
)
from app.bot.states import MealStates
from app.domain.goals import Goal
from app.domain.value_objects import Macros
from tests.fakes import InMemoryMealRepo, InMemoryProductRepo, InMemoryUserRepo, InMemoryWeightRepo


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
    stats = StatsService(meals, users, Goal(2000, 150, 65, 250))
    service = MealService(products, meals)
    assistant = Assistant(_NoLlm(), products, service, WeightService(InMemoryWeightRepo()), stats)
    user = await users.get_or_create(telegram_id=1, name="T", locale="ru", timezone="UTC")
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    return SimpleNamespace(assistant=assistant, user=user, meals=meals, state=state)


_BUCK = MealItem(name_snapshot="гречка", grams=150, macros=Macros(300, 20, 10, 30), product_id=3)
_CHICKEN = MealItem(name_snapshot="курица", grams=120, macros=Macros(240, 36, 9.6, 0), product_id=4)
_STATED = MealItem(name_snapshot="батончик", grams=0, macros=Macros(210, 10, 8, 24))


def _meal(uid: int, *items: MealItem) -> Meal:
    its = [MealItem(name_snapshot=i.name_snapshot, grams=i.grams, macros=i.macros,
                    product_id=i.product_id) for i in items]
    return Meal(
        user_id=uid, eaten_at=datetime.now(UTC) - timedelta(minutes=1), input_type="text",
        raw_input="—", items=its, source_confidence=0.9,
        totals=Macros(*(sum(getattr(i.macros, f) for i in its)
                        for f in ("kcal", "protein", "fat", "carb"))),
    )


def _callback(data: str, message_id: int = 5) -> CallbackQuery:
    bot = AsyncMock()
    msg = Message.model_construct(
        message_id=message_id, date=None, chat=Chat.model_construct(id=1, type="private"),
    ).as_(bot)
    cb = CallbackQuery.model_construct(
        id="1", from_user=TgUser.model_construct(id=1, is_bot=False, first_name="T"),
        chat_instance="ci", data=data, message=msg,
    )
    return cb.as_(bot)


def _text(text: str) -> Message:
    return Message.model_construct(
        message_id=9, date=None, chat=Chat.model_construct(id=1, type="private"), text=text,
        from_user=TgUser.model_construct(id=1, is_bot=False, first_name="T"),
    ).as_(AsyncMock())


async def _tap(w, data: str, session=None, message_id: int = 5) -> CallbackQuery:
    cb = _callback(data, message_id)
    await handlers.on_item_weight(cb, w.user, w.assistant, session or AsyncMock(), w.state)
    return cb


async def _say(w, text: str, session=None) -> Message:
    msg = _text(text)
    await handlers.on_item_grams_input(
        msg, msg.bot, w.user, w.assistant, session or AsyncMock(), w.state
    )
    return msg


def _answers(cb: CallbackQuery) -> list[str]:
    return [getattr(c.args[0], "text", None) or "" for c in cb.bot.await_args_list
            if type(c.args[0]).__name__ == "AnswerCallbackQuery"]


def _sent(obj) -> list[str]:
    return [c.args[0].text for c in obj.bot.await_args_list if isinstance(c.args[0], SendMessage)]


def _swapped(cb: CallbackQuery) -> InlineKeyboardMarkup:
    """Клавиатура, которой навигация заменила кнопки карточки (текст не трогается)."""
    [edit] = [c.args[0] for c in cb.bot.await_args_list
              if isinstance(c.args[0], EditMessageReplyMarkup)]
    return edit.reply_markup


def _texts(kb: InlineKeyboardMarkup) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


def _data(kb: InlineKeyboardMarkup) -> list[str]:
    return [b.callback_data or "" for row in kb.inline_keyboard for b in row]


def _item(w, name: str) -> MealItem:
    return next(i for m in w.meals._meals for i in m.items if i.name_snapshot == name)


# --- кнопка и выбор позиции (AC-120, AC-125) ------------------------------------


async def test_кнопка_только_при_позиции_с_весом_и_после_повторить():
    w = await _world()
    one = await w.meals.add(_meal(w.user.id, _BUCK))
    two = await w.meals.add(_meal(w.user.id, _CHICKEN, _BUCK))
    stated = await w.meals.add(_meal(w.user.id, _STATED))
    assert _texts(meal_actions_keyboard(one))[:2] == ["🔁 Повторить", "✏️ Вес"]
    assert f"iw:{two.id}" in _data(meal_actions_keyboard(two))
    assert "✏️ Вес" not in _texts(meal_actions_keyboard(stated))  # «свои цифры» — граммов нет


async def test_одна_позиция_сразу_пресеты_несколько_выбор(rich):
    w = await _world()
    one = await w.meals.add(_meal(w.user.id, _BUCK))
    cb = await _tap(w, f"iw:{one.id}")
    kb = _swapped(cb)
    assert _texts(kb) == ["½ · 75 г", "×1,5 · 225 г", "×2 · 300 г", "✍️ Своё", "← гречка · 150 г"]
    assert [len(r) for r in kb.inline_keyboard] == [2, 2, 1]  # три в ряд на 320 px режут числа
    assert _data(kb)[-1] == f"iw:{one.id}:back"  # одна позиция — назад сразу к карточке
    assert _answers(cb) == ["✏️ гречка · сейчас 150 г"]  # всплывашка называет позицию

    two = await w.meals.add(_meal(w.user.id, _CHICKEN, _STATED, _BUCK))
    kb = _swapped(await _tap(w, f"iw:{two.id}"))
    assert _texts(kb) == ["✏️ курица · 120 г", "✏️ гречка · 150 г", "← Назад"]  # без «своих цифр»
    kb = _swapped(await _tap(w, _data(kb)[1]))
    assert _texts(kb)[0] == "½ · 75 г" and _data(kb)[-1] == f"iw:{two.id}"  # назад — к выбору

    mixed = await w.meals.add(_meal(w.user.id, _STATED, _CHICKEN))  # с весом одна — сразу пресеты
    assert _texts(_swapped(await _tap(w, f"iw:{mixed.id}")))[0] == "½ · 60 г"
    assert not rich.await_count and _item(w, "гречка").grams == 150  # навигация ничего не пишет


async def test_назад_ничего_не_пишет(rich):
    w = await _world()
    two = await w.meals.add(_meal(w.user.id, _CHICKEN, _BUCK))
    back = _data(_swapped(await _tap(w, f"iw:{two.id}:{two.items[0].id}")))[-1]
    kb = _swapped(await _tap(w, back))
    assert _texts(kb)[0] == "✏️ курица · 120 г"
    kb = _swapped(await _tap(w, _data(kb)[-1]))
    assert _texts(kb)[:2] == ["🔁 Повторить", "✏️ Вес"]  # обычные кнопки карточки
    assert [i.grams for i in two.items] == [120, 150] and not rich.await_count


# --- пресеты: числа как у аппки, КБЖУ снапшота пропорционально (AC-121, AC-122) ----------


@pytest.mark.parametrize(("tap", "grams", "macros"), [
    (0, 75, Macros(150, 10, 5, 15)),
    (1, 225, Macros(450, 30, 15, 45)),
    (2, 300, Macros(600, 40, 20, 60)),
])
async def test_пресет_масштабирует_граммы_и_кбжу(rich, tap, grams, macros):
    w = await _world()
    src = await w.meals.add(_meal(w.user.id, _BUCK, _CHICKEN))
    preset = _data(item_weight_keyboard(src, src.items[0]))[tap]
    assert preset == f"iw:{src.id}:{src.items[0].id}:{grams}@150"  # граммы и вес-основа
    session = AsyncMock()
    cb = await _tap(w, preset, session)
    buck = _item(w, "гречка")
    assert buck.grams == grams and buck.macros == macros  # тот же adjust_item, снапшот × k
    assert _item(w, "курица").macros == Macros(240, 36, 9.6, 0)  # соседняя позиция цела
    assert src.totals == Macros(240 + macros.kcal, 36 + macros.protein, 9.6 + macros.fat,
                                macros.carb)
    session.commit.assert_awaited_once()
    assert _answers(cb) == [f"✏️ гречка: было 150 г, стало {grams} г"]  # как тост аппки
    _, html, kb = rich.await_args.args
    assert html.startswith("<h3>✏️ Поправил") and f"{grams} г" in html  # MA-INV-70
    assert _texts(kb)[:2] == ["🔁 Повторить", "✏️ Вес"]
    assert "запись теперь" in rich.await_args.kwargs["fallback_text"]


def test_числа_пресетов_как_у_чипов_аппки():
    assert weight_presets(150) == [("½", 75), ("×1,5", 225), ("×2", 300)]
    assert weight_presets(133.3) == [("½", 67), ("×1,5", 200), ("×2", 266)]  # половина вверх
    assert weight_presets(1500) == [("½", 750)]  # 2250 и 3000 — выше 2000 г (INV-11.2)
    assert weight_presets(1) == [("×1,5", 2)]  # ½ = 1 = как есть, ×2 = 2 — дубль


def test_пресеты_по_два_в_ряд_и_выбор_с_тем_же_округлением():
    meal = _meal(1, MealItem(name_snapshot="творог", grams=750, macros=Macros(1, 1, 1, 1)),
                 MealItem(name_snapshot="рис", grams=66.5, macros=Macros(1, 1, 1, 1)))
    for n, it in enumerate(meal.items):
        it.id = n + 1
    meal.id = 9
    kb = item_weight_keyboard(meal, meal.items[0])
    assert _texts(kb) == ["½ · 375 г", "×1,5 · 1125 г", "×2 · 1500 г", "✍️ Своё",
                          "← творог · 750 г"]
    assert [len(r) for r in kb.inline_keyboard] == [2, 2, 1]
    assert _texts(item_pick_keyboard(meal))[1] == "✏️ рис · 67 г"  # half_up, не банковское 66


def test_callback_в_64_байта():
    big = 2_000_000_000
    meal = _meal(1, _BUCK, _CHICKEN)
    meal.id = big
    for n, it in enumerate(meal.items):
        it.id = big + n
    for kb in (meal_actions_keyboard(meal), item_weight_keyboard(meal, meal.items[0])):
        assert all(len(d.encode()) <= 64 for d in _data(kb))
    assert len(f"iw:{big}:{big + 1}:=1999.5@1999.5".encode()) <= 64  # худшее уточнение


# --- «✍️ Своё» (AC-123, AC-124) -----------------------------------------------


@pytest.mark.parametrize(("text", "parsed"), [
    ("180", (180, False)), ("180 г", (180, True)), ("180г", (180, True)),
    ("180 гр.", (180, True)), ("180 грамм", (180, True)), ("0,2 кг", (200, True)),
    ("1.5кг", (1500, True)), ("  95,5 ", (95.5, False)), ("250 g", (250, True)),
    ("0.3 KG", (300, True)), ("1 000 г", (1000, True)), ("1\u00a0500", (1500, False)),
    ("2", (2, False)), ("0,96", (0.96, False)),
])
def test_разбор_ответа_граммами(text, parsed):
    assert handlers.parse_grams_answer(text) == parsed


@pytest.mark.parametrize(
    "text", ["много", "150 200", "150 г гречки", "-5", "", "2 яйца", "кг", "x", "x0"]
)
def test_не_вес_не_разбирается(text):
    assert handlers.parse_grams_answer(text) is None and handlers.parse_times_answer(text) is None


async def test_своё_спрашивает_и_правит_карточку_на_месте(rich):
    w = await _world()
    src = await w.meals.add(_meal(w.user.id, _BUCK))
    cb = await _tap(w, f"iw:{src.id}:{src.items[0].id}:own")
    assert await w.state.get_state() == MealStates.awaiting_item_grams.state
    assert _texts(_swapped(cb))[:2] == ["🔁 Повторить", "✏️ Вес"]  # карточке — обычные кнопки
    [ask] = [c.args[0] for c in cb.bot.await_args_list if isinstance(c.args[0], SendMessage)]
    assert ask.text.startswith("✍️ Сколько граммов: гречка? Сейчас 150 г.")
    assert _data(ask.reply_markup) == [f"iw:{src.id}:{src.items[0].id}:x"]  # своя, не wiz_cancel
    await w.state.update_data(iw_prompt=6)  # id вопроса (мок бота id не выдаёт)

    session = AsyncMock()
    msg = await _say(w, "0,2 кг", session)
    buck = _item(w, "гречка")
    assert buck.grams == 200 and buck.macros == Macros(400, 26.7, 13.3, 40)
    session.commit.assert_awaited_once()
    assert await w.state.get_state() is None
    card, trace = msg.bot.edit_message_text.await_args_list
    assert card.kwargs["message_id"] == 5  # карточка выше, а не новое сообщение
    assert card.kwargs["rich_message"].html.startswith("<h3>✏️ Поправил")
    assert _texts(card.kwargs["reply_markup"])[:2] == ["🔁 Повторить", "✏️ Вес"]
    assert trace.kwargs == {"text": "✏️ гречка: было 150 г, стало 200 г", "chat_id": 1,
                            "message_id": 6}  # вопрос стал следом, без «✖️ Отмена»


@pytest.mark.parametrize("text", ["0", "2500", "3 кг", "0 г"])
async def test_своё_вне_границ_переспрашивает(rich, text):
    w = await _world()
    src = await w.meals.add(_meal(w.user.id, _BUCK))
    await _tap(w, f"iw:{src.id}:{src.items[0].id}:own")
    msg = await _say(w, text)
    assert _sent(msg) == [handlers._GRAMS_BOUNDS] and _item(w, "гречка").grams == 150
    assert await w.state.get_state() == MealStates.awaiting_item_grams.state  # вопрос остаётся
    assert handlers._GRAMS_BOUNDS.startswith("Вес — от 1 до 2000 г за позицию.")


async def _asked(w, src, prompt_id: int = 6):
    """«✍️ Своё» у первой позиции; вопросу — id (мок бота id не выдаёт)."""
    await _tap(w, f"iw:{src.id}:{src.items[0].id}:own")
    await w.state.update_data(iw_prompt=prompt_id)


@pytest.mark.parametrize("text", ["гречка 200", "банан", "гречки было 150", "150 200", "-5"])
async def test_не_вес_не_съедается_а_идёт_записью(rich, monkeypatch, text):
    w = await _world()
    src = await w.meals.add(_meal(w.user.id, _BUCK))
    await _asked(w, src)
    usual = AsyncMock()
    monkeypatch.setattr(handlers, "on_text", usual)
    msg = await _say(w, text)
    assert usual.await_args.args[0] is msg  # тот же путь, что любое сообщение (AC-128)
    assert await w.state.get_state() is None and _item(w, "гречка").grams == 150
    assert msg.bot.edit_message_text.await_args.kwargs == {
        "text": "✍️ Вес «гречка» — вопрос снят.", "chat_id": 1, "message_id": 6,
    }


async def test_голос_и_фото_снимают_вопрос(rich):
    w = await _world()
    src = await w.meals.add(_meal(w.user.id, _BUCK))
    for handler in (handlers.on_voice, handlers.on_photo):
        handlers._taps.clear()  # второй «✍️ Своё» — не двойной тап
        await _asked(w, src)
        msg = Message.model_construct(
            message_id=9, date=None, chat=Chat.model_construct(id=1, type="private"),
            voice=None, photo=None,
        ).as_(AsyncMock())
        extra = (AsyncMock(),) if handler is handlers.on_photo else ()  # scans
        await handler(msg, msg.bot, w.user, w.assistant, AsyncMock(), w.state, *extra)
        assert await w.state.get_state() is None and not _sent(msg)  # не «жду ответ»
        assert msg.bot.edit_message_text.await_args.kwargs["text"].endswith("вопрос снят.")


@pytest.mark.parametrize(("text", "buttons", "data"), [
    ("2", ["2 г", "×2 · 300 г"], ["=2@150", "=300@150"]),
    ("1,5", ["1,5 г", "×1,5 · 225 г"], ["=1.5@150", "=225@150"]),
    ("0,2", ["0,2 кг · 200 г", "×0,2 · 30 г"], ["=200@150", "=30@150"]),  # < 1 — скорее кг
    ("0.35", ["0,35 кг · 350 г", "×0,35 · 53 г"], ["=350@150", "=53@150"]),
    ("0,96", ["0,96 кг · 960 г", "×0,96 · 144 г"], ["=960@150", "=144@150"]),
    ("5", ["5 г"], ["=5@150"]),
])
async def test_голое_малое_число_уточняется_кнопками(rich, text, buttons, data):
    w = await _world()
    src = await w.meals.add(_meal(w.user.id, _BUCK))
    head = f"iw:{src.id}:{src.items[0].id}"
    await _asked(w, src)
    msg = await _say(w, text)
    assert _item(w, "гречка").grams == 150  # граммами не записано
    [ask] = [c.args[0] for c in msg.bot.await_args_list if isinstance(c.args[0], SendMessage)]
    assert _texts(ask.reply_markup) == [*buttons, "✖️ Отмена"]
    assert _data(ask.reply_markup) == [*(f"{head}:{d}" for d in data), f"{head}:x"]
    assert ("единицей" in ask.text) == (text == "5")
    assert await w.state.get_state() == MealStates.awaiting_item_grams.state
    assert msg.bot.edit_message_text.await_args.kwargs["text"].endswith("вопрос снят.")


async def test_тап_по_уточнению_пишет_и_правит_карточку(rich):
    w = await _world()
    src = await w.meals.add(_meal(w.user.id, _BUCK))
    head = f"iw:{src.id}:{src.items[0].id}"
    await _asked(w, src)
    await _say(w, "2")
    await w.state.update_data(iw_prompt=7)  # уточнение стало текущим вопросом
    stale = await _tap(w, f"{head}:=300@150", message_id=6)  # кнопка старого вопроса
    assert _answers(stale) == ["Этот вопрос уже снят."] and _item(w, "гречка").grams == 150
    cb = await _tap(w, f"{head}:=300@150", message_id=7)
    assert _item(w, "гречка").grams == 300 and await w.state.get_state() is None
    assert _answers(cb) == ["✏️ гречка: было 150 г, стало 300 г"]
    card, trace = cb.bot.edit_message_text.await_args_list
    assert card.kwargs["message_id"] == 5 and trace.kwargs["message_id"] == 7


async def test_кнопки_после_своё_снимают_вопрос(rich):
    w = await _world()
    src = await w.meals.add(_meal(w.user.id, _BUCK))
    await _asked(w, src)
    cb = await _tap(w, f"iw:{src.id}")  # передумал — снова к пресетам
    assert await w.state.get_state() is None
    assert cb.bot.edit_message_text.await_args.kwargs["message_id"] == 6  # не сирота


async def test_вопросы_двух_карточек_не_путаются(rich, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(handlers, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK))
    b = await w.meals.add(_meal(w.user.id, _CHICKEN))
    await _asked(w, a)
    own_b = f"iw:{b.id}:{b.items[0].id}:own"
    cb = await _tap(w, own_b, message_id=8)
    assert cb.bot.edit_message_text.await_args.kwargs == {
        "text": "✍️ Вес «гречка» — вопрос снят.", "chat_id": 1, "message_id": 6,
    }
    assert (await w.state.get_data())["iw_item"] == b.items[0].id
    clock[0] += 1
    again = await _tap(w, own_b, message_id=8)  # двойной тап — один вопрос
    assert _answers(again) == ["Уже спросил — жду число."] and not _sent(again)
    await w.state.update_data(iw_prompt=10)
    cancel_a = await _tap(w, f"iw:{a.id}:{a.items[0].id}:x", message_id=6)
    assert _answers(cancel_a) == ["Этот вопрос уже снят."]
    assert await w.state.get_state() == MealStates.awaiting_item_grams.state  # B жив
    cancel_b = await _tap(w, f"iw:{b.id}:{b.items[0].id}:x", message_id=10)
    assert _answers(cancel_b) == ["Ок, отменил"] and await w.state.get_state() is None
    assert cancel_b.bot.edit_message_text.await_args.kwargs["text"] == (
        "✍️ Вес «курица» — вопрос снят."
    )


# --- без веса, устаревшие, двойной тап (AC-126) ----------------------------------


async def test_позиция_без_веса_и_удалённая_не_пишут(rich):
    w = await _world()
    src = await w.meals.add(_meal(w.user.id, _STATED, _BUCK))
    stated, buck = src.items
    cb = await _tap(w, f"iw:{src.id}:{stated.id}:75")  # «свои цифры»: доли нет
    assert _answers(cb) == [handlers._WEIGHT_GONE] and stated.macros == Macros(210, 10, 8, 24)

    await w.assistant.remove_item(w.user, src.id, buck.id)  # убрали в аппке
    cb = await _tap(w, f"iw:{src.id}:{buck.id}:75")
    assert _answers(cb) == ["Этой позиции уже нет — вес не поправил."]
    _, html, kb = rich.await_args.args  # карточка перерисована из живых данных
    assert "гречка" not in html and "✏️ Вес" not in _texts(kb)

    await w.assistant.delete_meal(w.user, src.id)
    assert _answers(await _tap(w, f"iw:{src.id}:{buck.id}:75")) == [handlers._GONE_CARD]
    for bad in ("iw:x", "iw", "iw:1:2:3:4", f"iw:{src.id}:²", f"iw:{src.id}:back:1"):
        assert _answers(await _tap(w, bad)) == ["Кнопка устарела."]


async def test_кривые_граммы_в_callback_не_пишут(rich):
    w = await _world()
    src = await w.meals.add(_meal(w.user.id, _BUCK))
    for g in ("nan@150", "inf@150", "0@150", "2001@150", "abc", "75", "75@0", "75@nan"):
        cb = await _tap(w, f"iw:{src.id}:{src.items[0].id}:{g}")
        assert _answers(cb) == ["Кнопка устарела."]
    assert _item(w, "гречка").grams == 150


async def test_устаревшие_пресеты_не_пишут(rich):
    w = await _world()
    src = await w.meals.add(_meal(w.user.id, _BUCK))
    half = _data(item_weight_keyboard(src, src.items[0]))[0]
    await w.assistant.adjust_item(w.user, src.id, src.items[0].id, 200)  # поправили в аппке
    cb = await _tap(w, half)
    assert _item(w, "гречка").grams == 200 and not rich.await_count
    assert _answers(cb) == ["Вес уже поменялся — выбери заново."]
    assert _texts(_swapped(cb))[:3] == ["½ · 100 г", "×1,5 · 300 г", "×2 · 400 г"]


async def test_основа_в_кнопке_ловит_совпавшее_число(rich):
    w = await _world()
    src = await w.meals.add(_meal(w.user.id, _BUCK))
    double = _data(item_weight_keyboard(src, src.items[0]))[2]
    assert double.endswith(":300@150")
    await w.assistant.adjust_item(w.user, src.id, src.items[0].id, 600)  # в аппке: 600 г
    cb = await _tap(w, double)  # 300 — это ½ от 600, но кнопка считала ×2 от 150
    assert _item(w, "гречка").grams == 600 and not rich.await_count
    assert _answers(cb) == ["Вес уже поменялся — выбери заново."]
    assert _texts(_swapped(cb))[:2] == ["½ · 300 г", "×1,5 · 900 г"]


async def test_уточнение_после_смены_веса_не_пишет(rich):
    w = await _world()
    src = await w.meals.add(_meal(w.user.id, _BUCK))
    await _asked(w, src)
    await _say(w, "2")
    await w.state.update_data(iw_prompt=7)
    await w.assistant.adjust_item(w.user, src.id, src.items[0].id, 600)
    cb = await _tap(w, f"iw:{src.id}:{src.items[0].id}:=300@150", message_id=7)
    assert _item(w, "гречка").grams == 600
    assert _answers(cb) == ["Вес уже поменялся — выбери заново."]
    assert cb.bot.edit_message_text.await_args.kwargs["text"] == (
        "✍️ Вес «гречка» уже поменялся — сейчас 600 г. Пришли число заново."
    )
    assert await w.state.get_state() == MealStates.awaiting_item_grams.state  # вопрос остаётся


@pytest.mark.parametrize(("text", "grams"), [
    ("x2", 300), ("×2", 300), ("х2", 300), ("Х2", 300), ("*2", 300), ("2x", 300), ("½", 75),
    ("x1.5", 225), ("x 1,5", 225), ("~180", 180), ("≈ 180", 180), ("180 мл", 180),
    ("0,5 л", 500), ("~180 г", 180),
])
async def test_множитель_и_мл_пишут_вес(rich, monkeypatch, text, grams):
    w = await _world()
    src = await w.meals.add(_meal(w.user.id, _BUCK))
    await _asked(w, src)
    usual = AsyncMock()
    monkeypatch.setattr(handlers, "on_text", usual)
    await _say(w, text)
    assert not usual.await_count  # ответ на вопрос, а не новая запись (AC-132)
    assert _item(w, "гречка").grams == grams and await w.state.get_state() is None


async def test_множитель_вне_границ_переспрашивает(rich):
    w = await _world()
    src = await w.meals.add(_meal(w.user.id, _BUCK))
    await _asked(w, src)
    msg = await _say(w, "x20")  # 3000 г
    assert _sent(msg) == [handlers._GRAMS_BOUNDS] and _item(w, "гречка").grams == 150
    assert await w.state.get_state() == MealStates.awaiting_item_grams.state


async def test_двойной_тап_правит_один_раз(rich, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(handlers, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    w = await _world()
    src = await w.meals.add(_meal(w.user.id, _BUCK))
    data = f"iw:{src.id}:{src.items[0].id}:75@150"
    await _tap(w, data)
    clock[0] += 1
    shown = rich.await_count
    second = await _tap(w, data)
    assert _answers(second) == ["Уже поправил."] and rich.await_count == shown
    assert _item(w, "гречка").grams == 75  # граммы на кнопке абсолютные — не 37,5


async def test_параллельный_второй_тап_слышит_уже_правлю(rich, monkeypatch):
    w = await _world()
    src = await w.meals.add(_meal(w.user.id, _BUCK))
    real = w.assistant.adjust_item
    calls = []

    async def slow(*a, **kw):
        calls.append(a)
        await asyncio.sleep(0.01)
        return await real(*a, **kw)

    monkeypatch.setattr(w.assistant, "adjust_item", slow)
    data = f"iw:{src.id}:{src.items[0].id}:225@150"
    first, second = _callback(data), _callback(data)
    await asyncio.gather(
        handlers.on_item_weight(first, w.user, w.assistant, AsyncMock(), w.state),
        handlers.on_item_weight(second, w.user, w.assistant, AsyncMock(), w.state),
    )
    assert len(calls) == 1 and _answers(second) == ["Уже правлю…"]
    assert _item(w, "гречка").grams == 225


# --- и «⏪ Вернуть» (AC-127) ---------------------------------------------------


async def test_вернуть_после_правки_веса_отказывает(rich):
    w = await _world()
    src = await w.meals.add(_meal(w.user.id, _CHICKEN, _BUCK))
    await handlers.on_item_del(
        _callback(f"item_del:{src.id}:{src.items[1].id}"), w.user, w.assistant, AsyncMock()
    )
    kb = rich.await_args.args[2]
    back = kb.inline_keyboard[0][0]
    assert back.text == "⏪ Вернуть: гречка" and _texts(kb)[2] == "✏️ Вес"

    cb = await _tap(w, f"iw:{src.id}")  # курица теперь одна — сразу пресеты
    await _tap(w, _data(_swapped(cb))[0])
    assert _item(w, "курица").grams == 60
    assert not any(t.startswith("⏪") for t in _texts(rich.await_args.args[2]))  # ⏪ не висит
    cb = await _tap(w, f"iw:{src.id}")  # ✏️ Вес → ← Назад: ⏪ не возвращается (ревью)
    kb = _swapped(await _tap(w, _data(_swapped(cb))[-1]))
    assert _texts(kb)[:2] == ["🔁 Повторить", "✏️ Вес"] and not any("⏪" in t for t in _texts(kb))
    await handlers._redraw_card(_callback("x"), w.user, w.assistant, src.id)  # «Уже убрано.»
    assert not any("⏪" in t for t in _texts(rich.await_args.args[2]))

    cb = _callback(back.callback_data)
    await handlers.on_restore(cb, w.user, w.assistant, AsyncMock())
    assert _answers(cb) == ["Приём уже поправили — вернуть нельзя."]
    [meal] = w.meals._meals
    assert [(i.name_snapshot, i.grams) for i in meal.items] == [("курица", 60)]  # правка цела


async def test_вернуть_на_экране_дня_после_правки_веса_не_показывается(rich):
    w = await _world()
    src = await w.meals.add(_meal(w.user.id, _CHICKEN, _BUCK))
    await handlers.on_day_del(
        _callback(f"day_del:0:{src.id}:{src.items[1].id}"), w.user, w.assistant, AsyncMock()
    )
    day = (await w.assistant.day_view(w.user, 0)).day
    meals = (await w.assistant.day_view(w.user, 0)).meals
    assert handlers._day_restore(w.user, day, meals) is not None
    await w.assistant.adjust_item(w.user, src.id, src.items[0].id, 60)  # ✏️ Вес / аппка
    assert handlers._day_restore(w.user, day, meals) is None  # отказала бы — не показываем
