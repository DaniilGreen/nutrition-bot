"""Карточка приёма виджетом (FR-49.4, AC-284…AC-286). Фейки: без БД и без Telegram.

Держим: правки идут теми же use-case, что кнопки карточки (`adjust_meal_item`, `remove_item`,
`delete_meal`, `restore_meal`), числа — из базы (INV-1/3), граница доверия и повтор тапа
(W-INV-4/5), «Вернуть» со сверкой отпечатка (FR-41), фолбэк на rich.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.application.entities import Meal, MealItem, User
from app.application.results import MealLogged
from app.bot.ui.widgets import LIMIT, meal_caption, meal_data
from app.bot.widgets import meal as wmeal
from app.bot.widgets import template
from app.bot.widgets.transport import EditMessageWidgetData, SendWidget
from app.domain.value_objects import Macros


class Bot:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[object] = []
        self.captions: list[dict] = []
        self.fail = fail

    async def __call__(self, method: object) -> object:
        if self.fail:
            raise RuntimeError("sendWidget: 400")
        self.calls.append(method)
        return SimpleNamespace(message_id=77)

    async def edit_message_caption(self, **kw: object) -> None:
        self.captions.append(kw)


def _rec(uid: int) -> Meal:
    items = [
        MealItem(name_snapshot="курица гриль", grams=200, macros=Macros(330, 50, 14, 0),
                 product_id=1),
        MealItem(name_snapshot="рис", grams=150, macros=Macros(175, 4, 1, 38), product_id=2,
                 is_estimate=True),
    ]
    return Meal(user_id=uid, eaten_at=datetime.now(UTC) - timedelta(minutes=5),
                input_type="text", raw_input="курица 200, рис 150", items=items,
                source_confidence=0.9, totals=Macros(505, 54, 15, 38))


@pytest.fixture
def world(fake_container):  # noqa: ANN001, ANN201
    wmeal._done.clear()
    wmeal._undo.clear()

    async def make() -> tuple[object, User, Meal]:
        assistant, users = fake_container.assistant_for(None)
        u = await users.get_or_create(telegram_id=5, name="x", locale="ru", timezone="UTC")
        meal = await fake_container.meals.add(_rec(u.id))
        return assistant, u, meal

    return make


async def _do(assistant: object, user: User, data: str, mid: int = 77) -> dict:
    """Как middleware: обработчик, затем «фиксация прошла» — commit-колбэк."""
    out = await wmeal.on_meal(user.telegram_id, mid, data, user, assistant)  # type: ignore[arg-type]
    if out.commit is not None:
        out.commit()
    return out.reply


def test_данные_карточки_в_бюджете_и_снапшоты() -> None:
    """AC-284: позиции со снапшотами граммов и ккал, вес правится только у взвешенных."""
    from app.domain.goals import Goal, remaining

    goal = Goal(2200, 183, 80, 200)
    m = _rec(1)
    m.id, m.items[0].id, m.items[1].id = 9, 1, 2
    d = json.loads(meal_data(m, remaining(m.totals, goal), goal, "UTC") or "{}")
    assert d["m"] == 9 and d["k"] == [505, 54, 15, 38] and d["l"][0] == 2200 - 505
    assert d["i"][1] == [2, "рис", 150, 175, 1, 1] and d["z"] == 0 and d["x"] == 0
    long = _rec(1)
    long.id = 9
    long.items = [MealItem(name_snapshot="Очень длинное название блюда " * 4, grams=100 + i,
                           macros=Macros(100, 1, 1, 1), id=i) for i in range(14)]
    text = meal_data(long, remaining(m.totals, goal), goal, "UTC", session_kcal=900, undo=2)
    assert text is not None and len(text.encode()) <= LIMIT - 8
    assert "перебор" in meal_caption(m, remaining(Macros(3000, 1, 1, 1), goal))


async def test_показ_и_фолбэк(world) -> None:  # noqa: ANN001
    """AC-284: виджет `meal` с подписью и кнопками под ним; сбой sendWidget — False (rich)."""
    assistant, u, meal = await world()
    left = await assistant.day_left(u, meal.eaten_at)
    bot = Bot()
    assert await wmeal.show(bot, 5, u, assistant, MealLogged(meal, left))  # type: ignore[arg-type]
    sent = bot.calls[0]
    assert isinstance(sent, SendWidget) and sent.html == template("meal") and sent.allow_queries
    assert sent.caption.startswith("✅ Записал: курица гриль 200 г, рис 150 г · 505 ккал")
    kb = [b.callback_data for r in sent.reply_markup.inline_keyboard for b in r]
    assert f"meal_repeat:{meal.id}" in kb and f"dish_save:{meal.id}" in kb
    assert not any(c and c.startswith(("meal_undo", "item_del", "iw:")) for c in kb)
    assert not await wmeal.show(Bot(fail=True), 5, u, assistant, MealLogged(meal, left))  # type: ignore[arg-type]


async def test_плюс_10_грамм_и_повтор(world) -> None:  # noqa: ANN001
    """AC-285: вес — тем же `adjust_meal_item`, КБЖУ из снапшота; повтор ключа не правит."""
    assistant, u, meal = await world()
    chicken = meal.items[0]
    r = await _do(assistant, u, f"m:a:{meal.id}:{chicken.id}:210:k1k1k1")
    assert r["ok"] == 1 and r["i"][0][2] == 210 and r["i"][0][3] == round(330 * 210 / 200)
    assert r["k"][0] == round(330 * 210 / 200 + 175)
    again = await _do(assistant, u, f"m:a:{meal.id}:{chicken.id}:210:k1k1k1")
    assert again == r
    for bad in (f"m:a:{meal.id}:{chicken.id}:0:k2k2k2", f"m:a:{meal.id}:{chicken.id}:2001:k3k3",
                f"m:a:{meal.id}:999:210:k4k4", f"m:a:{meal.id}:{chicken.id}:210",
                "m:x:1:abcd", f"m:a:{meal.id}:{chicken.id}:21.5:k5k5"):
        assert (await _do(assistant, u, bad))["err"] == "bad", bad
    stranger = User(telegram_id=6, name="y", locale="ru", timezone="UTC", id=99)
    assert (await _do(assistant, stranger, f"m:a:{meal.id}:{chicken.id}:220:k6k6"))["err"] == (
        "gone")
    got = await assistant.get_meal(u, meal.id)
    assert got is not None and got.items[0].grams == 210


async def test_убрать_позицию_и_вернуть(world) -> None:  # noqa: ANN001
    """AC-286: «🗑 рис» → «⏪ Вернуть» — запись снова как была, кнопки на новую запись."""
    assistant, u, meal = await world()
    r = await _do(assistant, u, f"m:d:{meal.id}:{meal.items[1].id}:d1d1")
    assert r["ok"] == 1 and len(r["i"]) == 1 and r["z"] == 1
    assert (await _do(assistant, u, f"m:d:{meal.id}:{r['i'][0][0]}:d2d2"))["err"] == "bad"
    back = await _do(assistant, u, f"m:r:{meal.id}:r1r1")
    assert back["ok"] == 1 and len(back["i"]) == 2 and back["z"] == 0 and back["m"] != meal.id
    assert (await _do(assistant, u, f"m:r:{back['m']}:r2r2"))["err"] == "old"  # уже вернули


async def test_отменить_всё_вернуть_и_правка_между(world, monkeypatch) -> None:  # noqa: ANN001
    """AC-286: «↩️ Отменить всё» → «Вернуть»; поправили между — `edited`; поздно — `old`."""
    assistant, u, meal = await world()
    gone = await _do(assistant, u, f"m:u:{meal.id}:u1u1")
    assert gone["ok"] == 1 and gone["x"] == 1 and gone["z"] == 2
    assert await assistant.get_meal(u, meal.id) is None
    back = await _do(assistant, u, f"m:r:{meal.id}:r1r1")
    assert back["ok"] == 1 and back["x"] == 0
    # Убрали позицию, потом поправили вес оставшейся — «Вернуть» не вернёт поверх правки.
    m2 = back["m"]
    r = await _do(assistant, u, f"m:d:{m2}:{back['i'][1][0]}:d1d1")
    # Правка в другом месте (аппка, «✏️ Вес») — слот жив, но отпечаток не сходится.
    await assistant.adjust_meal_item(u, m2, r["i"][0][0], 250.0)
    assert (await _do(assistant, u, f"m:r:{m2}:r2r2"))["err"] == "edited"
    # Через 11 минут окна нет.
    left = await _do(assistant, u, f"m:u:{m2}:u2u2")
    assert left["ok"] == 1
    monkeypatch.setattr(wmeal, "_now", lambda: 10**9)
    assert (await _do(assistant, u, f"m:r:{m2}:r3r3"))["err"] == "old"


async def test_после_правки_сообщение_поправлено(world) -> None:  # noqa: ANN001
    """FR-49.4: после ответа — данные, подпись и кнопки самого сообщения (открытая позже карточка
    показывает правду); после удаления под ним только аппка."""
    assistant, u, meal = await world()
    out = await wmeal.on_meal(5, 77, f"m:a:{meal.id}:{meal.items[0].id}:180:z1z1", u, assistant)  # type: ignore[arg-type]
    assert out.after is not None
    bot = Bot()
    await out.after(bot)  # type: ignore[arg-type]
    edit = bot.calls[0]
    assert isinstance(edit, EditMessageWidgetData) and edit.message_id == 77
    assert json.loads(edit.chat_data)["i"][0][2] == 180
    assert "курица гриль 180 г" in str(bot.captions[0]["caption"])
    out = await wmeal.on_meal(5, 77, f"m:u:{meal.id}:z2z2", u, assistant)  # type: ignore[arg-type]
    bot = Bot()
    await out.after(bot)  # type: ignore[arg-type]
    assert bot.captions[0]["caption"] == "↩️ Запись удалена"
    kb = bot.captions[0]["reply_markup"]
    assert kb is None or not any(b.callback_data for r in kb.inline_keyboard for b in r)


async def test_карточка_записи_виджет_или_rich(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-284 на уровне хендлера: с флагом — только виджет; виджет не ушёл — rich-карточка."""
    from unittest.mock import AsyncMock

    from app.bot import handlers

    rich: list[str] = []

    async def fake_rich(message: object, html: str, markup: object = None) -> None:
        rich.append(html)

    async def fake_gather(*a: object, **k: object) -> object:
        return "ctx"

    async def no_session(*a: object) -> None:
        return None

    monkeypatch.setattr(handlers, "_rich", fake_rich)
    monkeypatch.setattr(handlers, "gather", fake_gather)
    monkeypatch.setattr(handlers, "_session", no_session)
    monkeypatch.setattr(handlers.screens, "meal_card", lambda *a, **k: "RICH")
    monkeypatch.setattr(handlers.widgets_live, "enabled", lambda u, s=None: True)
    ok = [True]

    async def show(*a: object) -> bool:
        return ok[0]

    monkeypatch.setattr(handlers.widgets_meal, "show", show)
    msg = AsyncMock()
    msg.chat = SimpleNamespace(id=1)
    m = _rec(1)
    m.id = 3
    res = MealLogged(m, SimpleNamespace())  # type: ignore[arg-type]
    user = User(telegram_id=1, name="x", locale="ru", timezone="UTC", id=1)
    await handlers._send_meal_card(msg, res, user, None)  # type: ignore[arg-type]
    assert rich == []
    ok[0] = False
    await handlers._send_meal_card(msg, res, user, None)  # type: ignore[arg-type]
    assert rich == ["RICH"]


