"""«📱 Открыть в приложении» — из бота сразу на экран аппки (FR-46, AC-166…AC-170).

Держим три вещи: кнопка — `web_app` с адресом кнопки меню плюс `&s=<экран>` из белого списка,
она одна и последним рядом (старые кнопки на месте), и висит ровно на пяти экранах бота.
"""
from __future__ import annotations

import re
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.entities import Product
from app.application.results import DishSaved
from app.bot import handlers
from app.bot.keyboards import (
    OPEN_APP_TEXT,
    card_meal_id,
    open_in_app,
    products_fix_keyboard,
    sex_keyboard,
)
from app.config import get_settings
from app.domain.value_objects import Density
from tests.unit.test_bot_frequent import _world
from tests.unit.test_bot_photo import _State
from tests.unit.test_bot_week_scan import SCAN, _callback

URL = re.compile(r"https://[^/?#]+/\?v=[0-9a-f]{10}&s=([a-z0-9-]+)")


def _buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def _screen(kb) -> str:
    """Экран, на который ведёт кнопка аппки: она последним рядом, одна в нём и в сообщении."""
    last = kb.inline_keyboard[-1]
    assert len(last) == 1 and last[0].text == OPEN_APP_TEXT
    assert last[0].callback_data is None and last[0].web_app is not None
    m = URL.fullmatch(last[0].web_app.url)
    assert m, last[0].web_app.url
    assert sum(b.web_app is not None for b in _buttons(kb)) == 1
    return m.group(1)


def _product(pid: int = 42, source: str = "llm") -> Product:
    return Product(canonical_key=f"p{pid}", display_name="борщ", density=Density(55, 3, 2, 6),
                   is_estimate=source == "llm", source=source, id=pid)


# --- адрес и белый список -----------------------------------------------------


def test_адрес_как_у_кнопки_меню_плюс_экран():
    kb = open_in_app("cat")
    assert _screen(kb) == "cat"
    url = kb.inline_keyboard[0][0].web_app.url
    assert url.startswith(get_settings().miniapp_url.rstrip("/") + "/?v=")
    assert len(url) < 200


@pytest.mark.parametrize("screen", [
    "day", "cat", "cat-7", "recipe-42", "dupes", "tot", "tot-week", "body", "set", "cat-1234567890",
])
def test_белый_список_проходит(screen):
    assert _screen(open_in_app(screen)) == screen


@pytest.mark.parametrize("screen", [
    "", "foo", "ai", "CAT", "cat-0", "cat-01", "cat-1e3", "recipe", "recipe-", "tot-x",
    "cat-12345678901", "cat-7&s=set", "cat-7#x", "body ", "constructor", "cat-1٣",
])
def test_вне_списка_падает_до_отправки(screen):
    with pytest.raises(ValueError):
        open_in_app(screen)


def test_кнопка_последним_рядом_старые_на_месте():
    before = products_fix_keyboard([_product(1), _product(2)])
    rows = [[b.callback_data for b in r] for r in before.inline_keyboard]
    kb = open_in_app("cat", before)
    assert [[b.callback_data for b in r] for r in kb.inline_keyboard[:-1]] == rows
    assert _screen(kb) == "cat"


def test_без_адреса_аппки_кнопки_нет(monkeypatch):
    monkeypatch.setattr(get_settings(), "miniapp_url", "")
    sex = sex_keyboard()
    assert open_in_app("set", sex) is sex
    assert open_in_app("cat") is None
    with pytest.raises(ValueError):  # белый список проверяется и без адреса
        open_in_app("foo")


def test_кнопка_аппки_не_делает_сообщение_карточкой_приёма():
    # FR-44 узнаёт карточку по callback её кнопок; у web_app-кнопки callback нет.
    assert card_meal_id(open_in_app("cat")) is None


# --- экраны бота --------------------------------------------------------------


@pytest.mark.parametrize("found", [[_product(1)], []])
async def test_products_ведёт_в_каталог(found):
    assistant = AsyncMock()
    assistant.fixable_products.return_value = found
    msg = AsyncMock()
    await handlers.cmd_products(msg, SimpleNamespace(args="тофу"), object(), assistant)
    kb = msg.bot.send_rich_message.await_args.kwargs["reply_markup"]
    assert _screen(kb) == "cat"
    assert len(_buttons(kb)) == len(found) + 1  # кнопки правки продуктов остались


@pytest.mark.parametrize("entry", ["button", "command"])
async def test_неделя_ведёт_в_итоги_за_неделю(entry):
    assistant, user, _, _ = await _world()
    msg = AsyncMock()
    if entry == "button":
        await handlers.btn_week(msg, user, assistant, _State(), None)
    else:
        await handlers.cmd_week(msg, user, assistant, None)
    assert _screen(msg.bot.send_rich_message.await_args.kwargs["reply_markup"]) == "tot-week"


@pytest.mark.parametrize("entry", [handlers.btn_profile, handlers.cmd_profile])
async def test_профиль_ведёт_в_настройки_вопрос_на_месте(entry):
    msg = AsyncMock()
    await entry(msg, _State())
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert _screen(kb) == "set"
    assert [b.callback_data for b in _buttons(kb)[:-1]] == [
        "prof_sex:male", "prof_sex:female", "wiz_cancel"
    ]


async def test_визард_первого_запуска_без_кнопки_аппки():
    cb = _callback("onb:setup")
    await handlers.on_onboard_setup(cb, SimpleNamespace(id=1), AsyncMock(), _State())
    asked = [c.args[0] for c in cb.bot.await_args_list
             if "Твой пол?" in (getattr(c.args[0], "text", None) or "")]
    assert len(asked) == 1
    kb = asked[0].reply_markup
    assert [b.callback_data for b in _buttons(kb)] == [
        "prof_sex:male", "prof_sex:female", "wiz_cancel"
    ]
    assert all(b.web_app is None for b in _buttons(kb))


async def test_рецепт_ведёт_на_свой_состав():
    msg = AsyncMock()
    await handlers._reply_screen(msg, DishSaved(product=_product(42, "recipe")), object(), object())
    assert "Рецепт" in msg.answer.await_args.args[0]
    assert _screen(msg.answer.await_args.kwargs["reply_markup"]) == "recipe-42"


async def test_замер_ведёт_на_состав_тела():
    scans = AsyncMock()
    scans.save.return_value = (replace(SCAN, id=1), False)
    scans.overview.return_value = SimpleNamespace(proposal=None)
    cb = _callback("scan:save")
    await handlers.on_scan_btn(cb, object(), AsyncMock(), _State({"scan": SCAN}), scans)
    last = cb.bot.await_args_list[-1].args[0]
    assert "Замер сохранён" in last.text
    assert _screen(last.reply_markup) == "body"


async def test_замер_с_новыми_целями_применить_первым(monkeypatch):
    scans = AsyncMock()
    scans.save.return_value = (replace(SCAN, id=1), True)
    scans.overview.return_value = SimpleNamespace(proposal=SimpleNamespace(changed=True))
    monkeypatch.setattr(handlers, "format_goal_proposal", lambda _p: "Цели по замеру")
    cb = _callback("scan:save")
    await handlers.on_scan_btn(cb, object(), AsyncMock(), _State({"scan": SCAN}), scans)
    last = cb.bot.await_args_list[-1].args[0]
    assert "Замер обновлён" in last.text
    assert _buttons(last.reply_markup)[0].callback_data == "scan:apply"
    assert _screen(last.reply_markup) == "body"
