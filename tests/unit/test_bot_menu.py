"""Меню в два ряда и вложенные экраны (D-87)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import handlers
from app.bot.keyboards import (
    BTN_BUG,
    BTN_DIARY,
    BTN_FREQUENT,
    BTN_GOAL,
    BTN_HISTORY,
    BTN_PROFILE,
    BTN_TODAY,
    BTN_TOTALS,
    BTN_WEEK,
)


@pytest.fixture
def shown(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    for name in ("_show_today", "_show_week", "_show_history", "_show_frequent", "_start_profile",
                 "_send_goal_card"):
        async def fake(*a: object, _n: str = name, **kw: object) -> None:
            calls.append(_n)
        monkeypatch.setattr(handlers, name, fake)
    return calls


def _msg(text: str) -> AsyncMock:
    m = AsyncMock()
    m.text = text
    return m


class _A:
    async def current_goal(self, user: object) -> object:
        return SimpleNamespace()


async def test_новые_кнопки_и_прежние_подписи(shown: list[str]) -> None:
    """Новая кнопка — экран без лишних слов; прежняя подпись — тот же экран и следом новое меню
    одной строкой (клавиатура обновляется только сообщением бота)."""
    u, st = SimpleNamespace(id=1), AsyncMock()
    for text, want, legacy in ((BTN_DIARY, "_show_today", False), (BTN_TODAY, "_show_today", True)):
        m = _msg(text)
        await handlers.btn_today(m, u, _A(), st)  # type: ignore[arg-type]
        assert shown[-1] == want and bool(m.answer.await_count) is legacy
        if legacy:
            kb = m.answer.await_args.kwargs["reply_markup"]
            assert [[b.text for b in r] for r in kb.keyboard] == [
                ["📊 Дневник", "📈 Итоги"], ["⚖️ Вес", "⚙️ Профиль"]]
    for text, legacy in ((BTN_TOTALS, False), (BTN_WEEK, True)):
        m = _msg(text)
        await handlers.btn_week(m, u, _A(), st, None)  # type: ignore[arg-type]
        assert shown[-1] == "_show_week" and bool(m.answer.await_count) is legacy
    for fn, text, want in ((handlers.btn_history, BTN_HISTORY, "_show_history"),
                           (handlers.btn_frequent, BTN_FREQUENT, "_show_frequent")):
        m = _msg(text)
        await fn(m, u, _A(), st)  # type: ignore[arg-type]
        assert shown[-1] == want and m.answer.await_count == 1
    for text, legacy in ((BTN_PROFILE, False), (BTN_GOAL, True)):  # «Профиль» — это цель
        m = _msg(text)
        await handlers.btn_profile(m, u, _A(), st)  # type: ignore[arg-type]
        assert shown[-1] == "_send_goal_card" and bool(m.answer.await_count) is legacy
    m = _msg(BTN_BUG)
    await handlers.btn_bug(m, st)  # type: ignore[arg-type]
    assert m.answer.await_count == 2  # вопрос о баге и новое меню


@pytest.mark.parametrize(("code", "want"), [
    ("nav:hist", "_show_history"), ("nav:freq", "_show_frequent"), ("nav:prof", "_start_profile"),
])
async def test_вложенные_экраны_из_виджета(shown: list[str], code: str, want: str) -> None:
    """Кнопки в виджетах (`k:nav:…`) зовут те же функции показа, что были у кнопок меню."""
    from aiogram.types import Chat, Message

    msg = Message.model_construct(message_id=7, date=None,
                                  chat=Chat.model_construct(id=1, type="private"))
    cb = AsyncMock()
    cb.data, cb.message = code, msg
    await handlers.on_nav(cb, SimpleNamespace(id=1), _A(), AsyncMock())  # type: ignore[arg-type]
    assert shown == [want] and cb.answer.await_count == 1


async def test_баг_из_виджета_ждёт_текст(shown: list[str]) -> None:
    from aiogram.types import Chat, Message

    from app.bot.states import MealStates

    msg = Message.model_construct(message_id=7, date=None,
                                  chat=Chat.model_construct(id=1, type="private")).as_(AsyncMock())
    cb, st = AsyncMock(), AsyncMock()
    cb.data, cb.message = "nav:bug", msg
    await handlers.on_nav(cb, SimpleNamespace(id=1), _A(), st)  # type: ignore[arg-type]
    st.set_state.assert_awaited_with(MealStates.awaiting_bug)
    assert not shown


def test_кнопки_внутри_виджетов() -> None:
    from app.bot.widgets import template

    today, goal = template("today", "bars"), template("goal")
    assert "data-k=nav:hist" in today and "data-k=nav:freq" in today
    assert "data-k=nav:prof" in goal and "data-k=nav:bug" in goal
    for html in (today, goal):
        assert "W.request('k:'+k.dataset.k)" in html
