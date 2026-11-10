"""D-73: кнопки «↩️ Отменить» и `/undo` в меню больше нет — отмена в «Истории», «Сегодня» и
карточке. Старая клавиатура у людей в чате ещё висит: её текст не должен уйти в разбор как еда."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot import handlers
from app.bot.keyboards import BTN_UNDO, MENU_BUTTONS
from app.bot.menu import _COMMANDS


def test_кнопки_и_команды_нет() -> None:
    assert BTN_UNDO not in MENU_BUTTONS
    assert "undo" not in {c.command for c in _COMMANDS}


async def test_старая_кнопка_и_команда_объясняют_и_чинят_меню() -> None:
    for call in (handlers.btn_undo, handlers.cmd_undo):
        msg, state = AsyncMock(), AsyncMock()
        msg.chat = SimpleNamespace(id=1)
        await call(msg, state)
        text = msg.answer.await_args.args[0]
        kb = msg.answer.await_args.kwargs["reply_markup"]
        assert "🗒 История" in text and "📊 Сегодня" in text
        assert BTN_UNDO not in [b.text for row in kb.keyboard for b in row]
        state.clear.assert_awaited()
