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
    ], fat_kind="масло сливочное")
    base.update(kw)
    return PlateReading(**base)


def _data(**kw) -> dict:
    d = {"plate": _plate(), "grams": [250.0, 100.0], "weighed": False,
         "off": [], "card_id": 5}
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
