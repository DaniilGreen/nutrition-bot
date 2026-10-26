"""Фото блюда в боте (FR-34, MINIAPP-SPEC §6.22 «Бот»).

Держим три вещи: без ответа про масло запись не уходит, вес тарелки
пересчитывает граммы пропорционально, в разбор уходит фраза из черновика.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TgUser

from app.application.assistant import Assistant
from app.application.dto import Intent, ParsedItem, ParsedMessage, PlateReading, Unit
from app.application.meal_service import MealService
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.bot import handlers
from app.bot.formatters import format_photo_card
from app.bot.keyboards import photo_keyboard
from app.bot.states import MealStates
from app.domain.goals import Goal
from app.domain.plate import Component
from tests.fakes import (
    InMemoryMealRepo,
    InMemoryProductRepo,
    InMemoryUserRepo,
    InMemoryWeightRepo,
    StubVision,
)


class _State:
    def __init__(self, data: dict | None = None, current: str | None = None) -> None:
        self.data = dict(data or {})
        self.current = current
        self.cleared = False

    async def get_data(self) -> dict:
        return dict(self.data)

    async def update_data(self, **kw) -> None:
        self.data.update(kw)

    async def set_state(self, st) -> None:
        self.current = st.state if hasattr(st, "state") else st

    async def get_state(self):
        return self.current

    async def clear(self) -> None:
        self.cleared = True
        self.data = {}
        self.current = None


def _plate(**kw) -> PlateReading:
    base = dict(kind="dish", dish="паста с курицей", components=[
        Component("макароны отварные", 0.6, 250), Component("курица жареная", 0.3, 100),
    ], fat_kind="масло сливочное")
    base.update(kw)
    return PlateReading(**base)


def _data(**kw) -> dict:
    d = {"plate": _plate(), "grams": [250.0, 100.0], "weighed": False,
         "off": [], "card_id": 5}
    d.update(kw)
    return d


def _callback(data: str, message_id: int = 5, *, mount: bool = False) -> CallbackQuery:
    bot = AsyncMock()
    msg = Message.model_construct(
        message_id=message_id, date=None, chat=Chat.model_construct(id=1, type="private")
    )
    if mount:  # ответы в чат от имени вопроса (message.answer) — тоже через этот бот
        msg = msg.as_(bot)
    cb = CallbackQuery.model_construct(
        id="1", from_user=TgUser.model_construct(id=1, is_bot=False, first_name="T"),
        chat_instance="ci", data=data, message=msg,
    )
    return cb.as_(bot)


def _assistant() -> AsyncMock:
    a = AsyncMock()
    a.photo_text = Assistant.photo_text
    return a


def test_карточка_без_веса_просит_взвесить_и_не_спрашивает_масло():
    """D-MA-20: вопросов про масло и заправку нет — масло, если было, уже строка списка."""
    text = format_photo_card(_plate(), [250, 100], weighed=False)
    assert "вес на глаз" in text and "Всего 350 г" in text
    assert "пришли вес тарелки" in text.lower()
    assert "Масло при готовке" not in text and "Заправка" not in text


def test_убранная_позиция_зачёркнута_и_не_в_итоге():
    plate = _plate(components=[Component("бургер", 0.7, 250),
                               Component("картофель фри", 0.3, 120),
                               Component("масло подсолнечное", 0, 20, True, "на готовку")])
    text = format_photo_card(plate, [250, 120, 20], weighed=False, off=[1])
    assert "<s>картофель фри — 120 г</s>" in text and "Всего 270 г" in text
    assert "масло подсолнечное — 20 г · на готовку" in text


def test_карточка_экранирует_имена():
    plate = _plate(dish="<b>x</b>", components=[Component("a<i>", 1, 10)])
    text = format_photo_card(plate, [10], weighed=True)
    assert "<i>" not in text and "&lt;b&gt;x" in text
    assert "Взвешивал" not in text


def test_клавиатура_убрать_вернуть_и_влезает_в_лимит_callback():
    kb = photo_keyboard(_plate().components, [1])
    buttons = [b for row in kb.inline_keyboard for b in row]
    assert [b.text for b in buttons[:2]] == ["✖ макароны отварные", "↩ курица жареная"]
    assert all(len((b.callback_data or "").encode()) <= 64 for b in buttons)
    assert buttons[-2].text == "✅ Записать"


async def test_всё_убрано_не_пишет():
    state, assistant = _State(_data(off=[0, 1])), _assistant()
    cb = _callback("ph:ok")
    await handlers.on_photo_btn(cb, object(), assistant, AsyncMock(), state)
    assistant.log_photo.assert_not_awaited()
    method = cb.bot.await_args.args[0]          # AnswerCallbackQuery
    assert method.show_alert is True and "нечего" in method.text.lower()
    assert not state.cleared


async def test_записать_отдаёт_фразу_из_черновика(monkeypatch):
    render = AsyncMock()
    monkeypatch.setattr(handlers, "_render_after_continue", render)
    state, assistant = _State(_data(weighed=True)), _assistant()
    await handlers.on_photo_btn(_callback("ph:ok"), object(), assistant, AsyncMock(), state)
    args = assistant.log_photo.await_args
    assert args.args[1] == "макароны отварные 250 г, курица жареная 100 г"
    assert args.kwargs["weighed"] is True
    assert state.cleared
    render.assert_awaited_once()


async def test_убрать_позицию_до_записи_и_записать_без_неё(monkeypatch):
    """24.10.2026: картофель фри, которого не ел, убирается в карточке, а не правкой после."""
    edit = AsyncMock()
    monkeypatch.setattr(handlers, "_safe_edit", edit)
    monkeypatch.setattr(handlers, "_render_after_continue", AsyncMock())
    state, assistant = _State(_data()), _assistant()
    await handlers.on_photo_btn(_callback("ph:off:1"), object(), assistant, AsyncMock(), state)
    assert state.data["off"] == [1]
    assert "<s>курица жареная — 100 г</s>" in edit.await_args.args[1]
    await handlers.on_photo_btn(_callback("ph:ok"), object(), assistant, AsyncMock(), state)
    assert assistant.log_photo.await_args.args[1] == "макароны отварные 250 г"


async def test_повторный_тап_возвращает_позицию(monkeypatch):
    monkeypatch.setattr(handlers, "_safe_edit", AsyncMock())
    state = _State(_data(off=[1]))
    await handlers.on_photo_btn(_callback("ph:off:1"), object(), _assistant(), AsyncMock(), state)
    assert state.data["off"] == []


async def test_вес_тарелки_пересчитывает_пропорционально():
    state = _State(_data(), MealStates.awaiting_photo.state)
    msg, bot = AsyncMock(), AsyncMock()
    msg.text = "тарелка 700"
    msg.chat = Chat.model_construct(id=1, type="private")
    await handlers.on_photo_weight(msg, bot, state)
    assert state.data["grams"] == [500.0, 200.0]
    assert state.data["weighed"] is True
    assert "взвешено" in bot.edit_message_text.await_args.args[0]


@pytest.mark.parametrize("text", ["0", "99999", "тарелка 5"])
async def test_вес_тарелки_вне_границ_получает_подсказку(text):
    state = _State(_data(), MealStates.awaiting_photo.state)
    msg, bot = _msg(text), AsyncMock()
    await handlers.on_photo_weight(msg, bot, state)
    assert state.data["grams"] == [250.0, 100.0]
    assert "числом" in msg.answer.await_args.args[0]


async def test_устаревшая_карточка():
    state, cb = _State({}), _callback("ph:ok")
    assistant = _assistant()
    await handlers.on_photo_btn(cb, object(), assistant, AsyncMock(), state)
    assistant.log_photo.assert_not_awaited()
    assert "устарела" in cb.bot.await_args.args[0].text


async def test_новое_фото_снимает_открытую_карточку():
    from tests.unit.test_flow_guard import _State as Guard
    msg, st = AsyncMock(), Guard(MealStates.awaiting_photo.state)
    supersedes = (MealStates.awaiting_label_grams.state, MealStates.awaiting_photo.state)
    assert await handlers._busy_with_flow(msg, st, supersedes=supersedes) is False
    assert st.cleared


async def test_сбой_разбора_не_теряет_карточку():
    state, assistant = _State(_data()), _assistant()
    assistant.log_photo.side_effect = RuntimeError("openai down")
    with pytest.raises(RuntimeError):
        await handlers.on_photo_btn(_callback("ph:ok"), object(), assistant, AsyncMock(), state)
    assert not state.cleared and state.data["plate"] is not None


async def test_вес_тарелки_не_двигает_названный_вес_позиции():
    plate = _plate(components=[Component("макароны", 0.6, 250),
                               Component("курица", 0.3, 150, fixed=True)])
    state = _State(_data(plate=plate, grams=[270.0, 150.0], weighed=True),
                   MealStates.awaiting_photo.state)
    msg, bot = AsyncMock(), AsyncMock()
    msg.text = "450"
    msg.chat = Chat.model_construct(id=1, type="private")
    await handlers.on_photo_weight(msg, bot, state)
    assert state.data["grams"] == [300.0, 150.0]


# --- FR-34.1: правка карточки словами, без модели (D-MA-30) ---------------------------


def _msg(text: str, reply_to: int | None = None) -> AsyncMock:
    msg = AsyncMock()
    msg.text = text
    msg.chat = Chat.model_construct(id=1, type="private")
    msg.answer.return_value = Message.model_construct(message_id=77, date=None, chat=msg.chat)
    msg.reply_to_message = (
        None if reply_to is None
        else Message.model_construct(message_id=reply_to, date=None,
                                     chat=Chat.model_construct(id=1, type="private"))
    )
    return msg


async def _say(text: str, data: dict | None = None, reply_to: int | None = None):
    state = _State(data or _data(), MealStates.awaiting_photo.state)
    msg, bot = _msg(text, reply_to), AsyncMock()
    await handlers.on_photo_weight(msg, bot, state)
    return state, msg, bot


def _card(bot: AsyncMock) -> str:
    return bot.edit_message_text.await_args.args[0]


@pytest.mark.parametrize("text", ["курица 150", "курица 150 г", "курицы было 150",
                                  "150 г курицы", "Курица жареная 150г"])
async def test_вес_позиции_словами(text):
    """AC-234: вес позиции — без модели; позиция fixed, прочие не тронуты (тарелка без веса)."""
    state, msg, bot = await _say(text)
    assert state.data["grams"] == [250.0, 150.0]
    assert state.data["plate"].components[1].fixed
    assert "курица жареная — 150 г" in _card(bot)
    assert state.current == MealStates.awaiting_photo.state


async def test_вес_позиции_на_взвешенной_тарелке_делит_остаток():
    """AC-234: тарелка 400 уже названа — остаток 400 − 150 снова делится между прочими."""
    data = _data(grams=[280.0, 120.0], weighed=True, total=400.0)
    state, _, bot = await _say("курица 150", data)
    assert state.data["grams"] == [250.0, 150.0]
    assert "взвешено" in _card(bot)


async def test_вес_позиции_вне_границ_ничего_не_меняет():
    state, msg, bot = await _say("курица 5000")
    assert state.data["grams"] == [250.0, 100.0]
    assert "от 1 до 3000" in msg.answer.await_args.args[0]
    bot.edit_message_text.assert_not_awaited()


async def test_голое_малое_число_у_позиции_переспрашивает_единицу():
    state, msg, _ = await _say("курица 2")
    assert state.data["grams"] == [250.0, 100.0]
    assert "«курица 2 г»" in msg.answer.await_args.args[0]


@pytest.mark.parametrize("text", ["без курицы", "убери курицу", "курицу не ел", "удали курицу"])
async def test_убрать_словами(text):
    """AC-235: то же, что «✖»: зачёркнута и не в «Всего»."""
    state, _, bot = await _say(text)
    assert state.data["off"] == [1]
    assert "<s>курица жареная — 100 г</s>" in _card(bot) and "Всего 250 г" in _card(bot)


async def test_вернуть_словами_и_убрать_две():
    state, _, _ = await _say("верни курицу", _data(off=[1]))
    assert state.data["off"] == []
    state, _, _ = await _say("без макарон и курицы")
    assert sorted(state.data["off"]) == [0, 1]


async def test_нет_такой_позиции_отказ_и_рисовая_лапша_цела():
    plate = _plate(components=[Component("рисовая лапша", 0.6, 200),
                               Component("курица жареная", 0.4, 100)])
    state, msg, bot = await _say("без риса", _data(plate=plate, grams=[200.0, 100.0]))
    assert state.data["off"] == []
    assert "На карточке нет «риса»" in msg.answer.await_args.args[0]
    bot.edit_message_text.assert_not_awaited()


@pytest.mark.parametrize("text", ["это булгур, а не макароны", "не макароны, а булгур",
                                  "макароны → булгур", "макароны -> булгур",
                                  "замени макароны на булгур"])
async def test_переименовать_словами_и_записать(text, monkeypatch):
    """AC-236: имя меняется, граммы прежние; в разбор уходит новое имя — плотность даст он."""
    monkeypatch.setattr(handlers, "_render_after_continue", AsyncMock())
    state, _, bot = await _say(text)
    assert state.data["plate"].components[0].name == "булгур"
    assert "булгур — 250 г" in _card(bot)
    assistant = _assistant()
    await handlers.on_photo_btn(_callback("ph:ok"), object(), assistant, AsyncMock(), state)
    assert assistant.log_photo.await_args.args[1] == "булгур 250 г, курица жареная 100 г"




async def test_добавить_позицию_словами(monkeypatch):
    """AC-237: строка с весом от человека; во фразе — тоже."""
    monkeypatch.setattr(handlers, "_render_after_continue", AsyncMock())
    state, _, bot = await _say("добавь соус 30 г")
    assert [c.name for c in state.data["plate"].components][-1] == "соус"
    assert state.data["grams"] == [250.0, 100.0, 30.0]
    assert "соус — 30 г" in _card(bot) and "Всего 380 г" in _card(bot)
    assistant = _assistant()
    await handlers.on_photo_btn(_callback("ph:ok"), object(), assistant, AsyncMock(), state)
    args = assistant.log_photo.await_args
    assert args.args[1] == "макароны отварные 250 г, курица жареная 100 г, соус 30 г"
    assert args.kwargs["guessed"] == [True, True, False]  # соус взвесил человек


async def test_добавить_сверх_лимита():
    many = _plate(components=[Component(f"еда {chr(1072 + i)}", 0.1, 10) for i in range(10)])
    state, msg, _ = await _say("добавь соус 30 г", _data(plate=many, grams=[10.0] * 10))
    assert "уже 10 позиций" in msg.answer.await_args.args[0]
    assert len(state.data["plate"].components) == 10


@pytest.mark.parametrize("text", ["340", "340 г", "тарелка 340", "0,34 кг"])
async def test_вес_тарелки_формы(text):
    """AC-238."""
    state, _, bot = await _say(text)
    assert sum(state.data["grams"]) == pytest.approx(340, abs=1)
    assert state.data["weighed"] is True and state.data["total"] == 340
    assert "взвешено" in _card(bot)


async def test_несколько_правок_разом_и_атомарность():
    """AC-238: обе правки разом; одна не сходится — не меняем ничего."""
    state, _, _ = await _say("курица 150, без макарон")
    assert state.data["grams"][1] == 150 and state.data["off"] == [0]
    state, msg, bot = await _say("курица 150, яблоко 100")
    assert state.data["grams"] == [250.0, 100.0]
    assert "На карточке нет «яблоко»" in msg.answer.await_args.args[0]
    bot.edit_message_text.assert_not_awaited()


def _two_chickens() -> dict:
    plate = _plate(components=[Component("курица жареная", 0.5, 100),
                               Component("курица в соусе", 0.5, 80)])
    return _data(plate=plate, grams=[100.0, 80.0])




async def test_неоднозначное_отмена_и_снятие_новым_текстом(monkeypatch):
    edit = AsyncMock()
    monkeypatch.setattr(handlers, "_safe_edit", edit)
    state, _, _ = await _say("курица 150", _two_chickens())
    cb = _callback("ph:pick:x", 77)
    await handlers.on_photo_btn(cb, object(), _assistant(), AsyncMock(), state)
    assert edit.await_args.args[1] == "✏️ Ок, не правлю."
    assert state.data["grams"] == [100.0, 80.0]

    state, _, _ = await _say("курица 150", _two_chickens())
    msg, bot = _msg("тарелка 360"), AsyncMock()
    await handlers.on_photo_weight(msg, bot, state)
    assert bot.edit_message_text.await_args_list[0].args[0] == "✏️ Вопрос снят."
    assert state.data["pick"] is None and sum(state.data["grams"]) == 360
    # Тап по снятому вопросу — ничего не правит.
    cb = _callback("ph:pick:0", 77)
    await handlers.on_photo_btn(cb, object(), _assistant(), AsyncMock(), state)
    assert "уже снят" in cb.bot.await_args_list[0].args[0].text


async def test_несколько_правок_с_неоднозначностью_не_спрашивают():
    state, msg, _ = await _say("курица 150, без соуса", _two_chickens())
    assert "несколько таких" in msg.answer.await_args.args[0]
    assert state.data["grams"] == [100.0, 80.0] and state.data["off"] == []








# --- FR-34.2/34.3: вычет несъедобного и «вес на глаз» -------------------------------


def _shrimp(**kw) -> PlateReading:
    base = dict(kind="dish", dish="креветки", components=[
        Component("креветки отварные в панцире", 0.9, 300), Component("лимон", 0.1, 20),
    ])
    base.update(kw)
    return PlateReading(**base)


def test_карточка_показывает_вычет_и_кнопку_без_вычета():
    text = format_photo_card(_shrimp(), [341, 20], weighed=True)
    assert "креветки отварные в панцире — 341 г → съедобно ≈205 г" in text
    assert "лимон — 20 г\n" in text                     # у лимона вычета нет
    kb = photo_keyboard(_shrimp().components, [])
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "Без вычета — уже чищено" in texts
    assert all(len((b.callback_data or "").encode()) <= 64
               for row in kb.inline_keyboard for b in row)




async def test_вес_на_глаз_по_позиции(monkeypatch):
    """AC-243: курица названа подписью, тарелка — нет: на глаз только макароны."""
    monkeypatch.setattr(handlers, "_render_after_continue", AsyncMock())
    plate = _plate(components=[Component("макароны отварные", 0.6, 250),
                               Component("курица жареная", 0.3, 150, fixed=True)])
    data = _data(plate=plate, grams=[250.0, 150.0])
    assert "вес частично на глаз" in handlers._photo_text(data)
    state, assistant = _State(data), _assistant()
    await handlers.on_photo_btn(_callback("ph:ok"), object(), assistant, AsyncMock(), state)
    assert assistant.log_photo.await_args.kwargs["guessed"] == [True, False]
    # Правка словами — то же самое.
    state, _, bot = await _say("курица 150")
    assert "вес частично на глаз" in _card(bot)
    assert handlers._photo_guessed(state.data) == [True, False]


class _ShrimpNlu:
    """Разбор как у модели: «имя N г» → позиция с явным весом; панцирь — если бы он дошёл до
    разбора — вычитает по-своему (180 г): ровно то, чему карточка не может доверять."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def parse_samples(self, text, known_keys, *, force=False):
        import re as _re
        self.texts.append(text)
        items = []
        for part in text.split(", "):
            name, grams = _re.fullmatch(r"(.+) (\d+(?:\.\d+)?) г", part).groups()
            g = float(grams)
            if "в панцире" in name:
                g = 180.0
            items.append(ParsedItem(name=name, canonical_key=name.replace(" ", "_"),
                                    unit=Unit.GRAM, grams=g, kcal_per_100g=95,
                                    protein_per_100g=19, fat_per_100g=1, carb_per_100g=1))
        return [ParsedMessage(intent=Intent.MEAL, items=items)]


