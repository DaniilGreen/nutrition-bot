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
        self.marks: list[dict] = []  # правки кнопок под сообщением
        self.fail = fail

    async def __call__(self, method: object) -> object:
        if self.fail:
            raise RuntimeError("sendWidget: 400")
        self.calls.append(method)
        return SimpleNamespace(message_id=77)

    async def edit_message_reply_markup(self, **kw: object) -> None:
        assert "caption" not in kw  # D-75: подписи у виджета нет — правим только кнопки
        self.marks.append(kw)


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
    wmeal._rv.clear()

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
    d = json.loads(meal_data(m, remaining(m.totals, goal), "UTC") or "{}")
    assert d["m"] == 9 and d["k"] == [505, 54, 15, 38]
    # AC-292, D-60: день записи — четыре строки rich-карточки «Ккал · 505 / 2200 · 23%» и т.д.
    assert d["dy"] == [[505, 2200, 23], [54, 183, 30], [15, 80, 19], [38, 200, 19]]
    assert "l" not in d and "e" not in d and "g" not in d  # «осталось» заменено строками дня
    assert d["i"][1] == [2, "рис", 150, 175, 1, 1] and d["z"] == 0 and d["x"] == 0
    long = _rec(1)
    long.id = 9
    long.items = [MealItem(name_snapshot="Очень длинное название блюда " * 4, grams=100 + i,
                           macros=Macros(100, 1, 1, 1), id=i) for i in range(14)]
    text = meal_data(long, remaining(m.totals, goal), "UTC", session_kcal=900, undo=2)
    assert text is not None and len(text.encode()) <= LIMIT - 8
    # AC-290: подпись — что записал, без граммов и ккал (они в виджете)
    assert meal_caption(m) == "✅ Записал: курица гриль, рис"


def test_все_числа_карточки_целые() -> None:
    """AC-292: ккал и БЖУ в `chat_data` — целые, как у rich-карточки («+40», а не «+40.3»;
    «946», а не «946.77»). Дробь допустима только у граммов (до 0,1 г)."""
    from app.domain.goals import Goal, remaining

    goal = Goal(1635.4, 125.2, 55.6, 160.3)
    items = [MealItem(name_snapshot="молоко 1%", grams=95.5, macros=Macros(40.13, 3.12, 1.49, 4.77),
                      id=1, is_estimate=True)]
    m = Meal(user_id=1, eaten_at=datetime.now(UTC), input_type="text", raw_input="молоко",
             items=items, source_confidence=0.9, totals=Macros(40.33, 3.12, 1.49, 4.77), id=9)
    d = json.loads(meal_data(m, remaining(Macros(688.23, 48.1, 13.7, 70.2), goal), "UTC",
                             session_kcal=130.6) or "{}")
    ints = d["k"] + [x for row in d["dy"] for x in row] + [d["ss"], d["i"][0][3]]
    assert all(isinstance(x, int) for x in ints), ints
    assert d["k"][0] == 40 and d["dy"][0] == [688, 1635, 42] and d["i"][0][2] == 95.5


async def test_показ_и_фолбэк(world) -> None:  # noqa: ANN001
    """AC-284: виджет `meal` с подписью и кнопками под ним; сбой sendWidget — False (rich)."""
    assistant, u, meal = await world()
    left = await assistant.day_left(u, meal.eaten_at)
    bot = Bot()
    assert await wmeal.show(bot, 5, u, assistant, MealLogged(meal, left))  # type: ignore[arg-type]
    sent = bot.calls[0]
    assert isinstance(sent, SendWidget) and sent.html == template("meal") and sent.allow_queries
    assert sent.caption is None  # AC-290, D-75: только сам виджет
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
    """FR-49.4: после ответа — данные и кнопки самого сообщения (открытая позже карточка
    показывает правду); после удаления под ним только аппка."""
    assistant, u, meal = await world()
    out = await wmeal.on_meal(5, 77, f"m:a:{meal.id}:{meal.items[0].id}:180:z1z1", u, assistant)  # type: ignore[arg-type]
    assert out.after is not None
    bot = Bot()
    await out.after(bot)  # type: ignore[arg-type]
    edit = bot.calls[0]
    assert isinstance(edit, EditMessageWidgetData) and edit.message_id == 77
    assert json.loads(edit.chat_data)["i"][0][2] == 180
    assert bot.marks[0]["message_id"] == 77
    out = await wmeal.on_meal(5, 77, f"m:u:{meal.id}:z2z2", u, assistant)  # type: ignore[arg-type]
    bot = Bot()
    await out.after(bot)  # type: ignore[arg-type]
    kb = bot.marks[0]["reply_markup"]
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


