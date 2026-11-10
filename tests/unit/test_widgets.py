"""HTML-виджеты (FR-49, W-INV-1…8, AC-269…AC-275). Только фейки: без БД, без Telegram.

Стережём то, что в проде не видно до рендера: бюджет шаблона и данных (Telegram отказывает
молча для пользователя — ошибкой в лог), профиль 1 (недопустимое вырезается без ошибки),
границу доверия `widget_query` и «один живой виджет на день».
"""
from __future__ import annotations

import json
import re
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.application.entities import Meal, MealItem, User, WeightLog
from app.application.results import DailyStats, DayPace, WeightTrend
from app.bot.middlewares import WidgetQueryMiddleware
from app.bot.ui.widgets import LIMIT, today_data
from app.bot.widgets import live, template
from app.bot.widgets.transport import AnswerWidgetQuery, EditMessageWidgetData, SendWidget
from app.domain.goals import Goal, remaining
from app.domain.streak import Streak
from app.domain.value_objects import Macros

WIDGETS = Path(live.__file__).parent
GOAL = Goal(daily_kcal=2200, protein_g=183, fat_g=80, carb_g=200)
TZ = "Etc/GMT-7"
NOW = datetime(2026, 11, 10, 7, 20, tzinfo=UTC)  # 14:20 у владельца
OWNER = User(telegram_id=351362942, name="D", locale="ru", timezone=TZ, role="owner", id=1,
             ui_prefs={"day_start": 4})
OTHER = User(telegram_id=357309245, name="E", locale="ru", timezone=TZ, role="user", id=2)


def _meal(at: datetime, name: str, kcal: float, *, est: bool = False, n: int = 1) -> Meal:
    items = [
        MealItem(name_snapshot=f"{name} {i}" if n > 1 else name, grams=100,
                 macros=Macros(kcal / n, kcal / n / 20, kcal / n / 30, kcal / n / 8),
                 is_estimate=est, id=i)
        for i in range(n)
    ]
    tot = Macros(kcal, kcal / 20, kcal / 30, kcal / 8)
    return Meal(user_id=1, eaten_at=at, input_type="text", raw_input=name, items=items,
                source_confidence=1.0, totals=tot, id=int(at.timestamp()) % 100000)


def _stats(meals: list[Meal]) -> DailyStats:
    c = Macros(sum(m.totals.kcal for m in meals), sum(m.totals.protein for m in meals),
               sum(m.totals.fat for m in meals), sum(m.totals.carb for m in meals))
    return DailyStats(remaining(c, GOAL), len(meals), meals, TZ)


def _worst_day() -> list[Meal]:
    """20 записей с 05:00 до 23:00 у человека, имена по 99 символов, кириллица (AC-269)."""
    start = datetime(2026, 11, 9, 22, 0, tzinfo=UTC)  # 05:00 10.11 по Etc/GMT-7
    long = "Очень длинное название блюда домашнего приготовления с соусом и гарниром " * 2
    return [
        _meal(start + timedelta(minutes=57 * i), long[:99], 180 + i, est=i % 2 == 0, n=3)
        for i in range(20)
    ]


PACE = DayPace(show=False, hour=14, norm_kcal=1240, today_kcal=990, days_used=16,
               norm=[0, 0, 0, 0, 300, 450, 450, 600, 900, 1240, 1240, 1300, 1500, 1500, 1700,
                     1900, 2100, 2150, 2150, 2150, 2150, 2150, 2150, 2150])


# --- W-INV-3: бюджет и профиль --------------------------------------------------------------

@pytest.mark.parametrize("path", sorted(WIDGETS.glob("*.html")), ids=lambda p: p.name)
def test_шаблон_в_бюджете(path: Path) -> None:
    raw = path.read_bytes()
    assert len(raw) <= 32768
    assert len(zlib.compress(raw, 9)) <= 4096, "W-INV-3: шаблон больше 4 КБ после zlib-9"
    assert b"\x00" not in raw


_OUT_OF_PROFILE = [
    r"<input", r"<select", r"<textarea", r"<form", r"\d+vh\b", r"oklch\(", r"color-mix\(",
    r":has\(", r"clip-path", r"text-shadow", r"src=[\"']?https?:", r"@import", r"@font-face",
    r"position:\s*sticky", r"overflow:\s*(auto|scroll)", r"\bfetch\(", r"XMLHttpRequest",
    r"-webkit-line-clamp", r"<foreignObject", r"<animate",
    # D-56: рантайм виджета не обязан их иметь — без них «Сегодня» показывал бы 0 навсегда
    r"requestAnimationFrame", r"setTimeout", r"setInterval",
]


@pytest.mark.parametrize("path", sorted(WIDGETS.glob("*.html")), ids=lambda p: p.name)
def test_шаблон_в_профиле_1(path: Path) -> None:
    """Недопустимое Telegram молча вырезает — ловим до рендера (§6 промпта, W-INV-7)."""
    html = path.read_text()
    for pat in _OUT_OF_PROFILE:
        assert not re.search(pat, html), f"вне профиля 1: {pat}"


def test_данные_худшего_дня_в_бюджете() -> None:
    """AC-269: 20 записей, имена по 99 символов — ≤ 1024 байт, все часы на месте, «ещё N»:
    записей в часах больше, чем в ленте, — виджет пишет «ещё N — в приложении»."""
    meals = _worst_day()
    text = today_data(_stats(meals), GOAL, 91.5, PACE, Streak(127, True, 28, 30), NOW, 4)
    assert len(text.encode()) <= LIMIT
    d = json.loads(text)
    assert sum(n for _, _, n in d["h"]) == 20 and len(d["it"]) < 20
    assert abs(sum(k for _, k, _ in d["h"]) - sum(m.totals.kcal for m in meals)) <= len(d["h"])
    assert "x" not in d and "w" not in d  # мёртвые ключи не возвращаются
    assert "n" in d and len(d["n"]) == 24
    assert all(len(row[2]) <= 22 for row in d["it"])


def test_данные_обычного_дня() -> None:
    """Числа — посчитанные Python'ом (W-INV-1): съедено, цель, ≈-доля, час от начала суток."""
    meals = [
        _meal(datetime(2026, 11, 10, 2, 12, tzinfo=UTC), "Овсянка", 420, est=True),  # 09:12
        _meal(datetime(2026, 11, 10, 6, 5, tzinfo=UTC), "Курица", 700),  # 13:05
    ]
    d = json.loads(today_data(_stats(meals), GOAL, 91.5, PACE, Streak(3, False, 3, 3), NOW, 4))
    assert d["d"] == "2026-11-10" and d["dl"] == "вт, 10 ноя" and d["at"] == "14:20"
    assert d["hn"] == 10  # 14:00 − начало суток 4:00
    assert d["g"] == [2200, 183, 80, 200] and d["e"][0] == 1120
    assert d["es"] == 38  # 420 из 1120 — оценкой
    assert d["h"] == [[5, 420, 1], [9, 700, 1]]
    assert d["nk"] == 990  # «сейчас» кривой — число Python (`DayPace.today_kcal`), W-INV-1
    # свежие первыми; граммы и Б/Ж/У записи — Python (W-INV-1), под строкой «100 г · Б35 Ж23 У88»
    assert d["it"][0] == [9, "05", "Курица", 700, 0, 100, 35, 23, 88]
    assert d["it"][1] == [5, "12", "Овсянка", 420, 1, 100, 21, 14, 52]
    assert d["s"] == [3, 0, 3, 3] and d["pk"] == round(d["e"][1] / 91.5, 1)


def test_без_нормы_нет_кривой_и_серии() -> None:
    """MA-INV-37: нет пяти дошедших дней — `n` нет; серия 0 — пилюли нет."""
    empty = DayPace(show=False, hour=9, norm_kcal=0, today_kcal=0, days_used=2)
    d = json.loads(today_data(_stats([]), GOAL, None, empty, Streak(0, False), NOW, 4))
    assert "n" not in d and "s" not in d and "pk" not in d and d["it"] == [] and d["h"] == []