def _real_assistant(plate: PlateReading, nlu=None) -> tuple[Assistant, InMemoryUserRepo]:
    users, products, meals = InMemoryUserRepo(), InMemoryProductRepo(), InMemoryMealRepo()

    def clock() -> datetime:
        return datetime(2026, 10, 25, 15, 0, tzinfo=UTC)

    assistant = Assistant(
        nlu or _ShrimpNlu(), products, MealService(products, meals, clock),
        WeightService(InMemoryWeightRepo(), clock),
        StatsService(meals, users, Goal(2000, 150, 65, 250), clock),
        vision=StubVision(None, plate),
    )
    return assistant, users


async def test_голое_это_всегда_вопрос_даже_на_одной_позиции():
    """AC-236/AC-246: «это булгур» не переименовывает молча; «это вкусно» — не правка вовсе."""
    plate = _plate(components=[Component("рис отварной", 1, 200),
                               Component("масло подсолнечное", 0, 10, True, "на готовку")])
    state, msg, _ = await _say("это булгур", _data(plate=plate, grams=[200.0, 10.0]))
    assert state.data["plate"].components[0].name == "рис отварной"
    assert "Что заменить на «булгур»" in msg.answer.await_args.args[0]
    assert state.data["pick"]["cands"] == [0]

    state, msg, _ = await _say("это булгур")
    kb = msg.answer.await_args.kwargs["reply_markup"]
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert texts == ["макароны отварные", "курица жареная", "✖️ Отмена"]