async def test_два_одинаковых_запроса_сразу(world) -> None:  # noqa: ANN001
    """W-INV-5 под гонкой: два одинаковых «Отменить всё» задачами — одно удаление, второй `busy`."""
    import asyncio

    assistant, u, meal = await world()
    r1, r2 = await asyncio.gather(
        wmeal.on_meal(5, 77, f"m:u:{meal.id}:same1", u, assistant),  # type: ignore[arg-type]
        wmeal.on_meal(5, 77, f"m:u:{meal.id}:same1", u, assistant),  # type: ignore[arg-type]
    )
    replies = [r1.reply.get("err"), r2.reply.get("err")]
    assert "busy" in replies and None in replies


async def test_правка_граммов_прячет_вернуть(world) -> None:  # noqa: ANN001
    """После «🗑» поправили вес — «Вернуть» прячется (z=0): иначе обещал бы то, что даст `edited`."""
    assistant, u, meal = await world()
    r = await _do(assistant, u, f"m:d:{meal.id}:{meal.items[1].id}:p1p1")
    assert r["z"] == 1
    a = await _do(assistant, u, f"m:a:{meal.id}:{r['i'][0][0]}:190:p2p2")
    assert a["ok"] == 1 and a["z"] == 0
    assert (await _do(assistant, u, f"m:r:{meal.id}:p3p3"))["err"] == "old"
