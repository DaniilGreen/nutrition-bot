"""«🗒 История» виджетом (FR-49.8, AC-298/299, D-67). Фейки: без БД и без Telegram.

Держим: числа и группы — тот же `day_card`, что у rich-экрана (MA-INV-1); действия — те же
use-case (`remove_item`/`delete_meal`/`restore_meal`/`repeat_item`/`repeat_meal`); повтор ключа
не пишет дважды; чужая запись — `gone`.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.application.entities import Meal, MealItem, User
from app.bot.ui.kit import macros_rows, pct
from app.bot.ui.widgets import LIMIT
from app.bot.widgets import day, template
from app.bot.widgets.transport import EditMessageWidgetData, SendWidget
from app.domain.value_objects import Macros


def _meal(uid: int, ago: timedelta, *items: tuple[str, float, float]) -> Meal:
    its = [MealItem(name_snapshot=n, grams=g, macros=Macros(k, k / 10, k / 30, k / 8),
                    product_id=i + 1, is_estimate=True) for i, (n, g, k) in enumerate(items)]
    tot = Macros(*(sum(getattr(i.macros, f) for i in its)
                   for f in ("kcal", "protein", "fat", "carb")))
    return Meal(user_id=uid, eaten_at=datetime.now(UTC) - ago, input_type="text", raw_input="—",
                items=its, source_confidence=0.9, totals=tot)


@pytest.fixture
def world(fake_container):  # noqa: ANN001, ANN201
    day._undo.clear()
    day._done.clear()

    async def make() -> tuple[object, User, list[Meal]]:
        assistant, users = fake_container.assistant_for(None)
        u = await users.get_or_create(telegram_id=5, name="x", locale="ru", timezone="UTC")
        cheese = await fake_container.meals.add(
            _meal(u.id, timedelta(minutes=20), ("сыр сливочный", 28, 95)))
        eggs = await fake_container.meals.add(
            _meal(u.id, timedelta(minutes=12), ("яйца жареные", 230, 451), ("хлеб", 30, 75)))
        old = await fake_container.meals.add(
            _meal(u.id, timedelta(days=1, minutes=5), ("творог", 180, 218)))
        return assistant, u, [cheese, eggs, old]

    return make


async def _do(assistant: object, u: User, data: str) -> dict:
    out = await day.on_day(5, 77, data, u, assistant)  # type: ignore[arg-type]
    if out.reply.get("ok") and out.commit is not None:
        out.commit()
    return out.reply


async def test_день_те_же_числа_и_группы_что_rich(world) -> None:  # noqa: ANN001
    """AC-298: приём пищи из двух записей — шапкой; у позиции граммы и Б/Ж/У; строки дня —
    `macros_rows` от того же `day_card`; ключи данных читает шаблон."""
    assistant, u, (cheese, eggs, _) = await world()
    rem, _dv = await assistant.day_card(u, 0)
    d = await _do(assistant, u, "h:d:0")
    assert d["da"] == 0 and d["t"].startswith("Сегодня")
    assert d["dy"] == [[round(c), round(g), pct(c, g)] for _, c, g in
                       macros_rows(rem.consumed, rem.goal)]
    assert len(d["g"]) == 1 and d["g"][0][1] == 621
    rows = {r[3]: r for r in d["e"]}
    assert rows["сыр сливочный"][4:9] == [28, 95, 10, 3, 12] and rows["яйца жареные"][11] == 1
    assert all(r[10] == 0 for r in d["e"])  # все три позиции — в одном приёме пищи
    html = template("day")
    for k in d:
        assert k == "v" or re.search(rf"\bd\.{k}\b", html), k


async def test_убрать_вернуть_и_повтор_ключа(world) -> None:  # noqa: ANN001
    """AC-299: 🗑 позиции — `remove_item`, «⏪ Вернуть» — как было; повтор ключа не убирает
    дважды."""
    assistant, u, (_, eggs, _) = await world()
    bread = eggs.items[1]
    r = await _do(assistant, u, f"h:x:0:{eggs.id}:{bread.id}:k1k1")
    assert r["ok"] == 1 and r["z"] == 1 and "хлеб" not in [x[3] for x in r["e"]]
    assert await _do(assistant, u, f"h:x:0:{eggs.id}:{bread.id}:k1k1") == r
    back = await _do(assistant, u, "h:r:0:r1r1")
    assert back["ok"] == 1 and back["z"] == 0 and "хлеб" in [x[3] for x in back["e"]]
    assert (await _do(assistant, u, "h:r:0:r2r2"))["err"] == "old"


async def test_вся_запись_и_последняя_позиция(world) -> None:  # noqa: ANN001
    assistant, u, (cheese, eggs, _) = await world()
    r = await _do(assistant, u, f"h:x:0:{cheese.id}:{cheese.items[0].id}:a1a1")
    assert r["ok"] == 1 and await assistant.get_meal(u, cheese.id) is None
    w = await _do(assistant, u, f"h:x:0:{eggs.id}:0:a2a2")  # позиция 0 — вся запись
    assert w["ok"] == 1 and await assistant.get_meal(u, eggs.id) is None and not w["e"]


async def test_в_сегодня_только_с_прошлого_дня(world) -> None:  # noqa: ANN001
    """FR-42 тем же `repeat_item`: с вчера — запись в сегодня, вчера не меняется; с сегодня —
    отказ (задвоил бы); чужая запись — `gone`."""
    assistant, u, (cheese, _, old) = await world()
    r = await _do(assistant, u, f"h:p:1:{old.id}:{old.items[0].id}:p1p1")
    assert r["ok"] == 1 and r["da"] == 1 and r["tm"].startswith("🔁 Записал в сегодня: творог")
    today = await _do(assistant, u, "h:d:0")
    assert "творог" in [x[3] for x in today["e"]]
    assert (await _do(assistant, u, f"h:p:0:{cheese.id}:0:p2p2"))["err"] == "bad"
    stranger = User(telegram_id=6, name="y", locale="ru", timezone="UTC", id=99)
    out = await day.on_day(  # type: ignore[arg-type]
        6, 77, f"h:x:0:{cheese.id}:0:s1s1", stranger, assistant)
    assert out.reply["err"] == "gone"
    for bad in ("h:d:999", "h:x:0:1:2", "h:q:0", "h:d:-1"):
        assert (await _do(assistant, u, bad))["err"] == "bad", bad


async def test_после_правки_сообщение_поправлено(world) -> None:  # noqa: ANN001
    assistant, u, (_, eggs, _) = await world()
    out = await day.on_day(  # type: ignore[arg-type]
        5, 77, f"h:x:0:{eggs.id}:{eggs.items[1].id}:e1e1", u, assistant)
    calls: list[object] = []

    async def bot(m: object) -> None:
        calls.append(m)

    await out.after(bot)  # type: ignore[misc, arg-type]
    [edit] = calls
    assert isinstance(edit, EditMessageWidgetData) and edit.message_id == 77
    assert len(edit.chat_data.encode()) <= LIMIT


async def test_длинный_день_в_бюджете(fake_container) -> None:  # noqa: ANN001
    """20 записей с именами по 99 символов — ≤ 1024 байт, хвост — «ещё N»."""
    from app.bot.ui.widgets import day_data

    assistant, users = fake_container.assistant_for(None)
    u = await users.get_or_create(telegram_id=7, name="z", locale="ru", timezone="UTC")
    long = ("Очень длинное название блюда домашнего приготовления с соусом " * 2)[:99]
    for i in range(20):
        await fake_container.meals.add(_meal(u.id, timedelta(minutes=5 + i * 7), (long, 150, 300)))
    rem, dv = await assistant.day_card(u, 0)
    text = day_data(dv, rem)
    d = json.loads(text)
    assert len(text.encode()) <= LIMIT and d["mo"] > 0 and len(d["e"]) + d["mo"] == 20


async def test_кнопка_история_виджет_или_rich(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    from app.bot import handlers

    monkeypatch.setattr(handlers.widgets_live, "enabled", lambda u, s=None: True)
    shown = AsyncMock(return_value=True)
    monkeypatch.setattr(handlers.widgets_day, "show", shown)
    msg = AsyncMock()
    msg.chat = SimpleNamespace(id=1)
    user = User(telegram_id=1, name="x", locale="ru", timezone="UTC", id=1)
    assistant = AsyncMock()
    await handlers._show_history(msg, user, assistant)
    assert shown.await_count == 1 and not assistant.day_card.await_count  # rich не собирался
    assert SendWidget  # тип ответа виджета — тот же транспорт