@pytest.mark.parametrize("text", ["это вкусно", "это был обед", "это всё"])
async def test_это_вкусно_на_одной_позиции_не_правка(text, monkeypatch):
    on_text = AsyncMock(side_effect=_clears)
    monkeypatch.setattr(handlers, "on_text", on_text)
    plate = _plate(components=[Component("рис отварной", 1, 200)])
    state = _State(_data(plate=plate, grams=[200.0]), MealStates.awaiting_photo.state)
    await handlers.on_photo_weight(_msg(text), AsyncMock(), state, object(), AsyncMock(),
                                   AsyncMock())
    on_text.assert_awaited_once()
    assert state.data["plate"].components[0].name == "рис отварной"


async def test_неоднозначное_имя_спрашивает_кнопками_и_тап_правит(monkeypatch):
    """AC-239: след правки — на месте вопроса."""
    edit = AsyncMock()
    monkeypatch.setattr(handlers, "_safe_edit", edit)
    state, msg, _ = await _say("курица 150", _two_chickens())
    assert msg.answer.await_args.args[0] == "✏️ Чей вес «150 г»? Выбери позицию."
    assert state.data["grams"] == [100.0, 80.0]
    assert state.data["pick_id"] == 77
    await handlers.on_photo_btn(_callback("ph:pick:1", 77), object(), _assistant(), AsyncMock(),
                                state)
    assert state.data["grams"] == [100.0, 150.0] and state.data["pick"] is None
    assert edit.await_args_list[0].args[1] == "✏️ Поправил: курица в соусе 150 г"


