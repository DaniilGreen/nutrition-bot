"""Подтверждение разбора одним виджетом (FR-49.7, AC-296/297, D-66). Фейки: без БД и Telegram.

Держим главное — точность: ккал предпросмотра = ккал записи (та же плотность, INV-2; та же
цепочка граммов, INV-7), размер без сигнала не угадывается (FR-21), повтор не пишет дважды.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.bot import handlers
from app.bot.widgets import confirm, template
from app.bot.widgets.transport import EditMessageWidgetData, SendWidget
from tests.unit import test_bot_reply_fix as rf

SIZES = {"small": 100.0, "medium": 150.0, "large": 200.0}


def _item(name: str, grams: float | None = None, kcal: float = 110) -> ParsedItem:
    return ParsedItem(
        name=name, canonical_key=name, unit=Unit.GRAM if grams else Unit.PORTION, grams=grams,
        portion_options=None if grams else dict(SIZES), kcal_per_100g=kcal,
        protein_per_100g=10, fat_per_100g=3, carb_per_100g=12,
    )


def _lunch(n: int = 0) -> ParsedMessage:
    items = [_item("курица", 150, 165), _item("гречка"), _item("салат", kcal=60)]
    items += [_item(f"хлеб {i}", 30, 250) for i in range(n)]
    return ParsedMessage(intent=Intent.MEAL, items=items)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    confirm._drafts.clear()
    confirm._done.clear()
    monkeypatch.setattr(handlers.widgets_live, "enabled", lambda u, s=None: True)


def _bot(msg: object) -> list[object]:
    """Ответы фейкового Telegram: у отправленного виджета — настоящий message_id."""
    async def call(method: object, *a: object, **k: object) -> object:
        return SimpleNamespace(message_id=42)

    msg.bot.side_effect = call  # type: ignore[attr-defined]
    return []


def _sent(msg: object, kind: type) -> list:
    return [c.args[0] for c in msg.bot.await_args_list if isinstance(c.args[0], kind)]  # type: ignore[attr-defined]


async def _ask(w: object, text: str = "обед курица 150, гречка, салат", n: int = 0) -> object:
    w.nlu.queue = [_lunch(n)]  # type: ignore[attr-defined]
    msg = rf._plain(text)
    _bot(msg)
    await rf._say(w, msg)
    return msg


async def _tap(w: object, data: str) -> dict:
    out = await confirm.on_confirm(1, 42, data, w.user, w.assistant)  # type: ignore[attr-defined]
    if out.reply.get("ok") and out.commit is not None:
        out.commit()
    return out.reply


async def test_один_виджет_вместо_вопросов_и_честный_предпросмотр() -> None:
    """AC-296: три позиции одним виджетом; размер не выбран; ккал/г — плотность, что запишется."""
    w = await rf._world()
    msg = await _ask(w)
    [sent] = _sent(msg, SendWidget)
    assert sent.html == template("confirm") and sent.allow_queries
    from aiogram.methods import SendMessage
    assert not _sent(msg, SendMessage)  # вопросов «Маленькая / Средняя / Большая» нет
    d = json.loads(sent.chat_data)
    names = [r[0] for r in d["i"]]
    assert names == ["курица", "гречка", "салат"]
    assert d["i"][0][1] == 150 and d["i"][0][6] == 0 and d["i"][0][5] == 0
    assert d["i"][1][1] == 0 and d["i"][1][5] == [100, 150, 200]  # FR-21: не угадываем
    assert await w.state.get_state() is None
    assert not w.meals._meals  # ничего не записано до «Записать»


async def test_записать_пишет_то_что_показал_предпросмотр() -> None:
    """AC-297: курица +20 г, гречка и салат — M; запись тем же continue_meal; ккал записи =
    предпросмотр."""
    w = await rf._world()
    msg = await _ask(w)
    d = json.loads(_sent(msg, SendWidget)[0].chat_data)
    out = await confirm.on_confirm(1, 42, "c:w:170,150,150:k1k1k1", w.user, w.assistant)
    assert out.reply == {"ok": 1}
    out.commit()  # type: ignore[misc]
    [meal] = w.meals._meals
    assert [(i.name_snapshot, i.grams) for i in meal.items] == [
        ("курица", 170), ("гречка", 150), ("салат", 150)]
    for row, it, g in zip(d["i"], meal.items, (170, 150, 150), strict=True):
        assert round(row[2] * g) == round(it.macros.kcal)  # W-INV-1: превью = запись
    bot = rf._plain("x").bot
    _bot(rf._plain("x"))
    await out.after(bot)  # type: ignore[misc]
    kinds = [type(c.args[0]).__name__ for c in bot.await_args_list]
    assert "SendWidget" in kinds and bot.delete_message.await_count == 1
    again = await _tap(w, "c:w:170,150,150:k1k1k1")
    assert again == {"ok": 1} and len(w.meals._meals) == 1  # W-INV-5: повтор не пишет
    assert (await _tap(w, "c:w:170,150,150:k2k2k2"))["err"] == "old"  # черновик израсходован


async def test_без_размера_не_пишет_и_кривое_отказ() -> None:
    w = await rf._world()
    await _ask(w)
    assert (await _tap(w, "c:w:150,0,150:n1n1"))["err"] == "need"
    for bad in ("c:w:150,150:b1b1", "c:w:150,2001,150:b2b2", "c:w:150,150,150",
                "c:z:abcd", "c:w:1,2,3,4,5,6,7,8,9:b3b3"):
        assert (await _tap(w, bad))["err"] == "bad", bad
    assert not w.meals._meals


async def test_отмена_ничего_не_пишет() -> None:
    w = await rf._world()
    await _ask(w)
    out = await confirm.on_confirm(1, 42, "c:x:x1x1", w.user, w.assistant)
    assert out.reply == {"ok": 1, "x": 1}
    out.commit()  # type: ignore[misc]
    bot = rf._plain("x").bot
    await out.after(bot)  # type: ignore[misc]
    [edit] = [c.args[0] for c in bot.await_args_list
              if isinstance(c.args[0], EditMessageWidgetData)]
    assert json.loads(edit.chat_data) == {"v": 1, "x": 1}
    assert (await _tap(w, "c:w:150,150,150:k9k9"))["err"] == "old" and not w.meals._meals


async def test_больше_четырёх_позиций_прежние_вопросы() -> None:
    """Пять позиций — выше 560 px на 320 (W-INV-8): прежняя цепочка кнопок размера."""
    from aiogram.methods import SendMessage

    w = await rf._world()
    msg = await _ask(w, n=2)
    assert not _sent(msg, SendWidget)
    assert any("Маленькая" in str(m.reply_markup) for m in _sent(msg, SendMessage))
