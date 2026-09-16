"""Фото блюда в боте (FR-34, MINIAPP-SPEC §6.22 «Бот»).

Держим три вещи: без ответа про масло запись не уходит, вес тарелки
пересчитывает граммы пропорционально, в разбор уходит фраза из черновика.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TgUser

from app.application.assistant import Assistant
from app.application.dto import PlateReading
from app.bot import handlers
from app.bot.formatters import format_photo_card
from app.bot.keyboards import photo_keyboard
from app.bot.states import MealStates
from app.domain.plate import Component


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
    ], ask_fat=True, fat_kind="масло сливочное", ask_dressing=False)
    base.update(kw)
    return PlateReading(**base)


def _data(**kw) -> dict:
    d = {"plate": _plate(), "grams": [250.0, 100.0], "weighed": False,
         "fat": None, "dressing": None, "card_id": 5}
    d.update(kw)
    return d


def _callback(data: str) -> CallbackQuery:
    msg = Message.model_construct(
        message_id=5, date=None, chat=Chat.model_construct(id=1, type="private")
    )
    cb = CallbackQuery.model_construct(
        id="1", from_user=TgUser.model_construct(id=1, is_bot=False, first_name="T"),
        chat_instance="ci", data=data, message=msg,
    )
    return cb.as_(AsyncMock())


def _assistant() -> AsyncMock:
    a = AsyncMock()
    a.photo_text = Assistant.photo_text
    return a


def test_карточка_без_веса_просит_взвесить_и_спрашивает_масло():
    text = format_photo_card(_plate(), [250, 100], weighed=False)
    assert "вес на глаз" in text and "Всего 350 г" in text
    assert "пришли вес тарелки" in text.lower()
    assert "Масло при готовке (сливочное): ❔" in text
    assert "Заправка" not in text


def test_карточка_экранирует_имена():
    plate = _plate(dish="<b>x</b>", components=[Component("a<i>", 1, 10)])
    text = format_photo_card(plate, [10], weighed=True, fat="spoon")
    assert "<i>" not in text and "&lt;b&gt;x" in text
    assert "ложка, 10 г" in text and "Взвешивал" not in text


def test_клавиатура_отмечает_выбор_и_влезает_в_лимит_callback():
    kb = photo_keyboard(True, "spoon", True, None)
    buttons = [b for row in kb.inline_keyboard for b in row]
    assert any(b.text == "✓ Ложка" for b in buttons)
    assert all(len((b.callback_data or "").encode()) <= 64 for b in buttons)
    assert buttons[-2].text == "✅ Записать"


async def test_записать_без_ответа_про_масло_не_пишет():
    state, assistant = _State(_data()), _assistant()
    cb = _callback("ph:ok")
    await handlers.on_photo_btn(cb, object(), assistant, AsyncMock(), state)
    assistant.log_photo.assert_not_awaited()
    method = cb.bot.await_args.args[0]          # AnswerCallbackQuery
    assert method.show_alert is True and "масло" in method.text.lower()
    assert not state.cleared


async def test_записать_отдаёт_фразу_из_черновика(monkeypatch):
    render = AsyncMock()
    monkeypatch.setattr(handlers, "_render_after_continue", render)
    state, assistant = _State(_data(fat="spoon", weighed=True)), _assistant()
    await handlers.on_photo_btn(_callback("ph:ok"), object(), assistant, AsyncMock(), state)
    args = assistant.log_photo.await_args
    assert args.args[1] == ("макароны отварные 250 г, курица жареная 100 г, "
                            "масло сливочное 10 г")
    assert args.kwargs["weighed"] is True
    assert state.cleared
    render.assert_awaited_once()


async def test_чип_масла_правит_карточку_на_месте(monkeypatch):
    edit = AsyncMock()
    monkeypatch.setattr(handlers, "_safe_edit", edit)
    state = _State(_data())
    await handlers.on_photo_btn(_callback("ph:fat:lots"), object(), _assistant(),
                                AsyncMock(), state)
    assert state.data["fat"] == "lots"
    assert "много, 20 г" in edit.await_args.args[1]


async def test_вес_тарелки_пересчитывает_пропорционально():
    state = _State(_data(), MealStates.awaiting_photo.state)
    msg, bot = AsyncMock(), AsyncMock()
    msg.text = "тарелка 700"
    msg.chat = Chat.model_construct(id=1, type="private")
    await handlers.on_photo_weight(msg, bot, state)
    assert state.data["grams"] == [500.0, 200.0]
    assert state.data["weighed"] is True
    assert "взвешено" in bot.edit_message_text.await_args.args[0]


@pytest.mark.parametrize("text", ["вкусно", "0", "99999"])
async def test_не_вес_получает_подсказку(text):
    state = _State(_data(), MealStates.awaiting_photo.state)
    msg, bot = AsyncMock(), AsyncMock()
    msg.text = text
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