async def _clears(message, bot, app_user, assistant, session, state, container=None,
                  keep_state=False):
    if not keep_state:                      # как настоящий on_text
        await state.clear()


@pytest.mark.parametrize("text", ["борщ и хлеб", "сколько осталось?", "вкусно",
                                  "ещё 2 яйца", "+1 яйцо", "добавь 2 ложки сахара",
                                  "ещё кофе", "добавь кофе"])
async def test_не_правка_идёт_обычным_путём_а_карточка_остаётся(text, monkeypatch):
    """AC-240/AC-249: еда не теряется — ни в тексте, ни на фото; штуки — не граммы."""
    on_text = AsyncMock(side_effect=_clears)
    monkeypatch.setattr(handlers, "on_text", on_text)
    state = _State(_data(), MealStates.awaiting_photo.state)
    msg = _msg(text)
    await handlers.on_photo_weight(msg, AsyncMock(), state, object(), AsyncMock(), AsyncMock())
    on_text.assert_awaited_once()
    assert state.current == MealStates.awaiting_photo.state
    assert state.data["plate"] is not None and state.data["grams"] == [250.0, 100.0]
    assert len(state.data["plate"].components) == 2
    assert "ждёт «✅ Записать»" in msg.answer.await_args.args[0]


async def test_обычный_путь_упал_карточка_восстановлена(monkeypatch):
    """AC-247."""
    async def boom(message, bot, app_user, assistant, session, state, container=None,
                   keep_state=False):
        assert keep_state                   # карточку не сбрасываем на время пути (N3)
        raise RuntimeError("openai down")
    monkeypatch.setattr(handlers, "on_text", AsyncMock(side_effect=boom))
    state = _State(_data(), MealStates.awaiting_photo.state)
    with pytest.raises(RuntimeError):
        await handlers.on_photo_weight(_msg("борщ и хлеб"), AsyncMock(), state, object(),
                                       AsyncMock(), AsyncMock())
    assert state.current == MealStates.awaiting_photo.state
    assert state.data["plate"] is not None