async def test_подпись_дня_без_цифр(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-290, D-60: подпись не повторяет цифры виджета — ни на обычном, ни на пустом дне."""
    monkeypatch.setattr(live, "enabled", lambda u, s=None: True)
    for meals in ([], [_meal(datetime(2026, 11, 10, 2, 0, tzinfo=UTC), "Овсянка", 1626)]):
        bot = FakeBot()
        await live.show_today(bot, 351362942, OWNER, FakeAssistant(meals))  # type: ignore[arg-type]
        assert bot.of(SendWidget)[0].caption == "📊 Сегодня"


# --- флаг (D-51) -----------------------------------------------------------------------------

def test_флаг_раскатки() -> None:
    assert live.enabled(OWNER, "owner") and not live.enabled(OTHER, "owner")
    assert live.enabled(OTHER, "owner, 357309245") and live.enabled(OTHER, "all")
    assert not live.enabled(OWNER, "") and not live.enabled(OWNER, "357309245")


# --- граница доверия widget_query (W-INV-4, AC-270) ------------------------------------------

def _q(data: object = "t:2026-11-10", **kw: object) -> dict:
    q = {"id": "q1", "from": {"id": 351362942, "is_bot": False, "first_name": "D"},
         "chat": {"id": 351362942, "type": "private"}, "message_id": 77, "data": data}
    q.update(kw)
    return q


def test_разбор_запроса_строгий() -> None:
    assert live.parse_query(_q()) == (351362942, 77, "2026-11-10")
    for bad in (
        _q("x"), _q("t:2026-13-99"), _q("t:2026-11-10 "), _q("t:" + "1" * 63), _q(None),
        _q(chat={"id": -100, "type": "group"}),
        _q(chat={"id": 357309245, "type": "private"}),  # чужой чат
        _q(**{"from": {"id": 351362942, "is_bot": True}}),
        _q(message_id=None), _q(message_id=True),
    ):
        assert live.parse_query(bad) is None, bad


class FakeBot:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[object] = []
        self.fail = fail
        self.deleted: list[int] = []
        self.captions: list[str] = []

    async def __call__(self, method: object) -> object:
        if self.fail:
            raise RuntimeError("sendWidget: 400")
        self.calls.append(method)
        if isinstance(method, SendWidget):
            return SimpleNamespace(message_id=100 + len(self.calls))
        return True

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append(message_id)

    async def edit_message_caption(self, **kw: object) -> None:
        self.captions.append(str(kw["caption"]))

    def of(self, kind: type) -> list:
        return [c for c in self.calls if isinstance(c, kind)]


class FakeAssistant:
    """Ровно то, что читает `live.build`: те же методы Assistant, что и rich-экран."""

    def __init__(self, meals: list[Meal]) -> None:
        self.meals = meals

    async def today(self, user: User) -> DailyStats:
        return _stats(self.meals)

    async def current_goal(self, user: User) -> Goal:
        return GOAL

    async def weight_trend(self, user: User, limit: int = 14) -> WeightTrend:
        return WeightTrend([WeightLog(1, NOW, 91.5)], TZ)

    async def day_pace(self, user: User) -> DayPace:
        return PACE

    async def streak(self, user: User) -> Streak:
        return Streak(12, False, 28, 30)


async def _ask(bot: object, q: dict, user: User, assistant: object) -> None:
    """Запрос виджета тем же путём, что в проде: outer-middleware с фейковым контейнером."""
    from aiogram.types import Update

    mw = WidgetQueryMiddleware(_container(assistant=assistant, user=user))  # type: ignore[arg-type]
    upd = Update.model_validate({"update_id": 99, "widget_query": q})
    await mw(lambda e, d: None, upd, {"bot": bot})  # type: ignore[arg-type]


def _open(assistant: object):  # noqa: ANN202
    """Открывалка сессии для `live.push`: в тестах — сразу готовый фейковый ассистент."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def open_():  # noqa: ANN202
        yield assistant

    return open_


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    live._LIVE.clear()
    live._LOCKS.clear()  # замок привязывается к циклу событий, а у каждого теста он свой
    real = live.build

    async def fixed(assistant, user, now=None):  # noqa: ANN001, ANN202
        return await real(assistant, user, NOW)

    monkeypatch.setattr(live, "build", fixed)


async def test_чужой_и_без_флага_отказ(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-270: человек без флага / кривой запрос — `err`, данные не читаются, правки нет."""
    real = live.enabled
    monkeypatch.setattr(live, "enabled", lambda u, s=None: real(u, "owner"))
    bot = FakeBot()
    await _ask(bot, _q("x"), OWNER, FakeAssistant([]))
    await _ask(bot, _q(), OTHER, FakeAssistant([]))
    answers = bot.of(AnswerWidgetQuery)
    assert [json.loads(a.data) for a in answers] == [{"err": "bad"}, {"err": "bad"}]
    assert not bot.of(EditMessageWidgetData)


async def test_show_today_и_повтор_в_тот_же_день(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-271, D-47: виджет с подписью и кнопкой; повтор — новый внизу, прежний удалён."""
    monkeypatch.setattr(live, "enabled", lambda u, s=None: True)
    bot = FakeBot()
    a = FakeAssistant([_meal(datetime(2026, 11, 10, 2, 0, tzinfo=UTC), "Овсянка", 420)])
    assert await live.show_today(bot, 351362942, OWNER, a)  # type: ignore[arg-type]
    sent = bot.of(SendWidget)[0]
    assert sent.allow_queries and sent.caption == "📊 Сегодня"
    assert sent.html == template("today") and json.loads(sent.chat_data)["e"][0] == 420
    first = live._LIVE[351362942].message_id
    assert await live.show_today(bot, 351362942, OWNER, a)  # type: ignore[arg-type]
    assert bot.deleted == [first] and live._LIVE[351362942].message_id != first


async def test_фолбэк_при_ошибке_sendWidget() -> None:
    """AC-272: sendWidget упал — False, вызывающий шлёт rich."""
    bot = FakeBot(fail=True)
    ok = await live.show_today(bot, 1, OWNER, FakeAssistant([]))  # type: ignore[arg-type]
    assert ok is False and 351362942 not in live._LIVE


async def test_пуш_после_записи_и_тишина_без_изменений(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-273: запись изменила день — правка данных, подпись не трогается (D-60); ничего не
    изменилось — правки нет."""
    monkeypatch.setattr(live, "enabled", lambda u, s=None: True)
    bot = FakeBot()
    a = FakeAssistant([_meal(datetime(2026, 11, 10, 2, 0, tzinfo=UTC), "Овсянка", 420)])
    await live.show_today(bot, 351362942, OWNER, a)  # type: ignore[arg-type]
    await live.push(bot, OWNER, _open(a))  # type: ignore[arg-type]
    assert not bot.of(EditMessageWidgetData) and not bot.captions
    a.meals.append(_meal(datetime(2026, 11, 10, 6, 0, tzinfo=UTC), "Курица", 700))
    await live.push(bot, OWNER, _open(a))  # type: ignore[arg-type]
    edit = bot.of(EditMessageWidgetData)
    assert len(edit) == 1 and json.loads(edit[0].chat_data)["e"][0] == 1120
    assert edit[0].message_id == live._LIVE[351362942].message_id
    assert not bot.captions  # W-INV-2: в подписи нет цифр — править нечего


async def test_тяга_свежий_вчерашний_старший(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-274: свежий — данные дня; вчерашний и старший того же дня — снимок без правки."""
    monkeypatch.setattr(live, "enabled", lambda u, s=None: True)
    bot = FakeBot()
    a = FakeAssistant([_meal(datetime(2026, 11, 10, 2, 0, tzinfo=UTC), "Овсянка", 420)])
    # После рестарта бот виджета не помнит — запрос учит его id и правит данные.
    await _ask(bot, _q(message_id=90), OWNER, a)
    r = json.loads(bot.of(AnswerWidgetQuery)[-1].data)
    assert r["v"] == 1 and r["e"][0] == 420 and live._LIVE[351362942].message_id == 90
    assert len(bot.of(EditMessageWidgetData)) == 1
    await _ask(bot, _q("t:2026-11-09", message_id=60), OWNER, a)
    assert json.loads(bot.of(AnswerWidgetQuery)[-1].data) == {"st": 1}
    await _ask(bot, _q(message_id=80), OWNER, a)
    assert json.loads(bot.of(AnswerWidgetQuery)[-1].data) == {"st": 2}
    assert len(bot.of(EditMessageWidgetData)) == 1  # снимки не правятся
    # Новее известного (виджет, отправленный до рестарта и показанный позже) — он и живой.
    await _ask(bot, _q(message_id=95), OWNER, a)
    assert json.loads(bot.of(AnswerWidgetQuery)[-1].data)["v"] == 1
    assert live._LIVE[351362942].message_id == 95


async def test_пропавший_виджет_забывается(monkeypatch: pytest.MonkeyPatch) -> None:
    """Виджет удалили руками — бот забывает его, а не бьётся в API на каждом апдейте."""
    from aiogram.exceptions import TelegramBadRequest

    monkeypatch.setattr(live, "enabled", lambda u, s=None: True)
    bot = FakeBot()
    a = FakeAssistant([])
    await live.show_today(bot, 351362942, OWNER, a)  # type: ignore[arg-type]

    async def gone(method: object) -> object:
        raise TelegramBadRequest(method=method, message="Bad Request: message to edit not found")  # type: ignore[arg-type]

    monkeypatch.setattr(FakeBot, "__call__", lambda self, m: gone(m))
    a.meals.append(_meal(datetime(2026, 11, 10, 6, 0, tzinfo=UTC), "Курица", 700))
    await live.push(bot, OWNER, _open(a))  # type: ignore[arg-type]
    assert not live.watching(OWNER)


async def test_сбой_сборки_всё_равно_ответ(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без ответа виджет ждал бы 15 с (TIMEOUT): сбой БД — ответ `err`."""
    monkeypatch.setattr(live, "enabled", lambda u, s=None: True)

    async def boom(*a: object, **k: object) -> object:
        raise RuntimeError("db down")

    monkeypatch.setattr(live, "build", boom)
    bot = FakeBot()
    await _ask(bot, _q(), OWNER, FakeAssistant([]))
    assert json.loads(bot.of(AnswerWidgetQuery)[0].data) == {"err": "fail"}


def test_транспорт_сериализуется() -> None:
    """Имена методов и поля — как в Bot API 10.4 (ошибка тут выключила бы виджет только в проде)."""
    from app.bot.keyboards import open_in_app

    m = SendWidget(chat_id=1, html="<b>x</b>", chat_data="{}", caption="c", allow_queries=True,
                   reply_markup=open_in_app("day"))
    assert m.__api_method__ == "sendWidget"
    dump = m.model_dump(exclude_none=True)
    assert dump["allow_queries"] is True and dump["chat_data"] == "{}"
    if dump.get("reply_markup") is not None:
        assert "inline_keyboard" in dump["reply_markup"]
    assert EditMessageWidgetData(chat_id=1, message_id=2, chat_data="{}").__api_method__ == (
        "editMessageWidgetData")
    assert AnswerWidgetQuery(widget_query_id="q", data="{}").__api_method__ == "answerWidgetQuery"


async def test_handlers_today_виджет_или_rich(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-271/AC-272 на уровне хендлера: флаг и успех — только виджет; иначе — rich-экран."""
    from app.bot import handlers

    rich: list[str] = []

    async def fake_rich(message: object, html: str, markup: object = None) -> None:
        rich.append(html)

    async def fake_gather(assistant: object, user: object, scans: object = None) -> object:
        return "ctx"

    monkeypatch.setattr(handlers, "_rich", fake_rich)
    monkeypatch.setattr(handlers, "gather", fake_gather)
    monkeypatch.setattr(handlers.screens, "today", lambda ctx: "RICH")
    msg = SimpleNamespace(bot=object(), chat=SimpleNamespace(id=351362942))
    sent: list[int] = []

    async def ok(bot: object, chat_id: int, user: User, assistant: object) -> bool:
        sent.append(chat_id)
        return True

    async def fail(bot: object, chat_id: int, user: User, assistant: object) -> bool:
        return False

    monkeypatch.setattr(handlers.widgets_live, "show_today", ok)
    monkeypatch.setattr(handlers.widgets_live, "enabled", lambda u, s=None: u.role == "owner")
    await handlers._show_today(msg, OWNER, None)  # type: ignore[arg-type]
    assert sent == [351362942] and rich == []
    await handlers._show_today(msg, OTHER, None)  # type: ignore[arg-type]
    assert rich == ["RICH"]  # второму пользователю — прежний экран
    monkeypatch.setattr(handlers.widgets_live, "show_today", fail)
    await handlers._show_today(msg, OWNER, None)  # type: ignore[arg-type]
    assert rich == ["RICH", "RICH"]  # фолбэк


async def test_build_на_настоящем_ассистенте(fake_container) -> None:  # noqa: ANN001
    """Вся цепочка `live.build` на настоящих сервисах (фейковые репозитории): норма темпа
    приходит целиком при ≥ 5 дошедших днях, серия считается, числа совпадают с `today()`."""
    from tests.e2e.test_api_pace import _history

    assistant, users = fake_container.assistant_for(None)
    u = await users.get_or_create(telegram_id=11, name="x", locale="ru", timezone="Europe/Moscow")
    _history(fake_container.meals, u.id, days=8, kcal_per_day=2000)
    built = await live.build(assistant, u)
    d = json.loads(built.data)
    assert len(d["n"]) == 24 and d["n"][-1] == 2000
    assert d["s"][:2] == [8, 0]  # восемь дней подряд; сегодня ещё пусто — не рвёт и не считается
    today = await assistant.today(u)
    assert d["l"][0] == round(today.remaining.kcal_left)
    thin = await users.get_or_create(telegram_id=12, name="y", locale="ru",
                                     timezone="Europe/Moscow")
    _history(fake_container.meals, thin.id, days=2, kcal_per_day=2000)
    assert "n" not in json.loads((await live.build(assistant, thin)).data)  # MA-INV-37


async def test_middleware_ловит_widget_query() -> None:
    """D-45: апдейт с `widget_query` не доходит до роутера aiogram, остальные — доходят."""
    from aiogram.types import Update

    seen: list[object] = []

    async def handler(event: object, data: dict) -> str:
        seen.append(event)
        return "routed"

    mw = WidgetQueryMiddleware(container=None)  # type: ignore[arg-type]
    bot = FakeBot()
    upd = Update.model_validate({"update_id": 1, "widget_query": _q("bad")})
    assert await mw(handler, upd, {"bot": bot}) is None and not seen
    assert json.loads(bot.of(AnswerWidgetQuery)[0].data) == {"err": "bad"}
    plain = Update.model_validate({"update_id": 2})
    assert await mw(handler, plain, {"bot": bot}) == "routed"


# --- «⚡ Частое» виджетом (FR-49.2, AC-276…AC-278) ---------------------------------------------

def test_данные_частого_те_же_плитки_что_кнопки() -> None:
    """MA-INV-1: плитки — ровно кнопки FR-40; ккал — граммы × запиннутая плотность (INV-1)."""
    from app.application.entities import Product
    from app.bot.keyboards import frequent_tiles, set_key, tiles_keyboard
    from app.bot.quick import Frequent
    from app.bot.ui.widgets import frequent_data
    from app.domain.value_objects import Density

    tvorog = Product(canonical_key="tvorog", display_name="Творог 5%", id=12, is_estimate=False,
                     density=Density(121, 17, 5, 3))
    s = _meal(NOW, "Курица", 420, est=True)
    s.template_name = "Обед"
    tiles = frequent_tiles([(s, 5)], [(12, "Творог 5%", 180.0, 9)])
    markup = tiles_keyboard(tiles)
    assert markup is not None and [b.callback_data for r in markup.inline_keyboard for b in r] == [
        f"fq:{t.op}" for t in tiles]  # кнопки и плитки — из одного списка
    d = json.loads(frequent_data(Frequent(tiles, {12: tvorog}, {set_key("Обед"): s})))
    assert d["t"][0] == ["Творог 5%", "180 г", "p:12:180", 218, 0]  # 121 × 1.8 = 217.8
    assert d["t"][1] == ["Обед", "420 ккал", f"s:{set_key('Обед')}", 420, 1]
    assert len(json.dumps(d, ensure_ascii=False).encode()) <= LIMIT


@pytest.fixture
def freq(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Запись «Частого» без БД: `quick.log_frequent` подменён, запоминаем, что он получил."""
    from app.application.results import MealLogged
    from app.bot import quick
    from app.bot.widgets import frequent

    frequent._done.clear()
    frequent._wrote.clear()
    calls: list[str] = []
    meals: dict[int, Meal] = {}

    async def fake_log(user: User, assistant: object, data: str) -> MealLogged | None:
        calls.append(data)
        if data.endswith(":404:100"):
            return None
        meal = _meal(NOW, "Творог 5%", 218)
        meal.id = 500 + len(calls)
        meals[meal.id] = meal
        return MealLogged(meal=meal, remaining=remaining(meal.totals, GOAL))

    monkeypatch.setattr(quick, "log_frequent", fake_log)
    return frequent, calls, meals


async def _run(fn, uid: int, data: str, user: User, a: object) -> dict:  # noqa: ANN001
    """Как `_route`: обработчик, затем «фиксация прошла» — commit-колбэк."""
    out = await fn(uid, 7, data, user, a)
    if out.commit is not None:
        out.commit()
    return out.reply


async def test_частое_пишет_один_раз_на_тап(freq) -> None:  # noqa: ANN001
    """AC-276/AC-277: запись тем же путём, что кнопка; повтор того же ключа не пишет."""
    frequent, calls, _ = freq
    a = FakeAssistant([])
    w = frequent.on_write
    r = await _run(w, 1, "w:p:12:180:k3x9a1", OWNER, a)
    assert r["ok"] == 1 and r["k"] == 218 and r["l"] == 2200 - 218 and calls == ["p:12:180"]
    assert await _run(w, 1, "w:p:12:180:k3x9a1", OWNER, a) == r and len(calls) == 1  # W-INV-5
    assert (await _run(w, 1, "w:p:12:180:zz11yy", OWNER, a))["ok"] == 1 and len(calls) == 2
    for bad in ("w:p:12:0:abcd", "w:p:12:9999:abcd", "w:p:x:1:abcd", "w:p:12:180",
                "w:s:zz:abcd", "w:p:12:180:AB", "x:p:12:180:abcd"):
        assert (await _run(w, 1, bad, OWNER, a))["err"] == "bad", bad
    assert (await _run(w, 1, "w:p:404:100:qqqq", OWNER, a))["err"] == "gone"
    assert len(calls) == 3  # отказы не писали (404 дошёл до use-case и получил None)
    assert (await _run(w, 1, "w:p:404:100:qqqq", OWNER, a))["err"] == "gone"  # ключ не залип


async def test_частое_параллельный_повтор_пишет_раз(freq) -> None:  # noqa: ANN001
    """W-INV-5 под гонкой: два одинаковых запроса задачами — одна запись."""
    import asyncio

    frequent, calls, _ = freq
    a = FakeAssistant([])
    r1, r2 = await asyncio.gather(
        frequent.on_write(1, 7, "w:p:12:180:race01", OWNER, a),
        frequent.on_write(1, 7, "w:p:12:180:race01", OWNER, a),
    )
    assert len(calls) == 1 and {r1.reply.get("err"), r2.reply.get("err")} >= {"busy"}


async def test_частое_вернуть_только_своё(freq, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """AC-278: «Вернуть» — только запись этого виджета этим человеком, без правок, за 10 минут."""
    from app.application.results import MealDeleted

    frequent, _, meals = freq
    a = FakeAssistant([])
    deleted: list[int] = []

    async def delete_meal(user: User, meal_id: int) -> MealDeleted:
        deleted.append(meal_id)
        return MealDeleted(meal=meals.pop(meal_id, None))

    async def get_meal(user: User, meal_id: int) -> Meal | None:
        return meals.get(meal_id)

    a.delete_meal = delete_meal  # type: ignore[attr-defined]
    a.get_meal = get_meal  # type: ignore[attr-defined]
    u = frequent.on_undo
    r = await _run(frequent.on_write, 1, "w:p:12:180:aaaa", OWNER, a)
    assert (await _run(u, 2, f"u:{r['m']}:bbbb", OTHER, a))["err"] == "bad"  # чужой
    assert (await _run(u, 1, "u:999:cccc", OWNER, a))["err"] == "bad"  # не из виджета
    ok = await _run(u, 1, f"u:{r['m']}:dddd", OWNER, a)
    assert ok["ok"] == 1 and deleted == [r["m"]]
    assert (await _run(u, 1, f"u:{r['m']}:eeee", OWNER, a))["err"] == "bad"  # уже вернули
    r2 = await _run(frequent.on_write, 1, "w:p:12:180:ffff", OWNER, a)
    meals[r2["m"]].items[0].grams = 250  # поправили вес — «Вернуть» не снесёт правку
    assert (await _run(u, 1, f"u:{r2['m']}:gggg", OWNER, a))["err"] == "edited"
    r3 = await _run(frequent.on_write, 1, "w:p:12:180:hhhh", OWNER, a)
    monkeypatch.setattr(frequent, "_now", lambda: 10**9)  # прошло > 10 минут
    assert (await _run(u, 1, f"u:{r3['m']}:iiii", OWNER, a))["err"] == "bad"
    assert deleted == [r["m"]]


async def test_маршрут_пишущего_запроса(freq, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """Middleware: `w:` — в «Частое», фиксация, ответ, пуш виджета дня; без флага — отказ."""
    from contextlib import asynccontextmanager

    frequent, calls, _ = freq
    committed: list[bool] = []
    pushed: list[int] = []
    fail_commit: list[bool] = []

    class Session:
        async def commit(self) -> None:
            if fail_commit:
                raise RuntimeError("commit failed")
            committed.append(True)

    class Users:
        async def get_or_create(self, **kw: object) -> User:
            return OWNER if kw["telegram_id"] == OWNER.telegram_id else OTHER

    @asynccontextmanager
    async def factory():  # noqa: ANN202
        yield Session()

    container = SimpleNamespace(
        db=SimpleNamespace(session_factory=factory),
        assistant_for=lambda s: (FakeAssistant([]), Users()),
        settings=SimpleNamespace(default_timezone=TZ),
    )

    async def fake_push(bot: object, user: User, assistant: object) -> None:
        pushed.append(user.telegram_id)

    real = live.enabled
    monkeypatch.setattr(live, "enabled", lambda u, s=None: real(u, "owner"))
    monkeypatch.setattr(live, "push", fake_push)
    from aiogram.types import Update

    mw = WidgetQueryMiddleware(container)  # type: ignore[arg-type]
    bot = FakeBot()

    async def handler(event: object, data: dict) -> None:
        raise AssertionError("widget_query не должен идти в роутер")

    upd = Update.model_validate({"update_id": 3, "widget_query": _q("w:p:12:180:abcd")})
    await mw(handler, upd, {"bot": bot})
    assert json.loads(bot.of(AnswerWidgetQuery)[-1].data)["ok"] == 1
    assert committed == [True] and pushed == [351362942] and calls == ["p:12:180"]
    other = _q("w:p:12:180:efgh", **{"from": {"id": 357309245}},
               chat={"id": 357309245, "type": "private"})
    await mw(handler, Update.model_validate({"update_id": 4, "widget_query": other}), {"bot": bot})
    assert json.loads(bot.of(AnswerWidgetQuery)[-1].data) == {"err": "bad"} and len(calls) == 1
    # Фиксация упала — ответ не «записал», и тот же ключ потом пишет заново (B1 ревью).
    fail_commit.append(True)
    q3 = _q("w:p:12:180:zzzz")
    await mw(handler, Update.model_validate({"update_id": 5, "widget_query": q3}), {"bot": bot})
    assert json.loads(bot.of(AnswerWidgetQuery)[-1].data) == {"err": "fail"} and len(pushed) == 1
    fail_commit.clear()
    await mw(handler, Update.model_validate({"update_id": 6, "widget_query": q3}), {"bot": bot})
    assert json.loads(bot.of(AnswerWidgetQuery)[-1].data)["ok"] == 1 and len(calls) == 3


# --- «⚖️ Вес» виджетом (FR-49.3, AC-279) -------------------------------------------------------

def test_вес_данные_те_же_числа_что_текст() -> None:
    """AC-279: точки — сами замеры, Δ — `weight_change` (те же числа, что в тексте экрана)."""
    from app.bot.ui.widgets import weight_data
    from app.domain.weight_change import weight_change

    t0 = datetime(2026, 9, 28, 1, 30, tzinfo=UTC)  # 08:30 по Etc/GMT-7
    pts = [(t0 + timedelta(days=1.4 * i), 86.8 - 0.05 * i) for i in range(30)]
    d = json.loads(weight_data(pts, TZ, "cut") or "{}")
    ch = weight_change(pts)
    assert ch is not None and len(d["p"]) == 30 and d["p"][0] == [0.0, 86.8, 0]
    assert d["dp"] == ch.period_kg and d["dm"] == ch.month_kg and d["l"] == round(ch.last_kg, 1)
    assert d["st"] == "ok" and d["a0"] == "2026-09-28" and "h0" not in d
    assert weight_data(pts[:1], TZ, "cut") is None  # один замер — графика нет (FR-8)
    up = json.loads(weight_data(list(reversed([(t, w) for t, w in pts])), TZ, "cut") or "{}")
    assert up["st"] in ("ok", "bad")
    maint = json.loads(weight_data(pts, TZ, "maintain") or "{}")
    assert maint["st"] == "mute"


def test_вес_дата_точки_считает_python() -> None:
    """Дата под тапом по точке — местный день замера из Python (как «📋 Замеры» rich-экрана),
    а не `floor(h0/24 + x)` в JS: x округлён до 0,1 дня, и замер у полуночи уезжал на соседний
    день."""
    from app.bot.ui.widgets import weight_data

    t0 = datetime(2026, 10, 1, 1, 30, tzinfo=UTC)  # 08:30 1.10 по Etc/GMT-7
    late = datetime(2026, 10, 2, 16, 57, tzinfo=UTC)  # 23:57 2.10
    early = datetime(2026, 10, 2, 17, 4, tzinfo=UTC)  # 00:04 3.10: x 1,65 → 1,6 → у JS «2 окт»
    d = json.loads(weight_data([(t0, 86.0), (late, 85.8), (early, 85.7)], TZ, "cut") or "{}")
    assert [p[2] for p in d["p"]] == [0, 1, 2]
    assert "D.h0" not in template("weight") and "dt(q[2])" in template("weight")


def test_вес_с_прошлого_замера() -> None:
    """AC-291: «с прошлого» — последний − предпоследний замер, числом Python (W-INV-1): тот же
    Δ, что писал текст «Вес записан» (`delta_kg`), а не арифметика в шаблоне."""
    from app.bot.ui.widgets import weight_data

    t0 = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
    pts = [(t0, 86.0), (t0 + timedelta(days=3), 85.7), (t0 + timedelta(days=5), 85.3)]
    d = json.loads(weight_data(pts, TZ, "cut") or "{}")
    assert d["dd"] == -0.4 and d["dp"] == -0.7
    html = template("weight")
    assert "с прошлого" in html and "D.dd" in html
    assert "id=dp" not in html and ">последний<" not in html  # дубли «за период» и героя


def test_вес_120_замеров_в_бюджете() -> None:
    """AC-279: длинная история — данные ≤ 1024 байт, Δ «за период» — по показанным замерам."""
    from app.bot.ui.widgets import weight_data
    from app.domain.weight_change import weight_change

    t0 = datetime(2026, 5, 1, 1, 0, tzinfo=UTC)
    pts = [(t0 + timedelta(days=i * 1.5), 90.0 - 0.031 * i) for i in range(120)]
    text = weight_data(pts, TZ, "cut") or ""
    d = json.loads(text)
    assert len(text.encode()) <= LIMIT and 2 <= len(d["p"]) < 120
    shown = pts[-len(d["p"]):]
    ch = weight_change(shown)
    assert ch is not None and d["dp"] == ch.period_kg


async def test_вес_виджет_или_png(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-49.3/D-55: виджет ушёл — True; sendWidget упал — False (хендлер шлёт PNG)."""
    t0 = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
    pts = [(t0 + timedelta(days=i), 86.0 - 0.1 * i) for i in range(10)]
    bot = FakeBot()
    assert await live.show_weight(bot, 1, pts, TZ, "cut")  # type: ignore[arg-type]
    sent = bot.of(SendWidget)[0]
    assert sent.html == template("weight") and sent.caption == "⚖️ Вес"  # AC-290: без цифр
    assert not await live.show_weight(FakeBot(fail=True), 1, pts, TZ, "cut")  # type: ignore[arg-type]
    assert not await live.show_weight(bot, 1, pts[:1], TZ, "cut")  # type: ignore[arg-type]


# --- ревью 10.11: гонка, подпись, смена дня, сбой запроса, хуки (Step 1) ---------------------

async def test_not_modified_не_ошибка(monkeypatch: pytest.MonkeyPatch) -> None:
    """«message is not modified» у данных — запоминаем нарисованное, виджет живой (AC-281)."""
    from aiogram.exceptions import TelegramBadRequest

    monkeypatch.setattr(live, "enabled", lambda u, s=None: True)

    class Same(FakeBot):
        async def __call__(self, method: object) -> object:
            if isinstance(method, EditMessageWidgetData):
                self.calls.append(method)
                raise TelegramBadRequest(  # type: ignore[arg-type]
                    method=method, message="Bad Request: message is not modified")
            return await super().__call__(method)

    bot = Same()
    a = FakeAssistant([])
    await live.show_today(bot, 351362942, OWNER, a)  # type: ignore[arg-type]
    a.meals.append(_meal(datetime(2026, 11, 10, 6, 0, tzinfo=UTC), "Курица", 700))
    await live.push(bot, OWNER, _open(a))  # type: ignore[arg-type]
    state = live._LIVE[351362942]
    assert json.loads(state.key)["e"][0] == 700
    await live.push(bot, OWNER, _open(a))  # ничего не изменилось — правок больше нет
    assert len(bot.of(EditMessageWidgetData)) == 1 and not bot.captions


async def test_гонка_пушей_старые_цифры_не_ложатся_поверх(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Два апдейта задачами: первый собрал данные ДО записи и застрял, второй — после. Без замка
    первый правил бы последним и вернул старые цифры; под замком последней ложится свежая."""
    import asyncio

    monkeypatch.setattr(live, "enabled", lambda u, s=None: True)
    bot = FakeBot()
    gate = asyncio.Event()

    class Slow(FakeAssistant):
        calls = 0

        async def today(self, user: User) -> DailyStats:
            self.calls += 1
            snap = _stats(list(self.meals))  # снимок базы на момент чтения
            if self.calls == 2:  # первая сборка пуша (1-я — показ)
                await gate.wait()
            return snap

    a = Slow([])
    await live.show_today(bot, 351362942, OWNER, a)  # type: ignore[arg-type]
    a.meals.append(_meal(datetime(2026, 11, 10, 6, 0, tzinfo=UTC), "Курица", 700))
    first = asyncio.create_task(live.push(bot, OWNER, _open(a)))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    a.meals.append(_meal(datetime(2026, 11, 10, 6, 30, tzinfo=UTC), "Рис", 300))
    second = asyncio.create_task(live.push(bot, OWNER, _open(a)))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(first, second)
    edits = bot.of(EditMessageWidgetData)
    assert json.loads(edits[-1].chat_data)["e"][0] == 1000


async def test_новый_день_вчерашний_виджет_забыт(monkeypatch: pytest.MonkeyPatch) -> None:
    """После полуночи вчерашний виджет — снимок: пуш его забывает, а не собирает день впустую."""
    monkeypatch.setattr(live, "enabled", lambda u, s=None: True)
    bot = FakeBot()
    a = FakeAssistant([])
    await live.show_today(bot, 351362942, OWNER, a)  # type: ignore[arg-type]
    live._LIVE[351362942].day = "2026-11-09"
    await live.push(bot, OWNER, _open(a))  # type: ignore[arg-type]
    assert not live.watching(OWNER) and not bot.of(EditMessageWidgetData)


def _container(
    fail: bool = False, assistant: object | None = None, user: User = OWNER
) -> SimpleNamespace:
    from contextlib import asynccontextmanager

    class Session:
        async def commit(self) -> None: ...
        async def rollback(self) -> None: ...

    class Users:
        async def get_or_create(self, **kw: object) -> User:
            if fail:
                raise RuntimeError("db down")
            return user

    @asynccontextmanager
    async def factory():  # noqa: ANN202
        yield Session()

    return SimpleNamespace(
        db=SimpleNamespace(session_factory=factory),
        assistant_for=lambda s: (assistant or FakeAssistant([]), Users()),
        body_scans_for=lambda s: None,
        settings=SimpleNamespace(default_timezone=TZ),
    )


async def test_запрос_дня_сбой_бд_всё_равно_ответ(monkeypatch: pytest.MonkeyPatch) -> None:
    """`t:` при упавшем get_or_create — ответ `err:fail`, а не 15 с тишины (TIMEOUT)."""
    from aiogram.types import Update

    monkeypatch.setattr(live, "enabled", lambda u, s=None: True)
    bot = FakeBot()
    mw = WidgetQueryMiddleware(_container(fail=True))  # type: ignore[arg-type]
    upd = Update.model_validate({"update_id": 9, "widget_query": _q()})
    await mw(lambda e, d: None, upd, {"bot": bot})  # type: ignore[arg-type]
    assert json.loads(bot.of(AnswerWidgetQuery)[0].data) == {"err": "fail"}
    ok = WidgetQueryMiddleware(_container())  # type: ignore[arg-type]
    await ok(lambda e, d: None, Update.model_validate({"update_id": 10, "widget_query": _q()}),  # type: ignore[arg-type]
             {"bot": bot})
    assert json.loads(bot.of(AnswerWidgetQuery)[1].data)["v"] == 1
    assert len(bot.of(EditMessageWidgetData)) == 1  # после рестарта: выучил id и поправил


async def test_хук_пуша_в_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    """ServicesMiddleware: после апдейта — пуш; хендлер сам показал «Сегодня» — пуша нет."""
    from app.bot.middlewares import ServicesMiddleware

    monkeypatch.setattr(live, "enabled", lambda u, s=None: True)
    pushed: list[int] = []

    async def fake_push(bot: object, user: User, opener: object) -> None:
        pushed.append(user.telegram_id)

    monkeypatch.setattr(live, "push", fake_push)
    mw = ServicesMiddleware(_container())  # type: ignore[arg-type]
    tg = SimpleNamespace(id=OWNER.telegram_id, full_name="D", language_code="ru")
    data = {"event_from_user": tg, "bot": FakeBot()}

    async def plain(event: object, d: dict) -> str:
        return "ok"

    assert await mw(plain, object(), dict(data)) == "ok" and pushed == []  # виджета нет
    live._LIVE[OWNER.telegram_id] = live._Live("2026-11-10", 5, "")
    await mw(plain, object(), dict(data))
    assert pushed == [OWNER.telegram_id]

    async def shows(event: object, d: dict) -> None:
        await live.show_today(d["bot"], OWNER.telegram_id, OWNER, FakeAssistant([]))  # type: ignore[arg-type]

    await mw(shows, object(), dict(data))
    assert pushed == [OWNER.telegram_id]  # показал сам — без второй сборки


async def test_частое_вернуть_чужой_виджет_и_пропавшая_запись(freq) -> None:  # noqa: ANN001
    """«Вернуть» — только из того виджета, что записал (FR-49.2); записи нет — `gone`."""
    frequent, _, meals = freq
    a = FakeAssistant([])

    async def get_meal(user: User, meal_id: int) -> Meal | None:
        return meals.get(meal_id)

    a.get_meal = get_meal  # type: ignore[attr-defined]
    r = await _run(frequent.on_write, 1, "w:p:12:180:mmmm", OWNER, a)
    other = await frequent.on_undo(1, 8, f"u:{r['m']}:nnnn", OWNER, a)  # другой виджет
    assert other.reply == {"err": "bad"}
    meals.pop(r["m"])  # удалили в «Сегодня» / аппке
    assert (await _run(frequent.on_undo, 1, f"u:{r['m']}:oooo", OWNER, a)) == {"err": "gone"}


class _FreqAssistant(FakeAssistant):
    def __init__(self, products: list) -> None:  # noqa: ANN001
        super().__init__([])
        self.products = {p.id: p for p in products}

    async def recent_sets(self, user: User, days: int = 30) -> list:
        return []

    async def frequent(self, user: User, *, days: int = 30, limit: int = 10) -> list:
        return [(p.id, p.display_name, 180.0, 5) for p in self.products.values()]

    async def get_product(self, pid: int):  # noqa: ANN201
        return self.products.get(pid)


async def test_частое_виджет_и_фолбэк_на_клавиатуру(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-49.2: с флагом — виджет без клавиатуры; виджет не ушёл или пусто — прежние кнопки."""
    from unittest.mock import AsyncMock

    from app.application.entities import Product
    from app.bot import handlers
    from app.bot.widgets import frequent
    from app.domain.value_objects import Density

    long = Product(canonical_key="p", display_name="Протеиновый батончик шоколадный с орехами",
                   id=3, is_estimate=True, density=Density(400, 30, 15, 35))
    a = _FreqAssistant([long])
    bot = FakeBot()
    assert await frequent.show(bot, 1, OWNER, a)  # type: ignore[arg-type]
    sent = bot.of(SendWidget)[0]
    assert sent.caption == "⚡ Частое"  # AC-290: «тап — запишу сразу» — уже в шапке виджета
    d = json.loads(sent.chat_data)
    assert d["t"][0][2] == "p:3:180" and d["t"][0][3] == 720 and d["t"][0][4] == 1
    assert not await frequent.show(FakeBot(fail=True), 1, OWNER, a)  # type: ignore[arg-type]
    assert not await frequent.show(FakeBot(), 1, OWNER, _FreqAssistant([]))  # type: ignore[arg-type]

    monkeypatch.setattr(handlers.widgets_live, "enabled", lambda u, s=None: True)
    msg = AsyncMock()
    msg.chat = SimpleNamespace(id=1)
    msg.bot = FakeBot()
    await handlers.btn_frequent(msg, OWNER, a, AsyncMock())  # type: ignore[arg-type]
    assert not msg.answer.await_count and msg.bot.of(SendWidget)
    msg.bot = FakeBot(fail=True)
    await handlers.btn_frequent(msg, OWNER, a, AsyncMock())  # type: ignore[arg-type]
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert [b.callback_data for r in kb.inline_keyboard for b in r] == ["fq:p:3:180"]


async def test_вес_хендлеры_виджет_или_png(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-49.3, AC-291, D-60: «⚖️ Вес» и запись веса с флагом — ОДИН виджет: ни rich-экрана, ни
    текста «Вес записан» рядом. Виджет не ушёл — rich/текст без картинки (PNG снят, D-63)."""
    from unittest.mock import AsyncMock

    from app.application.results import WeightLogged
    from app.bot import handlers

    t0 = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
    pts = [(t0 + timedelta(days=i), 86.0 - 0.1 * i) for i in range(5)]
    ctx = SimpleNamespace(weight_points=pts, weight=SimpleNamespace(timezone=TZ),
                          goal=SimpleNamespace(target="cut"))
    rich: list[str] = []

    async def fake_gather(assistant: object, user: object, scans: object = None) -> object:
        return ctx

    async def fake_rich(message: object, html: str, markup: object = None) -> None:
        rich.append(html)

    monkeypatch.setattr(handlers, "gather", fake_gather)
    monkeypatch.setattr(handlers, "_rich", fake_rich)
    monkeypatch.setattr(handlers.screens, "weight", lambda c: "RICH")
    monkeypatch.setattr(handlers.widgets_live, "enabled", lambda u, s=None: True)
    sent: list[bool] = [True]
    captions: list[str] = []

    async def show(*a: object, caption: str = "⚖️ Вес") -> bool:
        captions.append(caption)
        return sent[0]

    monkeypatch.setattr(handlers.widgets_live, "show_weight", show)
    msg = AsyncMock()
    msg.chat = SimpleNamespace(id=1)
    await handlers._show_weight(msg, OWNER, None)  # type: ignore[arg-type]
    assert not msg.answer_photo.await_count and rich == []  # rich перед виджетом — дубль
    sent[0] = False
    await handlers._show_weight(msg, OWNER, None)  # type: ignore[arg-type]
    assert not msg.answer_photo.await_count and rich == ["RICH"]

    class A:
        async def weight_trend(self, user: User, limit: int = 14) -> WeightTrend:
            return WeightTrend([WeightLog(1, t, w) for t, w in reversed(pts)], TZ)

        async def current_goal(self, user: User) -> Goal:
            return GOAL

    logged = WeightLogged(WeightLog(1, NOW, 85.6), -0.1)
    msg = AsyncMock()
    msg.chat = SimpleNamespace(id=1)
    sent[0] = True
    await handlers._reply_screen(msg, logged, OWNER, A())  # type: ignore[arg-type]
    assert not msg.answer.await_count and not msg.answer_photo.await_count
    assert captions[-1] == "⚖️ Вес записан"
    sent[0] = False
    await handlers._reply_screen(msg, logged, OWNER, A())  # type: ignore[arg-type]
    assert msg.answer.await_count == 1 and not msg.answer_photo.await_count


async def test_запись_веса_текстом_один_график(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-291: «вес 85.2» текстом — ответ ОДИН. С 15.07 график слали и `_reply_screen`, и хендлер
    текста (`_maybe_weight_chart` дважды): два PNG, с флагом — два виджета веса."""
    from unittest.mock import AsyncMock

    from aiogram.methods import SendMessage, SendPhoto

    from app.application.dto import Intent, ParsedMessage
    from app.bot import handlers
    from tests.unit.test_bot_reply_fix import _plain, _world

    monkeypatch.setattr(handlers.widgets_live, "enabled", lambda u, s=None: True)
    w = await _world()
    for kg in (86.0, 85.6):
        await w.assistant._weights.log(w.user, kg)
    w.nlu.queue = [ParsedMessage(intent=Intent.WEIGHT, weight_kg=85.2)]
    msg = _plain("вес 85.2")
    await handlers.on_text(msg, msg.bot, w.user, w.assistant, AsyncMock(), w.state)
    sent = [c.args[0] for c in msg.bot.await_args_list]
    assert len([c for c in sent if isinstance(c, SendWidget)]) == 1
    assert not [c for c in sent if isinstance(c, SendPhoto | SendMessage)]


async def test_ответ_на_t_не_ждёт_замка(monkeypatch: pytest.MonkeyPatch) -> None:
    """Медленная правка чужого апдейта держит замок — ответ виджету уходит не дожидаясь (15 с)."""
    import asyncio

    from aiogram.types import Update

    monkeypatch.setattr(live, "enabled", lambda u, s=None: True)
    bot = FakeBot()
    mw = WidgetQueryMiddleware(_container())  # type: ignore[arg-type]
    held = live.lock(OWNER.telegram_id)
    await held.acquire()
    upd = Update.model_validate({"update_id": 11, "widget_query": _q()})
    task = asyncio.create_task(mw(lambda e, d: None, upd, {"bot": bot}))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    assert json.loads(bot.of(AnswerWidgetQuery)[0].data)["v"] == 1 and not task.done()
    held.release()
    await task
    assert len(bot.of(EditMessageWidgetData)) == 1  # правка — после, под замком


# --- серия записанных дней (D-57, AC-275) ------------------------------------------------------

async def test_серия_по_записям_и_закрытым_дням(fake_container) -> None:  # noqa: ANN001
    """D-57: день засчитан, если есть хоть одна запись (порога калорий нет) или он закрыт
    «✅ всё записал»; дни до первой записи — не пропуски; длинная серия не режется окном."""
    from tests.e2e.test_api_pace import _history

    assistant, users = fake_container.assistant_for(None)
    u = await users.get_or_create(telegram_id=21, name="x", locale="ru", timezone="Europe/Moscow")
    _history(fake_container.meals, u.id, days=3, kcal_per_day=150)  # мало ккал — всё равно день
    s = await assistant.streak(u)
    assert (s.days, s.frozen, s.covered, s.of) == (3, False, 3, 3)  # новичок: без ❄
    from app.domain.prefs import day_start
    from app.domain.timeframe import local_date

    today = local_date(datetime.now(UTC), u.timezone, day_start(u.ui_prefs))
    assert await assistant.close_day(u, today) is True  # закрыл пустой сегодня — засчитан
    assert await assistant.close_day(u, today) is False  # повтор ничего не пишет
    assert await assistant.close_day(u, today + timedelta(days=1)) is False  # будущее — нет
    assert (await assistant.streak(u)).days == 4
    long = await users.get_or_create(telegram_id=22, name="y", locale="ru",
                                     timezone="Europe/Moscow")
    _history(fake_container.meals, long.id, days=90, kcal_per_day=2000)
    s = await assistant.streak(long)
    assert s.days == 90 and (s.covered, s.of) == (29, 29)  # окно 30, сегодня ещё пусто


def test_серия_без_истории_пилюли_нет() -> None:
    """Ни одной записи — ни серии, ни покрытия: ключа `s` в данных нет."""
    empty = DayPace(show=False, hour=9, norm_kcal=0, today_kcal=0, days_used=0)
    d = json.loads(today_data(_stats([]), GOAL, None, empty, Streak(0, False), NOW, 4))
    assert "s" not in d
    d = json.loads(today_data(_stats([]), GOAL, None, empty, Streak(0, True, 20, 29), NOW, 4))
    assert d["s"] == [0, 1, 20, 29]  # серия порвалась, покрытие осталось


@pytest.mark.parametrize("path", sorted(WIDGETS.glob("*.html")), ids=lambda p: p.name)
def test_скрипт_шаблона_разбирается(path: Path, tmp_path: Path) -> None:
    """Синтаксическая ошибка в `<script>` убивает виджет целиком, а Python-тесты её не видят:
    повторное `const` в шаблоне уже доходило до проверки. `node --check` — где есть node (CI)."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node нет — синтаксис шаблона проверяет CI")
    m = re.search(r"<script>(.*)</script>", path.read_text(), re.S)
    assert m is not None
    js = tmp_path / f"{path.stem}.js"
    js.write_text(m.group(1))
    out = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


# --- «📈 Неделя» виджетом (FR-49.5, AC-287) ---------------------------------------------------

@pytest.mark.parametrize("name", ["пятница в плане", "утро среды"])
async def test_неделя_те_же_числа_что_экран(name: str) -> None:
    """AC-287: семь дней, клетки и «в цели K из M» — те же, что у rich-экрана; КБЖУ дня — из
    `macros7`; данные ≤ 1024 байт; sendWidget упал — False (rich)."""
    from app.bot.ui import screens
    from app.bot.widgets import week
    from tests.unit.test_ui_widths import WEEK

    ctx = WEEK[name]
    bot = FakeBot()
    assert await week.show(bot, 1, ctx)  # type: ignore[arg-type]
    sent = bot.of(SendWidget)[0]
    assert sent.html == template("week") and len(sent.chat_data.encode()) <= LIMIT
    assert sent.caption == "📈 Неделя"  # AC-290: герой и средние — в виджете
    d = json.loads(sent.chat_data)
    v = screens.week_view(ctx)
    assert not isinstance(v, str)
    assert len(d["c"]) == 7 and d["k"] == list(screens.week_hits(v))
    assert [c[1] for c in d["c"]] == [{"none": 0, "ok": 1, "low": 2, "over": 3}[x] for x in v.cells]
    rows = dict(ctx.macros7)
    for c, day in zip(d["c"], v.days, strict=True):
        if day in rows:
            assert c[2] == round(rows[day].kcal)
    assert f"в цели {d['k'][0]} из {d['k'][1]}" in screens.week(ctx)
    assert not await week.show(FakeBot(fail=True), 1, ctx)  # type: ignore[arg-type]


# --- аудит 10.11: линтер шаблонов и гонка показа ---------------------------------------------

@pytest.mark.parametrize("path", sorted(WIDGETS.glob("*.html")), ids=lambda p: p.name)
def test_шаблон_вызовы_моста_в_try_и_полосы_не_минус(path: Path) -> None:
    """`W.request`/`W.ready` — в `try` (исключение не оставит виджет «занятым», D-56); ширина
    полосы — не меньше нуля (отрицательная width невалидна → полоса во всю ширину); число и
    единица — через неразрывный пробел."""
    html = path.read_text()
    for m in re.finditer(r"W\.(request|ready)\b", html):
        if html[m.start() - 3:m.start()] in ("!W.", "(W."):  # проверка наличия, не вызов
            continue
        before = html[max(0, m.start() - 40):m.start()]
        call = html[m.end():m.end() + 2]
        if "(" not in call and "&&" not in call:
            continue
        assert "try{" in before, f"{path.name}: W.{m.group(1)} вне try"
    for m in re.finditer(r"Math\.min\(100,", html):
        assert html[m.start() - 11:m.start()] == "Math.max(0,", f"{path.name}: полоса без max(0)"
    assert not re.search(r"' (г|ккал|кг)\b", html), f"{path.name}: единица оторвётся от числа"


def _builders() -> dict[str, str]:
    """`chat_data` каждого виджета на данных тестов — для сверки ключей с шаблоном."""
    from app.application.entities import Product
    from app.bot.keyboards import frequent_tiles
    from app.bot.quick import Frequent
    from app.bot.ui.screens import week_view
    from app.bot.ui.widgets import evening_data, frequent_data, meal_data, week_data, weight_data
    from app.domain.value_objects import Density
    from tests.unit.test_ui_widths import CTXS, WEEK

    meals = [_meal(datetime(2026, 11, 10, 2, 0, tzinfo=UTC), "Овсянка", 420, est=True, n=2)]
    t0 = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
    prod = Product(canonical_key="p", display_name="Творог", id=3, density=Density(121, 17, 5, 3))
    ctx = WEEK["пятница в плане"]
    v = week_view(ctx)
    assert not isinstance(v, str)
    m = meals[0]
    m.id = 9
    return {
        "today": today_data(_stats(meals), GOAL, 91.5, PACE, Streak(3, True, 3, 3), NOW, 4),
        "weight": weight_data([(t0 + timedelta(days=i), 86 - i / 10) for i in range(30)], TZ,
                              "cut") or "",
        "meal": meal_data(m, _stats(meals).remaining, TZ, session_kcal=900, undo=1) or "",
        "week": week_data(v, dict(ctx.macros7), ctx.days[-1][0]),
        "evening": evening_data(CTXS["обычный день"], NOW.date(), Streak(3, False, 3, 3)),
        "frequent": frequent_data(Frequent(frequent_tiles([], [(3, "Творог", 180.0, 9)]),
                                           {3: prod}, {})),
    }


@pytest.mark.parametrize("name", ["today", "weight", "meal", "week", "evening", "frequent"])
def test_ключи_данных_читает_шаблон(name: str) -> None:
    """Ключ `chat_data`, которого шаблон не читает, — мёртвый вес в бюджете 1024 байта и ложное
    обещание (так жил `cap` у недели). `v` — версия формата, её читают не все."""
    html = template(name)
    from app.bot.ui.widgets import merged_data

    keys = set(json.loads(_builders()[name])) - {"v"}
    if name == "meal":  # шапка и след «вошло в набор» (FR-47) — того же шаблона
        keys |= set(json.loads(merged_data("бургер"))) - {"v"}
    dead = {k for k in keys if not re.search(rf"\b[dDr]\.{k}\b", html)}
    assert not dead, f"{name}: шаблон не читает {sorted(dead)}"


async def test_повторный_сегодня_удаляет_прежний_даже_если_t_успел(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-47 под гонкой: новый виджет шлёт `t:` сразу, ответ на него (без замка) учит `_LIVE` его
    id ещё до того, как `show_today` дочитал ответ `sendWidget`. Прежний всё равно удаляется."""
    monkeypatch.setattr(live, "enabled", lambda u, s=None: True)

    class Racy(FakeBot):
        async def __call__(self, method: object) -> object:
            msg = await super().__call__(method)
            if isinstance(method, SendWidget):
                live._LIVE[OWNER.telegram_id] = live._Live("2026-11-10", msg.message_id, "")
            return msg

    bot = Racy()
    a = FakeAssistant([])
    await live.show_today(bot, 351362942, OWNER, a)  # type: ignore[arg-type]
    first = live._LIVE[351362942].message_id
    await live.show_today(bot, 351362942, OWNER, a)  # type: ignore[arg-type]
    assert bot.deleted == [first]


def test_неделя_тысячи_одним_видом() -> None:
    """FR-48: «1 124» в герое (Python, `kit.k`) и в столбиках/средних (JS) — один и тот же узкий
    неразрывный пробел, а не два разных."""
    from app.bot.ui.kit import NB

    assert NB == "\u202f" and "'\\u202f'" in template("week")


def test_плитка_частого_граммы_не_отрываются() -> None:
    """Хвост «180 г» — общий с текстом кнопки FR-40; неразрывный пробел ставит шаблон."""
    assert "T(x[1]).replace(' ','\\u00a0')" in template("frequent")


@pytest.mark.parametrize("path", sorted(WIDGETS.glob("*.html")), ids=lambda p: p.name)
def test_анимация_только_css_и_при_появлении(path: Path) -> None:
    """D-64: анимация — CSS под классом `an`, который ставится только первым показом; счётчику
    свойство регистрирует JS в `try` (нет `registerProperty` — число сразу финальное, D-56);
    `prefers-reduced-motion` её снимает."""
    html = path.read_text()
    assert "AN=()=>{document.body.className=A1?'an':'';A1=0}" in html
    assert "try{CSS.registerProperty(" in html
    assert re.search(r"prefers-reduced-motion:reduce\)\{[^}]*\.an \*\{animation:none", html)
    assert ".an " in html.split("</style>")[0]


def test_сегодня_одинаковый_ответ_не_перерисовывает() -> None:
    """Тяга `t:` приходит через долю секунды после показа: тот же день не должен перерисовать
    героя и оборвать счёт (D-64)."""
    assert "sm=K==k" in template("today") and "if(sm)return;" in template("today")


def test_сегодня_тап_по_часу_не_меняет_высоту() -> None:
    """Тап по часу менял число строк ленты — виджет менял высоту, и лента чата прыгала. Теперь
    списки всех часов лежат в одной ячейке сетки, виден один: высота — по самому длинному."""
    html = template("today")
    assert "#ls{display:grid;grid-template-columns:minmax(0,1fr)}" in html
    assert "#ls>div{grid-area:1/1;visibility:hidden}#ls>.on{visibility:visible}" in html
    assert "$('ls').innerHTML=hs.map(" in html


@pytest.mark.parametrize("path", sorted(WIDGETS.glob("*.html")), ids=lambda p: p.name)
def test_низ_под_значок_telegram(path: Path) -> None:
    """В правом нижнем углу виджета Telegram рисует свой значок ↗ — он закрывал последнее число.
    Нижний отступ — не меньше 26 px."""
    assert "max(26px,env(safe-area-inset-bottom))" in path.read_text()
