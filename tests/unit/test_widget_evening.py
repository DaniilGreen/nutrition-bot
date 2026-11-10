"""Вечерний итог виджетом с «✅ Всё записал» (FR-49.6, D-57, AC-288…AC-289). Только фейки."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.application.entities import User
from app.bot.ui import screens
from app.bot.ui.widgets import LIMIT, evening_data
from app.bot.widgets import evening, template
from app.bot.widgets.transport import EditMessageWidgetData, SendWidget
from app.domain.streak import Streak
from tests.unit.test_ui_widths import CTXS


class Bot:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[object] = []
        self.fail = fail

    async def __call__(self, method: object) -> object:
        if self.fail:
            raise RuntimeError("400")
        self.calls.append(method)
        return SimpleNamespace(message_id=55)


@pytest.mark.parametrize("name", ["обычный день", "крупный перебор", "утро"])
def test_итог_те_же_слова_что_rich(name: str) -> None:
    """AC-288: цифры дня и строки итога — из тех же блоков, что rich-итог; ≤ 1024 байт."""
    ctx = CTXS[name]
    text = evening_data(ctx, date(2026, 11, 10), Streak(12, False, 28, 30))
    assert len(text.encode()) <= LIMIT
    d = json.loads(text)
    c = ctx.today.remaining.consumed
    assert d["e"][0] == round(c.kcal) and d["c"] == 0 and d["s"] == [12, 0, 28, 30]
    rich = screens.evening(ctx)
    for line in d["nt"]:
        assert line.split(" ")[0] in rich.replace("<b>", "").replace("</b>", "")


class _A:
    def __init__(self) -> None:
        self.closed: list[date] = []

    async def close_day(self, user: User, day: date) -> bool:
        fresh = day not in self.closed
        self.closed.append(day)
        return fresh

    async def streak(self, user: User) -> Streak:
        return Streak(len(set(self.closed)) + 10, False, 30, 30)


USER = User(telegram_id=7, name="x", locale="ru", timezone="UTC", id=1)


async def test_всё_записал_закрывает_день() -> None:
    """AC-289: тап — день закрыт, серия в ответе, сообщение поправлено на «закрыт»; повтор —
    тот же ответ без вреда; кривая дата, будущее и старше недели — `bad`."""
    a = _A()
    bot = Bot()
    assert await evening.show(bot, USER, a, CTXS["обычный день"], date.today())  # type: ignore[arg-type]
    sent = bot.calls[0]
    assert isinstance(sent, SendWidget) and sent.html == template("evening") and sent.allow_queries
    assert sent.caption == "🌙 Итог дня"  # AC-290: уведомление без цифр, они — в итоге
    today = datetime.now(UTC).date()
    out = await evening.on_close(7, 55, f"e:c:{today}:abcd", USER, a)  # type: ignore[arg-type]
    assert out.reply == {"ok": 1, "c": 1, "s": [11, 0, 30, 30]} and a.closed == [today]
    assert out.after is not None
    edit = Bot()
    await out.after(edit)  # type: ignore[arg-type]
    fixed = edit.calls[0]
    assert isinstance(fixed, EditMessageWidgetData) and json.loads(fixed.chat_data)["c"] == 1
    again = await evening.on_close(7, 55, f"e:c:{today}:efgh", USER, a)  # type: ignore[arg-type]
    assert again.reply["ok"] == 1
    for bad in (f"e:c:{today + timedelta(days=1)}:abcd", f"e:c:{today - timedelta(days=8)}:abcd",
                "e:c:2026-13-40:abcd", f"e:c:{today}", "e:x:1:abcd"):
        assert (await evening.on_close(7, 55, bad, USER, a)).reply == {"err": "bad"}, bad  # type: ignore[arg-type]
    assert not await evening.show(Bot(fail=True), USER, a, CTXS["обычный день"], today)  # type: ignore[arg-type]


async def test_напоминание_виджет_или_rich(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-18/FR-49.6: с флагом — виджет, без флага или сбой — прежний rich-итог."""
    from contextlib import asynccontextmanager

    from app.bot import reminders

    ctx = CTXS["обычный день"]

    async def fake_gather(*a: object, **k: object) -> object:
        return ctx

    monkeypatch.setattr(reminders, "gather", fake_gather)
    monkeypatch.setattr(reminders, "reminder_due", lambda *a, **k: (True, date(2026, 11, 10)))
    shown: list[int] = []
    ok = [True]

    async def show(bot: object, user: User, *a: object) -> bool:
        shown.append(user.telegram_id)
        return ok[0]

    monkeypatch.setattr(reminders.evening, "show", show)
    monkeypatch.setattr(reminders.live, "enabled", lambda u, s=None: True)

    class Users:
        async def list_active(self) -> list[User]:
            return [USER]

    class Sess:
        async def commit(self) -> None: ...

    @asynccontextmanager
    async def factory():  # noqa: ANN202
        yield Sess()

    async def no_nudge(*a: object) -> None: ...

    monkeypatch.setattr(reminders, "_nudge", no_nudge)
    container = SimpleNamespace(
        db=SimpleNamespace(session_factory=factory), assistant_for=lambda s: (None, Users()),
        clock=lambda: datetime(2026, 11, 10, 20, 59, tzinfo=UTC), body_scans_for=lambda s: None,
    )
    rich: list[object] = []

    class B:
        async def send_rich_message(self, **kw: object) -> None:
            rich.append(kw)

    await reminders._tick(B(), container, 23, 59, {})  # type: ignore[arg-type]
    assert shown == [7] and rich == []
    ok[0] = False
    await reminders._tick(B(), container, 23, 59, {})  # type: ignore[arg-type]
    assert len(rich) == 1


async def test_без_истории_нет_пилюли_и_бюджет_правки() -> None:
    """Серии нет (`of == 0`) — в ответе нет `s` («0 из 0» не рисуем); правка сообщения ≤ 1024."""

    class Empty(_A):
        async def streak(self, user: User) -> Streak:
            return Streak(0, False)

    a = Empty()
    bot = Bot()
    await evening.show(bot, USER, a, CTXS["крупный перебор"], date.today())  # type: ignore[arg-type]
    out = await evening.on_close(7, 55, f"e:c:{datetime.now(UTC).date()}:abcd", USER, a)  # type: ignore[arg-type]
    assert "s" not in out.reply
    edit = Bot()
    await out.after(edit)  # type: ignore[arg-type]
    assert len(edit.calls[0].chat_data.encode()) <= LIMIT  # type: ignore[attr-defined]