async def test_обычный_путь_открыл_вопрос_карточка_снята_и_сказала(monkeypatch):
    """AC-247: уступает — и говорит сразу, кнопки карточки убраны."""
    async def asks(message, bot, app_user, assistant, session, state, container=None,
                   keep_state=False):
        await state.set_state(MealStates.awaiting_portion)
    monkeypatch.setattr(handlers, "on_text", AsyncMock(side_effect=asks))
    state = _State(_data(), MealStates.awaiting_photo.state)
    msg, bot = _msg("суп"), AsyncMock()
    await handlers.on_photo_weight(msg, bot, state, object(), AsyncMock(), AsyncMock())
    assert state.current == MealStates.awaiting_portion.state and state.data["plate"] is None
    assert msg.answer.await_args.args[0] == "📷 Карточка фото снята — пришли фото снова."
    assert bot.edit_message_reply_markup.await_args.kwargs["message_id"] == 5


async def test_ответ_на_карточку_не_пишется_записью(monkeypatch):
    """AC-240: reply на саму карточку не уходит записью; имя не нашлось — вопрос."""
    on_text = AsyncMock()
    monkeypatch.setattr(handlers, "on_text", on_text)
    state, msg, _ = await _say("вкусно", reply_to=5)
    on_text.assert_not_awaited()
    assert msg.answer.await_args.args[0].startswith("Не понял, что поправить.")


def _fillet() -> dict:
    plate = _plate(components=[Component("рис отварной", 0.6, 200),
                               Component("куриное филе жареное", 0.4, 120)])
    return _data(plate=plate, grams=[200.0, 120.0])


@pytest.mark.parametrize("text", ["курица 150", "курицу съел 150"])
async def test_имя_не_нашлось_вопрос_похожие_первыми(text):
    """AC-245: «курица» при «куриное филе» — не новая еда; вопрос, филе первой кнопкой."""
    state, msg, bot = await _say(text, _fillet())
    assert msg.answer.await_args.args[0] == (
        "✏️ Чей вес «150 г»? Выбери позицию или запиши отдельно.")
    kb = msg.answer.await_args.kwargs["reply_markup"]
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert texts == ["куриное филе жареное", "рис отварной", "➕ Записать отдельно", "✖️ Отмена"]
    assert state.data["grams"] == [200.0, 120.0]
    bot.edit_message_text.assert_not_awaited()


