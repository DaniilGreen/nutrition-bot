"""«Поправить словами» ответом на карточку приёма в боте (FR-44, AC-133…AC-141, D-MA-26).

Держим: ответ правит ТОТ приём, на карточку которого ответили (не самый свежий); приём узнаём
по кнопкам карточки; замена — тем же `fix_item`, что «поправить словами» аппки, вес —
`adjust_item`, удаление — `remove_item` с «⏪ Вернуть»; из нескольких позиций непонятно какую —
вопрос кнопками, тап без второго вызова модели; «ещё яблоко» — новая запись со строкой; карточка
правится на месте «✏️ Поправил», не принял Telegram — строка с кнопками, нового «✅ Записал» нет;
висящий «⏪ Вернуть» после правки отказывает; ответ при вопросе «✍️ Своё» — правка, а не число.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message
from aiogram.types import User as TgUser

from app.application.assistant import Assistant
from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.entities import Meal, MealItem
from app.application.meal_service import MealService, _name_matches, items_named
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.bot import handlers
from app.bot.keyboards import (
    card_meal_id,
    fix_pick_keyboard,
    item_pick_keyboard,
    item_weight_keyboard,
    meal_actions_keyboard,
    undo_picker_keyboard,
)
from app.bot.states import MealStates
from app.domain.goals import Goal
from app.domain.value_objects import Macros
from tests.fakes import InMemoryMealRepo, InMemoryProductRepo, InMemoryUserRepo, InMemoryWeightRepo

_NEW = "➕ Это новая еда, а не правка — записал отдельно."


class _Nlu:
    """Сценарные ответы по порядку; считает вызовы — тап по вопросу не должен звать модель."""

    def __init__(self) -> None:
        self.queue: list[ParsedMessage | list[ParsedMessage]] = []
        self.texts: list[str] = []

    async def parse(self, text, known_keys):
        raise AssertionError("бот зовёт parse_samples")

    @property
    def calls(self) -> int:
        return len(self.texts)

    async def parse_samples(self, text, known_keys, *, force=False):
        self.texts.append(text)
        nxt = self.queue.pop(0)
        return nxt if isinstance(nxt, list) else [nxt]  # список — k сэмплов (INV-12)


@pytest.fixture(autouse=True)
def _fresh():
    handlers._taps.clear()
    handlers._trash.clear()
    yield
    handlers._taps.clear()
    handlers._trash.clear()


async def _world():
    users, products, meals = InMemoryUserRepo(), InMemoryProductRepo(), InMemoryMealRepo()
    stats = StatsService(meals, users, Goal(2000, 150, 65, 250))
    nlu, weights = _Nlu(), InMemoryWeightRepo()
    service = MealService(products, meals)
    assistant = Assistant(nlu, products, service, WeightService(weights), stats)
    user = await users.get_or_create(telegram_id=1, name="T", locale="ru", timezone="UTC")
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    return SimpleNamespace(
        assistant=assistant, user=user, meals=meals, state=state, nlu=nlu, weights=weights,
    )


_BUCK = MealItem(name_snapshot="гречка", grams=150, macros=Macros(300, 20, 10, 30), product_id=3)
_CHICKEN = MealItem(name_snapshot="курица", grams=120, macros=Macros(240, 36, 9.6, 0), product_id=4)
_OMELET = MealItem(name_snapshot="омлет", grams=200, macros=Macros(300, 20, 22, 4), product_id=5)
_RICE = MealItem(name_snapshot="рис", grams=200, macros=Macros(260, 5, 1, 56), product_id=6)


def _meal(uid: int, *items: MealItem, ago: int = 1) -> Meal:
    its = [MealItem(name_snapshot=i.name_snapshot, grams=i.grams, macros=i.macros,
                    product_id=i.product_id) for i in items]
    return Meal(
        user_id=uid, eaten_at=datetime.now(UTC) - timedelta(minutes=ago), input_type="text",
        raw_input="—", items=its, source_confidence=0.9,
        totals=Macros(*(sum(getattr(i.macros, f) for i in its)
                        for f in ("kcal", "protein", "fat", "carb"))),
    )


def _food(name: str, grams: float | None = None, kcal: float = 340) -> ParsedItem:
    return ParsedItem(
        name=name, canonical_key=name, unit=Unit.GRAM if grams else Unit.UNKNOWN, grams=grams,
        kcal_per_100g=kcal, protein_per_100g=12 if kcal else 0, fat_per_100g=1.3 if kcal else 0,
        carb_per_100g=70 if kcal else 0,
    )


def _eat(*items: ParsedItem) -> ParsedMessage:
    return ParsedMessage(intent=Intent.MEAL, items=list(items))


def _fix(op: str, *items: ParsedItem) -> ParsedMessage:
    return ParsedMessage(intent=Intent.CORRECTION, correction_op=op, items=list(items))


def _reply(text: str, markup: InlineKeyboardMarkup | None, card_id: int = 5) -> Message:
    chat = Chat.model_construct(id=1, type="private")
    card = Message.model_construct(message_id=card_id, date=None, chat=chat, reply_markup=markup)
    return Message.model_construct(
        message_id=9, date=None, chat=chat, text=text, reply_to_message=card,
        from_user=TgUser.model_construct(id=1, is_bot=False, first_name="T"),
    ).as_(AsyncMock())


def _plain(text: str) -> Message:
    """Обычное сообщение, не ответ."""
    return Message.model_construct(
        message_id=9, date=None, chat=Chat.model_construct(id=1, type="private"), text=text,
        from_user=TgUser.model_construct(id=1, is_bot=False, first_name="T"),
    ).as_(AsyncMock())


def _callback(data: str, message_id: int = 7) -> CallbackQuery:
    bot = AsyncMock()
    msg = Message.model_construct(
        message_id=message_id, date=None, chat=Chat.model_construct(id=1, type="private"),
    ).as_(bot)
    cb = CallbackQuery.model_construct(
        id="1", from_user=TgUser.model_construct(id=1, is_bot=False, first_name="T"),
        chat_instance="ci", data=data, message=msg,
    )
    return cb.as_(bot)


async def _say(w, msg: Message, session=None) -> Message:
    await handlers.on_text(msg, msg.bot, w.user, w.assistant, session or AsyncMock(), w.state)
    return msg


async def _tap(w, data: str, message_id: int = 7) -> CallbackQuery:
    cb = _callback(data, message_id)
    await handlers.on_fix_pick(cb, w.user, w.assistant, AsyncMock(), w.state)
    return cb


def _sent(obj) -> list[SendMessage]:
    return [c.args[0] for c in obj.bot.await_args_list if isinstance(c.args[0], SendMessage)]


def _answers(cb: CallbackQuery) -> list[str]:
    return [getattr(c.args[0], "text", None) or "" for c in cb.bot.await_args_list
            if type(c.args[0]).__name__ == "AnswerCallbackQuery"]


def _edits(obj) -> list[dict]:
    return [c.kwargs for c in obj.bot.edit_message_text.await_args_list]


def _texts(kb: InlineKeyboardMarkup | None) -> list[str]:
    return [b.text for row in (kb.inline_keyboard if kb else []) for b in row]


def _data(kb: InlineKeyboardMarkup) -> list[str]:
    return [b.callback_data or "" for row in kb.inline_keyboard for b in row]


def _items(meal: Meal) -> list[tuple[str, float]]:
    return [(i.name_snapshot, i.grams) for i in meal.items]


# --- карточку узнаём по её кнопкам (D-MA-26) ------------------------------------------------


async def test_карточка_узнаётся_по_кнопкам_а_прочее_нет():
    w = await _world()
    one = await w.meals.add(_meal(w.user.id, _BUCK))
    two = await w.meals.add(_meal(w.user.id, _CHICKEN, _BUCK))
    assert card_meal_id(meal_actions_keyboard(one)) == one.id
    assert card_meal_id(item_pick_keyboard(two)) == two.id  # открыт ✏️ Вес — всё ещё карточка
    assert card_meal_id(item_weight_keyboard(two, two.items[0])) == two.id
    assert card_meal_id(undo_picker_keyboard([one, two], "UTC")) is None  # пикер — не карточка
    prompt = InlineKeyboardMarkup(inline_keyboard=[handlers._cancel_row(one.id, 1)])
    assert card_meal_id(prompt) is None  # вопрос «✍️ Своё» — не карточка
    assert card_meal_id(fix_pick_keyboard(two, two.items)) is None and card_meal_id(None) is None


def test_имя_целым_словом_со_словоформой():
    assert _name_matches("гречку", "гречка") and _name_matches("курица", "курица гриль")
    assert _name_matches("свёкла", "свекла")  # ё = е
    assert not _name_matches("рис", "рисовая лапша")  # подстрокой склеивалась разная еда
    assert not _name_matches("рис", "ирис") and not _name_matches("курица", "куриная грудка")


async def test_опечатка_целым_словом_вторым_проходом_и_только_однозначная():
    w = await _world()
    service = MealService(InMemoryProductRepo(), w.meals)
    rice = MealItem(name_snapshot="рисовая лапша", grams=200, macros=Macros(300, 6, 1, 66))
    iris = MealItem(name_snapshot="ирис", grams=20, macros=Macros(90, 0, 2, 17))
    await w.meals.add(_meal(w.user.id, _BUCK, rice, iris, ago=30))
    assert await service.remove_item_by_name(w.user, "рис") is None  # ни лапша, ни ирис
    removed = await service.remove_item_by_name(w.user, "гречику")  # глобальный FR-14.1
    assert removed is not None and removed.name_snapshot == "гречка"
    await w.meals.add(_meal(w.user.id, _BUCK))
    adjusted = await service.adjust_item_by_name(w.user, "гречика", 100)
    assert adjusted is not None and adjusted[0].grams == 100

    pair = [MealItem(name_snapshot="макароны", grams=1, macros=Macros(1, 0, 0, 0), id=1),
            MealItem(name_snapshot="макаруны", grams=1, macros=Macros(1, 0, 0, 0), id=2)]
    assert items_named(pair, "макроны") == []  # опечатка на два имени — не гадаем
    assert [i.id for i in items_named(pair[:1], "макроны")] == [1]  # одно имя — опечатка ок


async def test_удали_с_опечаткой_ответом_на_карточку():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK, _CHICKEN))
    w.nlu.queue = [_fix("delete", _food("гречика", kcal=0))]
    await _say(w, _reply("удали гречику", meal_actions_keyboard(a)))
    assert _items(a) == [("курица", 120)]


# --- ответ правит ТУ карточку (AC-133, AC-134) ----------------------------------------------


async def test_ответ_заменяет_позицию_той_карточки_а_не_свежей():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK, ago=30))
    b = await w.meals.add(_meal(w.user.id, _OMELET))  # свежее
    w.nlu.queue = [_eat(_food("булгур"))]
    session = AsyncMock()
    msg = await _say(w, _reply("это был булгур", meal_actions_keyboard(a)), session)

    assert _items(a) == [("булгур", 150)]  # вес не назван — прежний (тот же fix_item, что аппка)
    assert a.items[0].macros.kcal == 510  # 340 ккал/100 г × 150 г — считает приложение (INV-1)
    assert _items(b) == [("омлет", 200)] and w.nlu.calls == 1
    session.commit.assert_awaited_once()
    [card] = _edits(msg)
    assert card["message_id"] == 5 and card["rich_message"].html.startswith("<h3>✏️ Поправил")
    assert _texts(card["reply_markup"])[:3] == ["⏪ Вернуть: гречка", "🔁 Повторить", "✏️ Вес"]
    [trace] = _sent(msg)
    assert trace.text == "✏️ Поправил: гречка → булгур 150 г · приём теперь 510 ккал"
    assert trace.reply_markup is None  # карточку приняли — кнопки на ней, не на следе
    assert not msg.bot.send_rich_message.await_count  # нового «✅ Записал» нет


async def test_вес_и_удаление_ответом_только_в_этом_приёме():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK, _CHICKEN, ago=30))
    b = await w.meals.add(_meal(w.user.id, _BUCK))  # свежая гречка — FR-14.2 взял бы её
    w.nlu.queue = [_fix("adjust", _food("гречка", 75, kcal=0))]
    msg = await _say(w, _reply("гречки было 75", meal_actions_keyboard(a)))
    assert _items(a) == [("гречка", 75), ("курица", 120)] and _items(b) == [("гречка", 150)]
    assert a.items[0].macros == Macros(150, 10, 5, 15)  # снапшот × ratio (INV-3)
    assert _sent(msg)[0].text == "✏️ гречка: было 150 г, стало 75 г · приём теперь 390 ккал"

    w.nlu.queue = [_fix("adjust", _food("", 100, kcal=0))]  # «не 150, а 100» — имени нет
    await _say(w, _reply("не 150, а 100", meal_actions_keyboard(b)))
    assert _items(b) == [("гречка", 100)]  # позиция с весом одна — она

    w.nlu.queue = [_fix("delete", _food("гречка", kcal=0))]
    msg = await _say(w, _reply("удали гречку", meal_actions_keyboard(a)))
    assert _items(a) == [("курица", 120)] and _items(b) == [("гречка", 100)]
    [card] = _edits(msg)
    assert _texts(card["reply_markup"])[0] == "⏪ Вернуть: гречка"  # как у 🗑 (FR-41)
    assert _sent(msg)[0].text == "🗑 Убрал: гречка · приём теперь 240 ккал"


async def test_удаление_последней_позиции_отменяет_запись_с_вернуть():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK))
    w.nlu.queue = [_fix("delete", _food("гречка", kcal=0))]
    msg = await _say(w, _reply("удали гречку", meal_actions_keyboard(a)))
    assert w.meals._meals == []
    [card] = _edits(msg)
    assert card["rich_message"].html.startswith("<h3>🗑 Запись отменена")
    back = card["reply_markup"].inline_keyboard[0][0]
    assert _sent(msg)[0].text == "🗑 Убрал: гречка · запись отменена"
    cb = _callback(back.callback_data, message_id=5)
    await handlers.on_restore(cb, w.user, w.assistant, AsyncMock())
    assert _answers(cb) == ["⏪ Вернул"] and _items(w.meals._meals[0]) == [("гречка", 150)]


async def test_замени_x_на_y_первое_совпадение_x_остальное_y():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _RICE, _CHICKEN))
    w.nlu.queue = [
        _fix("replace", _food("рис", kcal=0), _food("рисовая лапша", kcal=0)),
        _eat(_food("рисовая лапша", kcal=360)),  # Y — заново, путём записи
        _fix("replace", _food("курица", kcal=0), _food("куриная грудка", kcal=0)),
        _eat(_food("куриная грудка", kcal=113)),
    ]
    await _say(w, _reply("замени рис на рисовую лапшу", meal_actions_keyboard(a)))
    await _say(w, _reply("курицу на куриную грудку", meal_actions_keyboard(a)))
    assert _items(a) == [("рисовая лапша", 200), ("куриная грудка", 120)]
    assert w.nlu.texts[1::2] == ["рисовая лапша", "куриная грудка"]


async def test_замена_новым_продуктом_пиннит_медиану_сэмплов_а_не_оценку_правки():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK))
    w.nlu.queue = [
        _fix("replace", _food("гречка", kcal=0), _food("булгур", kcal=0)),  # ноль правки
        [_eat(_food("булгур", kcal=k)) for k in (300, 342, 420)],  # k > 1 — как у записи
    ]
    await _say(w, _reply("замени гречку на булгур", meal_actions_keyboard(a)))
    assert _items(a) == [("булгур", 150)]
    assert a.items[0].macros.kcal == 513  # медиана 342 ккал/100 г × 150 г (INV-12)

    w.nlu.queue = [_fix("replace", _food("булгур", kcal=0), _food("киноа", kcal=0)),
                   ParsedMessage(intent=Intent.UNKNOWN)]  # Y не разобрался едой — не пиним
    msg = await _say(w, _reply("замени булгур на киноа", meal_actions_keyboard(a)))
    assert _items(a) == [("булгур", 150)]
    assert _sent(msg)[0].text.startswith("Не понял, что поправить. Ответь на карточку:")


@pytest.mark.parametrize(
    "text", ["йогурт без добавок", "кофе с добавкой сиропа", "творог плюс мёд",
             "гречка, ещё горячая"],
)
async def test_добавка_не_в_начале_это_правка(text):
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK))
    w.nlu.queue = [_eat(_food(text))]
    msg = await _say(w, _reply(text, meal_actions_keyboard(a)))
    assert len(w.meals._meals) == 1 and _items(a) == [(text, 150)]  # заменено, не дописано
    assert _NEW not in [m.text for m in _sent(msg)]


async def test_вес_с_чужим_именем_не_правит_единственную():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK))
    w.nlu.queue = [_fix("adjust", _food("курица", 200, kcal=0))]
    msg = await _say(w, _reply("курицы было 200", meal_actions_keyboard(a)))
    assert _items(a) == [("гречка", 150)]
    assert _sent(msg)[0].text.startswith("В этой записи нет «курица».")


async def test_несколько_разобранных_каждая_на_своё_место_или_отказ():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK, _CHICKEN))
    w.nlu.queue = [_eat(_food("гречка", 100), _food("курица", 100, kcal=165))]
    msg = await _say(w, _reply("гречка 100 и курица 100", meal_actions_keyboard(a)))
    assert _items(a) == [("гречка", 100), ("курица", 100)]
    assert _sent(msg)[0].text.startswith("✏️ Поправил: гречка → гречка 100 г; курица → курица")

    w.nlu.queue = [_eat(_food("гречка"), _food("булгур"))]
    msg = await _say(w, _reply("гречка и булгур", meal_actions_keyboard(a)))
    assert _sent(msg)[0].text == "Поправь по одной позиции за раз."
    assert _items(a) == [("гречка", 100), ("курица", 100)]  # одну обеими — не заменяем


# --- какую позицию из нескольких (AC-135) ---------------------------------------------------


async def test_несколько_позиций_спрашивает_и_тап_без_второго_разбора():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK, _CHICKEN))
    w.nlu.queue = [_eat(_food("булгур"))]
    msg = await _say(w, _reply("это был булгур", meal_actions_keyboard(a)))
    [ask] = _sent(msg)
    assert ask.text == "✏️ Что заменить на «булгур»?"
    assert _texts(ask.reply_markup) == ["гречка · 150 г", "курица · 120 г", "✖️ Отмена"]
    assert _items(a) == [("гречка", 150), ("курица", 120)] and not _edits(msg)
    await w.state.update_data(rf_prompt=7)  # id вопроса (мок бота id не выдаёт)

    cb = await _tap(w, _data(ask.reply_markup)[0])
    assert _items(a) == [("булгур", 150), ("курица", 120)] and w.nlu.calls == 1
    assert _answers(cb) == ["✏️ Поправил: гречка → булгур 150 г · приём теперь 750 ккал"]
    card, trace = _edits(cb)
    assert card["message_id"] == 5 and card["rich_message"].html.startswith("<h3>✏️ Поправил")
    assert trace["message_id"] == 7 and trace["text"].startswith("✏️ Поправил: гречка → булгур")
    assert "reply_markup" not in trace  # вопрос стал следом, без кнопок

    w.nlu.queue = [_eat(_food("курица гриль", 200))]  # имя совпало ровно с одной — без вопроса
    await _say(w, _reply("курица гриль 200 г", meal_actions_keyboard(a)))
    assert _items(a) == [("булгур", 150), ("курица гриль", 200)]


async def test_текстом_на_вопрос_позиция_по_имени_без_модели():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK, _CHICKEN))
    w.nlu.queue = [_eat(_food("булгур"))]
    [ask] = _sent(await _say(w, _reply("это был булгур", meal_actions_keyboard(a))))
    await w.state.update_data(rf_prompt=7)
    msg = await _say(w, _plain("гречка"))  # голое имя просто текстом, не ответом
    assert _items(a) == [("булгур", 150), ("курица", 120)] and w.nlu.calls == 1
    card, trace = _edits(msg)
    assert card["message_id"] == 5 and trace["message_id"] == 7  # вопрос стал следом

    w.nlu.queue = [_eat(_food("индейка"))]
    [ask] = _sent(await _say(w, _reply("это была индейка", meal_actions_keyboard(a))))
    await w.state.update_data(rf_prompt=7)
    await _say(w, _reply("курицу", ask.reply_markup, card_id=7))  # ответом на сам вопрос
    assert _items(a) == [("булгур", 150), ("индейка", 120)] and w.nlu.calls == 2


async def test_вопрос_снимается_устаревает_и_пишет_один_раз():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK, _CHICKEN))
    w.nlu.queue = [_eat(_food("булгур")), ParsedMessage(intent=Intent.UNKNOWN)]
    [ask] = _sent(await _say(w, _reply("это был булгур", meal_actions_keyboard(a))))
    await w.state.update_data(rf_prompt=7)
    msg = await _say(w, _plain("привет"))  # новый текст снимает вопрос
    assert _edits(msg)[0] == {"text": "✏️ Вопрос снят.", "chat_id": 1, "message_id": 7}
    cb = await _tap(w, _data(ask.reply_markup)[0])
    assert _answers(cb) == ["Этот вопрос уже снят."] and _items(a)[0] == ("гречка", 150)

    w.nlu.queue = [_eat(_food("булгур"))]
    [ask] = _sent(await _say(w, _reply("это был булгур", meal_actions_keyboard(a))))
    await w.state.update_data(rf_prompt=7)
    await _tap(w, _data(ask.reply_markup)[0])
    second = await _tap(w, _data(ask.reply_markup)[0])
    assert _answers(second) == ["Этот вопрос уже снят."]
    assert _items(a) == [("булгур", 150), ("курица", 120)]  # заменено один раз

    [ask] = _sent(await _say(w, _reply("100 г", meal_actions_keyboard(a))))  # вес: id тот же
    await w.state.update_data(rf_prompt=7)
    await _tap(w, _data(ask.reply_markup)[1])
    [ask] = _sent(await _say(w, _reply("90 г", meal_actions_keyboard(a))))
    await w.state.update_data(rf_prompt=7)
    cb = await _tap(w, _data(ask.reply_markup)[1])  # тот же rf:<meal>:<item> через миг
    assert _answers(cb) != ["Уже поправил."] and _items(a)[1] == ("курица", 90)

    w.nlu.queue = [_eat(_food("перловка"))]
    [ask] = _sent(await _say(w, _reply("это была перловка", meal_actions_keyboard(a))))
    await w.state.update_data(rf_prompt=7)
    await w.assistant.remove_item(w.user, a.id, a.items[1].id)  # курицу убрали в аппке
    cb = await _tap(w, _data(ask.reply_markup)[1])
    assert _answers(cb) == ["Запись уже поменялась — ответь на карточку заново."]
    assert _items(a) == [("булгур", 150)]

    b = await w.meals.add(_meal(w.user.id, _BUCK, _CHICKEN))
    w.nlu.queue = [_eat(_food("перловка"))]
    [ask] = _sent(await _say(w, _reply("это была перловка", meal_actions_keyboard(b))))
    await w.state.update_data(rf_prompt=7)
    cb = await _tap(w, _data(ask.reply_markup)[-1])
    assert _answers(cb) == ["Ок, отменил"] and _edits(cb)[0]["text"] == "✏️ Ок, не правлю."
    assert _items(b) == [("гречка", 150), ("курица", 120)]


# --- отказы и не-карточки (AC-136) ----------------------------------------------------------


async def test_приём_удалён_и_имя_не_нашлось():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK))
    kb = meal_actions_keyboard(a)
    await w.assistant.delete_meal(w.user, a.id)  # удалили в аппке
    msg = await _say(w, _reply("это был булгур", kb))
    assert _sent(msg)[0].text == "Этой записи уже нет — поправлять нечего."
    assert w.nlu.calls == 0 and w.meals._meals == []  # модель не звали, ничего не записали

    b = await w.meals.add(_meal(w.user.id, _BUCK))
    w.nlu.queue = [_fix("delete", _food("рис", kcal=0))]
    msg = await _say(w, _reply("удали рис", meal_actions_keyboard(b)))
    assert _sent(msg)[0].text.startswith("В этой записи нет «рис». Ответь на карточку:")
    assert _items(b) == [("гречка", 150)]


async def test_ответ_не_на_карточку_идёт_обычным_путём(monkeypatch):
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK))
    seen: list[str] = []

    async def process(user, text, **kw):
        seen.append(text)
        return ParsedMessage(intent=Intent.UNKNOWN)  # любой результат — проверяем маршрут

    monkeypatch.setattr(w.assistant, "process", process)
    monkeypatch.setattr(handlers, "_reply_message", AsyncMock())
    picker = undo_picker_keyboard([a], "UTC")  # пикер с одним приёмом — всё равно не карточка
    await _say(w, _reply("это был булгур", picker))
    await _say(w, _reply("это был булгур", None))  # ответ на своё сообщение / без кнопок
    assert seen == ["это был булгур", "это был булгур"] and _items(a) == [("гречка", 150)]


# --- новая еда, а не правка (AC-137) --------------------------------------------------------


async def test_ещё_яблоко_новая_запись_со_строкой_в_день_карточки():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK, ago=24 * 60 + 5))  # вчерашняя карточка
    w.nlu.queue = [_eat(_food("яблоко", 100, kcal=52))]
    msg = await _say(w, _reply("ещё яблоко 100 г", meal_actions_keyboard(a)))
    assert _items(a) == [("гречка", 150)]
    assert [_items(m) for m in w.meals._meals] == [[("гречка", 150)], [("яблоко", 100)]]
    assert _sent(msg)[0].text == _NEW
    msg.bot.send_rich_message.assert_awaited_once()  # обычная карточка нового приёма
    assert not _edits(msg)  # карточка, на которую ответили, не тронута
    apple = w.meals._meals[1]
    assert w.assistant._stats.days_ago(w.user, apple.eaten_at) == 1  # во вчера, как карточка


async def test_остаток_на_карточке_за_день_приёма():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK, ago=24 * 60 + 5))
    await w.meals.add(_meal(w.user.id, _OMELET))  # сегодня
    w.nlu.queue = [_eat(_food("булгур"))]
    result = await w.assistant.correct_meal(w.user, a.id, "это был булгур")
    assert result.remaining.consumed.kcal == 510  # вчерашний день, без сегодняшнего омлета


# --- карточку не приняли (AC-138) -----------------------------------------------------------


async def test_старая_карточка_строка_с_кнопками_без_новой_записал():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK))
    w.nlu.queue = [_eat(_food("булгур"))]
    msg = _reply("это был булгур", meal_actions_keyboard(a))
    msg.bot.edit_message_text.side_effect = TelegramBadRequest(
        method=EditMessageText(text="x"), message="Bad Request: message can't be edited"
    )
    await _say(w, msg)
    [trace] = _sent(msg)
    assert trace.text.startswith("✏️ Поправил: гречка → булгур 150 г")
    assert _texts(trace.reply_markup)[1:3] == ["🔁 Повторить", "✏️ Вес"]  # первой — ⏪
    assert not msg.bot.send_rich_message.await_count


# --- «⏪ Вернуть» и «✍️ Своё» (AC-139, AC-140) ----------------------------------------------


async def test_вернуть_после_правки_ответом_отказывает(monkeypatch):
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK, _CHICKEN))
    rich = AsyncMock()
    monkeypatch.setattr(handlers, "_rich_edit", rich)
    await handlers.on_item_del(
        _callback(f"item_del:{a.id}:{a.items[1].id}", message_id=5), w.user, w.assistant,
        AsyncMock(),
    )
    back = rich.await_args.args[2].inline_keyboard[0][0]
    assert back.text == "⏪ Вернуть: курица"

    w.nlu.queue = [_eat(_food("булгур"))]  # позиция теперь одна — без вопроса
    msg = await _say(w, _reply("это был булгур", meal_actions_keyboard(a)))
    own = _edits(msg)[0]["reply_markup"].inline_keyboard[0][0]
    assert own.text == "⏪ Вернуть: гречка"  # своя ⏪ замены; ⏪ курицы с карточки ушла
    cb = _callback(back.callback_data, message_id=5)
    await handlers.on_restore(cb, w.user, w.assistant, AsyncMock())
    assert _answers(cb)[0].startswith("Вернуть")  # висящий снимок отказывает
    assert _items(a) == [("булгур", 150)]  # замена цела

    await _say(w, _reply("100", meal_actions_keyboard(a)))  # вес после замены — отпечаток другой
    cb = _callback(own.callback_data, message_id=5)
    await handlers.on_restore(cb, w.user, w.assistant, AsyncMock())
    assert _answers(cb) == ["Приём уже поправили — вернуть нельзя."]


async def test_ответ_при_вопросе_своё_правит_а_ответ_на_вопрос_пишет_вес():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK))
    own = _callback(f"iw:{a.id}:{a.items[0].id}:own", message_id=5)
    await handlers.on_item_weight(own, w.user, w.assistant, AsyncMock(), w.state)
    assert await w.state.get_state() == MealStates.awaiting_item_grams.state
    await w.state.update_data(iw_prompt=6)

    w.nlu.queue = [_eat(_food("булгур"))]
    msg = _reply("это был булгур", meal_actions_keyboard(a))
    await handlers.on_item_grams_input(msg, msg.bot, w.user, w.assistant, AsyncMock(), w.state)
    assert await w.state.get_state() is None
    assert _edits(msg)[0] == {"text": "✍️ Вес «гречка» — вопрос снят.", "chat_id": 1,
                              "message_id": 6}
    assert _items(a) == [("булгур", 150)]  # правка ответом, а не «не вес» → новая запись

    b = await w.meals.add(_meal(w.user.id, _CHICKEN))
    handlers._taps.clear()  # тот же `own` через миг — страж двойного тапа его бы не пустил
    await handlers.on_item_weight(
        _callback(f"iw:{a.id}:{a.items[0].id}:own", message_id=5), w.user, w.assistant,
        AsyncMock(), w.state,
    )
    msg = _reply("200", meal_actions_keyboard(b), card_id=8)  # число — но ответом на карточку B
    await handlers.on_item_grams_input(msg, msg.bot, w.user, w.assistant, AsyncMock(), w.state)
    assert _items(b) == [("курица", 200)] and _items(a) == [("булгур", 150)]  # не в вопрос A
    assert await w.state.get_state() is None

    handlers._taps.clear()
    await handlers.on_item_weight(
        _callback(f"iw:{a.id}:{a.items[0].id}:own", message_id=5), w.user, w.assistant,
        AsyncMock(), w.state,
    )
    assert await w.state.get_state() == MealStates.awaiting_item_grams.state
    await w.state.update_data(iw_prompt=6)
    prompt = InlineKeyboardMarkup(inline_keyboard=[handlers._cancel_row(a.id, a.items[0].id)])
    msg = _reply("75", prompt, card_id=6)  # ответ на сам вопрос — это ответ на вопрос
    await handlers.on_item_grams_input(msg, msg.bot, w.user, w.assistant, AsyncMock(), w.state)
    assert _items(a) == [("булгур", 75)] and w.nlu.calls == 1  # числа — без модели


async def test_голое_число_ответом_вес_без_модели():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK))
    msg = await _say(w, _reply("200", meal_actions_keyboard(a)))
    assert _items(a) == [("гречка", 200)] and w.nlu.calls == 0
    assert _sent(msg)[0].text == "✏️ гречка: было 150 г, стало 200 г · приём теперь 400 ккал"
    await _say(w, _reply("x2", meal_actions_keyboard(a)))
    assert _items(a) == [("гречка", 400)]
    msg = await _say(w, _reply("2", meal_actions_keyboard(a)))
    assert _sent(msg)[0].text.startswith("«2» — это граммы или порции?")
    msg = await _say(w, _reply("3000", meal_actions_keyboard(a)))
    assert _sent(msg)[0].text.startswith("Вес — от 1 до 2000 г") and _items(a)[0][1] == 400

    two = await w.meals.add(_meal(w.user.id, _BUCK, _CHICKEN))
    [ask] = _sent(await _say(w, _reply("150 г", meal_actions_keyboard(two))))
    assert ask.text == "✏️ Чей вес «150 г»? Выбери позицию."
    assert _texts(ask.reply_markup) == ["гречка · 150 г", "курица · 120 г", "✖️ Отмена"]
    await w.state.update_data(rf_prompt=7)
    await _tap(w, _data(ask.reply_markup)[1])
    assert _items(two) == [("гречка", 150), ("курица", 150)] and w.nlu.calls == 0


async def test_вес_тела_ответом_на_карточку_только_со_словом():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK))
    w.nlu.queue = [ParsedMessage(intent=Intent.WEIGHT, weight_kg=80)]
    msg = await _say(w, _reply("утром 80", meal_actions_keyboard(a)))
    assert w.weights._logs == [] and _sent(msg)[0].text.startswith("Не понял, что поправить.")
    w.nlu.queue = [ParsedMessage(intent=Intent.WEIGHT, weight_kg=80.5)]
    await _say(w, _reply("вес 80,5", meal_actions_keyboard(a)))
    w.nlu.queue = [ParsedMessage(intent=Intent.WEIGHT, weight_kg=72.5)]
    await _say(w, _reply("72.5 кг", meal_actions_keyboard(a)))  # «кг» — не 72 500 г гречки
    assert [log.weight_kg for log in w.weights._logs] == [80.5, 72.5]
    assert _items(a) == [("гречка", 150)] and w.nlu.calls == 3


async def test_запись_с_числом_при_вопросе_не_выбор_а_запись():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK, _CHICKEN))
    w.nlu.queue = [_eat(_food("булгур")), _eat(_food("гречка", 200))]
    await _say(w, _reply("это был булгур", meal_actions_keyboard(a)))
    await w.state.update_data(rf_prompt=7)
    msg = await _say(w, _plain("гречка 200 г"))  # обычная запись, не ответ-имя
    assert _items(a) == [("гречка", 150), ("курица", 120)]  # черновик «булгур» не применён
    assert _edits(msg)[0]["text"] == "✏️ Вопрос снят."
    assert [_items(m) for m in w.meals._meals][1] == [("гречка", 200)]  # записано обычным путём


async def test_аппке_fix_item_отдаёт_остаток_сегодня():
    w = await _world()
    a = await w.meals.add(_meal(w.user.id, _BUCK, ago=24 * 60 + 5))  # вчерашний приём
    await w.meals.add(_meal(w.user.id, _OMELET))  # сегодня
    w.nlu.queue = [_eat(_food("булгур"))]
    logged = await w.assistant.fix_item(w.user, a.id, a.items[0].id, "это был булгур")
    assert logged is not None and logged.remaining.consumed.kcal == 300  # сегодняшний омлет