# --- ответ текстом на карточку-виджет (FR-44, AC-293, D-61) ----------------------------------

async def test_ответ_на_виджет_правит_виджет_и_вернуть_в_нём(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«удали рис» ответом на виджет: виджет перерисован из базы (rich-правки нет — виджет её не
    примет), «⏪ Вернуть» — внутри (z=1) и работает тем же `m:r`; след — строка без кнопок и ккал.
    До D-61 виджет оставался со старыми граммами, а след шёл с полной rich-клавиатурой."""
    from aiogram.methods import SendMessage

    from app.bot import handlers
    from app.bot.keyboards import meal_widget_keyboard
    from tests.unit import test_bot_reply_fix as rf

    monkeypatch.setattr(handlers.widgets_live, "enabled", lambda u, s=None: True)
    wmeal._undo.clear()
    w = await rf._world()
    a = await w.meals.add(rf._meal(w.user.id, rf._BUCK, rf._RICE))
    w.nlu.queue = [rf._fix("delete", rf._food("рис", kcal=0))]
    msg = await rf._say(w, rf._reply("удали рис", meal_widget_keyboard(a)))
    assert rf._items(a) == [("гречка", 150)]
    [edit] = [c.args[0] for c in msg.bot.await_args_list
              if isinstance(c.args[0], EditMessageWidgetData)]
    d = json.loads(edit.chat_data)
    assert edit.message_id == 5 and [x[1] for x in d["i"]] == ["гречка"] and d["z"] == 1
    assert not msg.bot.edit_message_text.await_count  # rich-правки виджета нет
    [trace] = [c.args[0] for c in msg.bot.await_args_list if isinstance(c.args[0], SendMessage)]
    assert trace.text == "🗑 Убрал: рис" and trace.reply_markup is None
    back = await _do(w.assistant, w.user, f"m:r:{a.id}:rb01", mid=5)
    assert back["ok"] == 1 and [x[1] for x in back["i"]] == ["гречка", "рис"]


async def test_ответ_весом_на_виджет_без_вернуть(monkeypatch: pytest.MonkeyPatch) -> None:
    """«гречки было 120» — вес правится, виджет перерисован, «Вернуть» нет (у веса его нет, FR-43);
    прежний слот «Вернуть» этого виджета снят: он откатил бы и правку веса."""
    from app.bot import handlers
    from app.bot.keyboards import meal_widget_keyboard
    from tests.unit import test_bot_reply_fix as rf

    monkeypatch.setattr(handlers.widgets_live, "enabled", lambda u, s=None: True)
    wmeal._undo.clear()
    w = await rf._world()
    a = await w.meals.add(rf._meal(w.user.id, rf._BUCK))
    wmeal._undo[(w.user.telegram_id, 5)] = wmeal._Undo(0, a, None, 1)
    msg = await rf._say(w, rf._reply("120 г", meal_widget_keyboard(a)))
    assert rf._items(a) == [("гречка", 120)]
    [edit] = [c.args[0] for c in msg.bot.await_args_list
              if isinstance(c.args[0], EditMessageWidgetData)]
    assert json.loads(edit.chat_data)["z"] == 0 and (w.user.telegram_id, 5) not in wmeal._undo


async def test_ответ_на_rich_карточку_прежним_путём(monkeypatch: pytest.MonkeyPatch) -> None:
    """Флаг есть, но карточка — rich (старше раскатки): `editMessageWidgetData` отказывает —
    прежняя rich-правка со «⏪ Вернуть» на карточке (D-61)."""
    from aiogram.exceptions import TelegramBadRequest

    from app.bot import handlers
    from app.bot.keyboards import meal_actions_keyboard
    from tests.unit import test_bot_reply_fix as rf

    monkeypatch.setattr(handlers.widgets_live, "enabled", lambda u, s=None: True)
    w = await rf._world()
    a = await w.meals.add(rf._meal(w.user.id, rf._BUCK, rf._RICE))
    w.nlu.queue = [rf._fix("delete", rf._food("рис", kcal=0))]
    msg = rf._reply("удали рис", meal_actions_keyboard(a))

    async def bot(method: object, *a: object, **k: object) -> object:
        if isinstance(method, EditMessageWidgetData):
            raise TelegramBadRequest(method=method, message="Bad Request: message is not a widget")  # type: ignore[arg-type]
        return SimpleNamespace(message_id=11)

    msg.bot.side_effect = bot
    await rf._say(w, msg)
    [card] = rf._edits(msg)
    assert card["message_id"] == 5 and card["rich_message"].html.startswith("<h3>✏️ Поправил")
    assert rf._texts(card["reply_markup"])[0] == "⏪ Вернуть: рис"


async def test_старшая_правка_не_ложится_поверх_свежей() -> None:
    """Тап в виджете и ответ текстом правят одно сообщение задачами: если собранное раньше дошло
    до правки позже, оно пропускается по ревизии — открытая потом карточка не врёт (аудит 10.11)."""
    wmeal._rv.clear()
    bot = Bot()
    new, old = json.dumps({"v": 1, "rv": 200}), json.dumps({"v": 1, "rv": 100})
    assert await wmeal._edit(bot, 5, 77, new, None, None)  # type: ignore[arg-type]
    assert await wmeal._edit(bot, 5, 77, old, None, None)  # type: ignore[arg-type]
    edits = [c for c in bot.calls if isinstance(c, EditMessageWidgetData)]
    assert [json.loads(e.chat_data)["rv"] for e in edits] == [200]


async def test_тап_по_записи_открывает_её_карточку(world) -> None:  # noqa: ANN001
    """D-69: `o:<запись>` — карточка этой записи следом, шапка «Запись», подпись «✏️ Запись: …»
    (не «Записал» — MA-INV-70); ничего не пишет; двойной тап — одна карточка; чужая — `gone`.
    Правка в открытой карточке сохраняет «✏️ Запись»."""
    wmeal._opened.clear()
    assistant, u, meal = await world()
    out = await wmeal.on_open(5, 1, f"o:{meal.id}:o1o1", u, assistant)  # type: ignore[arg-type]
    assert out.reply == {"ok": 1}
    out.commit()  # type: ignore[misc]
    bot = Bot()
    await out.after(bot)  # type: ignore[misc]
    [sent] = bot.calls
    assert isinstance(sent, SendWidget) and sent.html == template("meal")
    assert json.loads(sent.chat_data)["hd"] == "Запись" and sent.caption is None
    again = await wmeal.on_open(5, 1, f"o:{meal.id}:o1o1", u, assistant)  # type: ignore[arg-type]
    assert again.reply == {"ok": 1} and again.after is None
    stranger = User(telegram_id=6, name="y", locale="ru", timezone="UTC", id=99)
    assert (await wmeal.on_open(6, 1, f"o:{meal.id}:o2o2", stranger, assistant)).reply == {  # type: ignore[arg-type]
        "err": "gone"}
    r = await _do(assistant, u, f"m:a:{meal.id}:{meal.items[0].id}:210:o3o3", mid=77)
    assert r["hd"] == "Запись"


async def test_правка_прямо_в_сегодня(world) -> None:  # noqa: ANN001
    """D-74: `s:a` — вес позиции тем же `adjust_meal_item`, ответ — данные дня (`live.build`);
    `s:x` — удалить, `s:r` — вернуть; повтор ключа не пишет дважды; кривое — `bad`."""
    wmeal._undo.clear()
    assistant, u, meal = await world()
    rice = meal.items[1]
    out = await wmeal.on_today(5, 300, f"s:a:{meal.id}:{rice.id}:200:t1t1", u, assistant)  # type: ignore[arg-type]
    assert out.reply["ok"] == 1 and out.reply["z"] == 0 and "it" in out.reply
    out.commit()  # type: ignore[misc]
    got = await assistant.get_meal(u, meal.id)
    assert got is not None and got.items[1].grams == 200
    again = await wmeal.on_today(5, 300, f"s:a:{meal.id}:{rice.id}:200:t1t1", u, assistant)  # type: ignore[arg-type]
    assert again.reply == out.reply
    for bad in (f"s:a:{meal.id}:{rice.id}:0:t4t4", f"s:a:{meal.id}:999:200:t5t5", "s:q:t6t6"):
        assert (await wmeal.on_today(5, 300, bad, u, assistant)).reply["err"] == "bad", bad  # type: ignore[arg-type]
    gone = await wmeal.on_today(5, 300, f"s:x:{meal.id}:t2t2", u, assistant)  # type: ignore[arg-type]
    assert gone.reply["z"] == 1
    gone.commit()  # type: ignore[misc]
    assert await assistant.get_meal(u, meal.id) is None
    back = await wmeal.on_today(5, 300, "s:r:t3t3", u, assistant)  # type: ignore[arg-type]
    assert back.reply["ok"] == 1 and back.reply["z"] == 0