async def test_записать_отдельно_обычным_путём_карточка_остаётся(monkeypatch):
    """AC-245: «яблоко 100 г» → «➕ Записать отдельно» — запись обычным путём."""
    monkeypatch.setattr(handlers, "_safe_edit", AsyncMock())
    reply = AsyncMock()
    monkeypatch.setattr(handlers, "_reply_message", reply)
    state, _, _ = await _say("яблоко 100 г", _fillet())
    assert state.data["pick"]["text"] == "яблоко 100 г"
    assistant = _assistant()
    cb = _callback("ph:pick:new", 77, mount=True)
    await handlers.on_photo_btn(cb, object(), assistant, AsyncMock(), state)
    assert assistant.process.await_args.args[1] == "яблоко 100 г"
    reply.assert_awaited_once()
    assert state.current == MealStates.awaiting_photo.state
    assert state.data["grams"] == [200.0, 120.0] and state.data["plate"] is not None


async def test_след_правки_и_повтор_добавки():
    """AC-248."""
    state, msg, _ = await _say("курица 150")
    assert msg.answer.await_args.args[0] == "✏️ Поправил: курица жареная 150 г"
    state, msg, _ = await _say("добавь соус 30 г")
    assert msg.answer.await_args.args[0] == "✏️ Поправил: добавил соус 30 г"
    msg2, bot = _msg("добавь соус 30 г"), AsyncMock()
    await handlers.on_photo_weight(msg2, bot, state)
    assert msg2.answer.await_args.args[0] == "✏️ Уже добавил: соус 30 г."
    assert len(state.data["plate"].components) == 3


async def test_пока_пишем_правки_и_второе_записать_ждут():
    """AC-248: «✅» нажата, разбор в пути."""
    state, msg, _ = await _say("курица 150", _data(committing=True))
    assert msg.answer.await_args.args[0] == "⏳ Уже записываю…"
    assert state.data["grams"] == [250.0, 100.0]
    assistant = _assistant()
    cb = _callback("ph:ok")
    await handlers.on_photo_btn(cb, object(), assistant, AsyncMock(), state)
    assistant.log_photo.assert_not_awaited()
    assert "Уже записываю" in cb.bot.await_args.args[0].text


async def test_записать_ставит_флаг_и_снимает_при_сбое():
    state, assistant = _State(_data()), _assistant()
    seen = {}

    async def slow(*a, **kw):
        seen["committing"] = state.data.get("committing")
        raise RuntimeError("openai down")
    assistant.log_photo.side_effect = slow
    with pytest.raises(RuntimeError):
        await handlers.on_photo_btn(_callback("ph:ok"), object(), assistant, AsyncMock(), state)
    assert seen["committing"] is True and state.data["committing"] is False


async def test_кнопки_старой_карточки_устарели():
    """Нит ревью: кнопка не той карточки, что открыта сейчас, ничего не трогает."""
    state = _State(_data(card_id=9))
    cb = _callback("ph:off:1", 5)
    await handlers.on_photo_btn(cb, object(), _assistant(), AsyncMock(), state)
    assert state.data["off"] == []
    assert "устарела" in cb.bot.await_args.args[0].text


async def test_вес_с_запятой_спрашивает_тарелка_или_тело(monkeypatch):
    """AC-249: «вес 72,4» — вопрос; «Мой вес» — замер, карточка цела; «Тарелка» — её вес."""
    monkeypatch.setattr(handlers, "_safe_edit", AsyncMock())
    monkeypatch.setattr(handlers, "_reply_screen", AsyncMock())
    state, msg, _ = await _say("вес 72,4")
    assert msg.answer.await_args.args[0] == "✏️ 72,4 — это тарелка или твой вес?"
    assistant = _assistant()
    assistant.log_weight.return_value = (object(), None)
    await handlers.on_photo_btn(_callback("ph:wq:body", 77, mount=True), object(), assistant,
                                AsyncMock(), state)
    assert assistant.log_weight.await_args.args[1] == 72.4
    assert state.data["grams"] == [250.0, 100.0] and state.data["plate"] is not None

    state, _, _ = await _say("вес 72,4")
    await handlers.on_photo_btn(_callback("ph:wq:plate", 77), object(), _assistant(),
                                AsyncMock(), state)
    assert sum(state.data["grams"]) == pytest.approx(72, abs=1) and state.data["weighed"]


async def test_вес_позиции_больше_тарелки_отказ():
    """AC-249."""
    state, msg, _ = await _say("курица 500", _data(grams=[280.0, 120.0], weighed=True,
                                                   total=400.0))
    assert msg.answer.await_args.args[0].startswith("Это больше всей тарелки (400 г)")
    assert state.data["grams"] == [280.0, 120.0]


async def test_без_вычета_переключается_и_уходит_в_фразу(monkeypatch):
    """AC-242: чищеные — «очищенные» целиком; с вычетом — съедобное без панциря."""
    edit = AsyncMock()
    monkeypatch.setattr(handlers, "_safe_edit", edit)
    monkeypatch.setattr(handlers, "_render_after_continue", AsyncMock())
    state = _State(_data(plate=_shrimp(), grams=[341.0, 20.0], weighed=True, total=361.0))
    await handlers.on_photo_btn(_callback("ph:peel:0"), object(), _assistant(), AsyncMock(), state)
    assert state.data["peeled"] == [0]
    card, kb = edit.await_args.args[1], edit.await_args.args[2]
    assert "креветки отварные очищенные — 341 г\n" in card and "съедобно" not in card
    assert "↩ Вычитать несъедобное" in [b.text for row in kb.inline_keyboard for b in row]
    assistant = _assistant()
    await handlers.on_photo_btn(_callback("ph:ok"), object(), assistant, AsyncMock(), state)
    args = assistant.log_photo.await_args
    assert args.args[1] == "креветки отварные очищенные 341 г, лимон 20 г"
    assert args.kwargs["raw"] is None

    state = _State(_data(plate=_shrimp(), grams=[341.0, 20.0], weighed=True, total=361.0))
    assistant = _assistant()
    await handlers.on_photo_btn(_callback("ph:ok"), object(), assistant, AsyncMock(), state)
    args = assistant.log_photo.await_args
    assert args.args[1] == "креветки отварные без панциря 205 г, лимон 20 г"
    assert args.kwargs["raw"] == "креветки отварные в панцире 341 г, лимон 20 г"

    state = _State(_data(plate=_shrimp(), grams=[341.0, 20.0], peeled=[0]))
    await handlers.on_photo_btn(_callback("ph:peel:0"), object(), _assistant(), AsyncMock(), state)
    assert state.data["peeled"] == []
    await handlers.on_photo_btn(_callback("ph:peel:1"), object(), _assistant(), AsyncMock(), state)
    assert state.data["peeled"] == []


def test_убранная_позиция_без_вычета_на_карточке():
    text = format_photo_card(_shrimp(), [341, 20], weighed=True, off=[0])
    assert "съедобно" not in text


async def _log(plate: PlateReading, caption: str, peeled: list[int] | None = None,
               nlu=None):
    assistant, users = _real_assistant(plate, nlu)
    user = await users.get_or_create(telegram_id=1, name="t", locale="ru", timezone="UTC")
    read = await assistant.read_photo(user, b"jpeg", caption=caption)
    data = {"plate": read.reading, "grams": read.grams, "weighed": read.total_g is not None,
            "total": read.total_g, "off": [], "peeled": peeled or []}
    card = handlers._photo_text(data)
    named = handlers._named_plate(data)
    text = assistant.photo_text(named, read.grams)
    said = assistant.photo_text(named, read.grams, edible=False)
    logged = await assistant.log_photo(user, text, weighed=data["weighed"],
                                       guessed=handlers._photo_guessed(data),
                                       raw=said if said != text else None)
    return read, card, text, logged


async def test_креветки_25_10_карточка_равна_записи():
    """AC-241/AC-250: подпись «340», модель вернула 341 — «взвешено» 340 → ≈204; в разбор —
    уже съедобное, запись 204 с пояснением из фразы человека, без «вес на глаз»."""
    plate = PlateReading(kind="dish", dish="креветки", caption_total_g=341, components=[
        Component("креветки отварные в панцире", 1.0, 341)])
    read, card, text, logged = await _log(plate, "340")
    assert read.total_g == 340 and read.grams == [340]
    assert "взвешено" in card and "— 340 г → съедобно ≈204 г" in card
    assert text == "креветки отварные без панциря 204 г"
    item = logged.meal.items[0]
    assert item.grams == 204
    assert item.name_snapshot == "креветки отварные без панциря · съедобное из 340 г в панцире"
    assert logged.meal.raw_input == "креветки отварные в панцире 340 г"


async def test_креветки_уже_чищеные_пишутся_целиком():
    """AC-242."""
    plate = PlateReading(kind="dish", dish="креветки", components=[
        Component("креветки отварные в панцире", 1.0, 341)])
    _, _, text, logged = await _log(plate, "тарелка 340 г", peeled=[0])
    assert text == "креветки отварные очищенные 340 г"
    item = logged.meal.items[0]
    assert item.grams == 340 and "съедобное" not in item.name_snapshot


async def test_подпись_2_не_вес_тарелки():
    """AC-249: голое малое число в подписи — не вес тарелки."""
    plate = PlateReading(kind="dish", dish="креветки", caption_total_g=2, components=[
        Component("рис отварной", 1.0, 200)])
    read, card, _, logged = await _log(plate, "2")
    assert read.total_g is None and read.grams == [200] and "вес на глаз" in card
    assert logged.meal.items[0].name_snapshot.endswith("· вес на глаз")


async def test_рис_неочищенный_и_салат_без_вычета():
    """AC-250."""
    plate = PlateReading(kind="dish", dish="обед", components=[
        Component("рис неочищенный", 0.5, 200), Component("салат с креветками в панцире", 0.5,
                                                          250)])
    _, card, text, logged = await _log(plate, "")
    assert "съедобно" not in card
    # Приложение не вычитает ни у риса, ни у салата: фраза — как на карточке.
    assert text == "рис неочищенный 200 г, салат с креветками в панцире 250 г"
    assert logged.meal.items[0].grams == 200


class _ReversedNlu(_ShrimpNlu):
    async def parse_samples(self, text, known_keys, *, force=False):
        out = await super().parse_samples(text, known_keys, force=force)
        out[0].items.reverse()
        return out


async def test_разбор_переставил_позиции_на_глаз_по_имени():
    """AC-250: «на глаз» находит свой кусок фразы по имени, а не по порядку."""
    plate = PlateReading(kind="dish", dish="паста", components=[
        Component("макароны отварные", 0.6, 250), Component("курица жареная", 0.3, 150, True)])
    _, _, text, logged = await _log(plate, "курица 150", nlu=_ReversedNlu())
    assert text == "макароны отварные 250 г, курица жареная 150 г"
    names = {i.name_snapshot.split(" · ")[0]: i.name_snapshot for i in logged.meal.items}
    assert names["макароны отварные"].endswith("вес на глаз")
    assert "на глаз" not in names["курица жареная"]


# --- Повторное ревью: окна записи и обычного пути, ответ на вопрос текстом -----------


async def test_ответ_текстом_на_чей_вес_правит_кандидата():
    """AC-252: «в соусе» — одна из позиций вопроса; без модели, след на месте вопроса."""
    state, _, _ = await _say("курица 150", _two_chickens())
    msg, bot = _msg("в соусе"), AsyncMock()
    await handlers.on_photo_weight(msg, bot, state)
    assert state.data["grams"] == [100.0, 150.0] and state.data["pick"] is None
    traces = [c.args[0] for c in bot.edit_message_text.await_args_list]
    assert "✏️ Поправил: курица в соусе 150 г" in traces


async def test_ответ_текстом_мимо_кандидатов_снимает_вопрос(monkeypatch):
    on_text = AsyncMock(side_effect=_clears)
    monkeypatch.setattr(handlers, "on_text", on_text)
    state, _, _ = await _say("курица 150", _two_chickens())
    msg, bot = _msg("чай"), AsyncMock()
    await handlers.on_photo_weight(msg, bot, state, object(), AsyncMock(), AsyncMock())
    assert bot.edit_message_text.await_args_list[0].args[0] == "✏️ Вопрос снят."
    on_text.assert_awaited_once()
    assert state.data["grams"] == [100.0, 80.0] and state.data["pick"] is None


async def test_пока_пишем_свободный_текст_идёт_обычным_путём(monkeypatch):
    """AC-251 (N1): «кофе» в окне записи — записан, а не проглочен."""
    on_text = AsyncMock(side_effect=_clears)
    monkeypatch.setattr(handlers, "on_text", on_text)
    state = _State(_data(committing=True), MealStates.awaiting_photo.state)
    msg = _msg("кофе")
    await handlers.on_photo_weight(msg, AsyncMock(), state, object(), AsyncMock(), AsyncMock())
    on_text.assert_awaited_once()
    assert on_text.await_args.kwargs["keep_state"] is True
    assert msg.answer.await_count == 0          # «ждёт ✅» не врём: карточка уже пишется
    assert state.data["committing"] is True and state.data["plate"] is not None


async def test_сбой_фиксации_снимает_флаг_записи():
    """AC-251 (N2): упала фиксация в БД — флаг снят, «✅» можно нажать снова."""
    state, assistant = _State(_data()), _assistant()
    session = AsyncMock()
    session.commit.side_effect = RuntimeError("db down")
    with pytest.raises(RuntimeError):
        await handlers.on_photo_btn(_callback("ph:ok"), object(), assistant, session, state)
    assert state.data["committing"] is False and state.data["plate"] is not None


async def test_конец_записи_не_стирает_вопрос_обычного_пути(monkeypatch):
    """AC-251: текст в окне записи открыл вопрос порции — запись карточки его не стирает."""
    monkeypatch.setattr(handlers, "_render_after_continue", AsyncMock())
    state, assistant = _State(_data(), MealStates.awaiting_photo.state), _assistant()

    async def meanwhile(*a, **kw):
        await state.set_state(MealStates.awaiting_portion)
    assistant.log_photo.side_effect = meanwhile
    await handlers.on_photo_btn(_callback("ph:ok"), object(), assistant, AsyncMock(), state)
    assert state.current == MealStates.awaiting_portion.state and not state.cleared
    assert state.data["plate"] is None


async def test_окно_обычного_пути_правка_ложится_на_карточку(monkeypatch):
    """AC-253 (N3): пока первый текст идёт обычным путём, карточка цела: вторая правка —
    на неё, «✅» — «подожди секунду»."""
    seen = {}

    async def slow(message, bot, app_user, assistant, session, state, container=None,
                   keep_state=False):
        seen["state"] = await state.get_state()
        msg2, bot2 = _msg("курица 150"), AsyncMock()
        await handlers.on_photo_weight(msg2, bot2, state)
        cb = _callback("ph:ok")
        assistant2 = _assistant()
        await handlers.on_photo_btn(cb, object(), assistant2, AsyncMock(), state)
        seen["ok"] = cb.bot.await_args.args[0].text
        seen["logged"] = assistant2.log_photo.await_count
    monkeypatch.setattr(handlers, "on_text", AsyncMock(side_effect=slow))
    state = _State(_data(), MealStates.awaiting_photo.state)
    await handlers.on_photo_weight(_msg("борщ и хлеб"), AsyncMock(), state, object(),
                                   AsyncMock(), AsyncMock())
    assert seen["state"] == MealStates.awaiting_photo.state
    assert seen["ok"] == "⏳ Подожди секунду…" and seen["logged"] == 0
    assert state.data["grams"] == [250.0, 150.0] and state.data["passing"] is False


async def test_двойной_тап_записать_отдельно_пишет_один_раз(monkeypatch):
    """AC-252 (N4): второй тап, пока первый ждёт ответа Telegram, — «вопрос уже снят»."""
    monkeypatch.setattr(handlers, "_safe_edit", AsyncMock())
    monkeypatch.setattr(handlers, "_reply_message", AsyncMock())
    state, _, _ = await _say("яблоко 100 г", _fillet())
    assistant = _assistant()
    first, second = _callback("ph:pick:new", 77, mount=True), _callback("ph:pick:new", 77)

    async def tap_again(*a, **kw):          # второй тап приходит, пока первый в сети
        await handlers.on_photo_btn(second, object(), assistant, AsyncMock(), state)
    first.bot.side_effect = tap_again
    await handlers.on_photo_btn(first, object(), assistant, AsyncMock(), state)
    assert assistant.process.await_count == 1
    assert "уже снят" in second.bot.await_args_list[0].args[0].text
