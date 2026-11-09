"""HTML-виджеты (FR-49, W-INV-1…8, AC-269…AC-275). Только фейки: без БД, без Telegram.

Стережём то, что в проде не видно до рендера: бюджет шаблона и данных (Telegram отказывает
молча для пользователя — ошибкой в лог), профиль 1 (недопустимое вырезается без ошибки),
границу доверия `widget_query` и «один живой виджет на день».
"""
from __future__ import annotations

import json
import re
import zlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.application.entities import Meal, MealItem, User, WeightLog
from app.application.results import DailyStats, DayPace, WeightTrend
from app.bot.ui.widgets import LIMIT, today_caption, today_data
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
]


@pytest.mark.parametrize("path", sorted(WIDGETS.glob("*.html")), ids=lambda p: p.name)
def test_шаблон_в_профиле_1(path: Path) -> None:
    """Недопустимое Telegram молча вырезает — ловим до рендера (§6 промпта, W-INV-7)."""
    html = path.read_text()
    for pat in _OUT_OF_PROFILE:
        assert not re.search(pat, html), f"вне профиля 1: {pat}"


def test_данные_худшего_дня_в_бюджете() -> None:
    """AC-269: 20 записей, имена по 99 символов — ≤ 1024 байт, все часы на месте, «ещё N»."""
    meals = _worst_day()
    text = today_data(_stats(meals), GOAL, 91.5, PACE, Streak(12, True), NOW, 4)
    assert len(text.encode()) <= LIMIT
    d = json.loads(text)
    assert d["x"] > 0 and len(d["it"]) + d["x"] == 20
    assert abs(sum(k for _, k in d["h"]) - sum(m.totals.kcal for m in meals)) <= len(d["h"])
    assert "n" in d and len(d["n"]) == 24
    assert all(len(row[2]) <= 22 for row in d["it"])


def test_данные_обычного_дня() -> None:
    """Числа — посчитанные Python'ом (W-INV-1): съедено, цель, ≈-доля, час от начала суток."""
    meals = [
        _meal(datetime(2026, 11, 10, 2, 12, tzinfo=UTC), "Овсянка", 420, est=True),  # 09:12
        _meal(datetime(2026, 11, 10, 6, 5, tzinfo=UTC), "Курица", 700),  # 13:05
    ]
    d = json.loads(today_data(_stats(meals), GOAL, 91.5, PACE, Streak(3, False), NOW, 4))
    assert d["d"] == "2026-11-10" and d["dl"] == "вт, 10 ноя" and d["at"] == "14:20"
    assert d["hn"] == 10  # 14:00 − начало суток 4:00
    assert d["g"] == [2200, 183, 80, 200] and d["e"][0] == 1120
    assert d["es"] == 38  # 420 из 1120 — оценкой
    assert d["h"] == [[5, 420], [9, 700]]
    assert d["it"][0] == [9, "05", "Курица", 700, 0]  # свежие первыми
    assert d["it"][1] == [5, "12", "Овсянка", 420, 1]
    assert d["s"] == [3, 0] and d["w"] == 91.5 and d["x"] == 0


def test_без_нормы_нет_кривой_и_серии() -> None:
    """MA-INV-37: нет пяти дошедших дней — `n` нет; серия 0 — пилюли нет."""
    empty = DayPace(show=False, hour=9, norm_kcal=0, today_kcal=0, days_used=2)
    d = json.loads(today_data(_stats([]), GOAL, None, empty, Streak(0, False), NOW, 4))
    assert "n" not in d and "s" not in d and "w" not in d and d["it"] == [] and d["h"] == []


def test_подпись() -> None:
    meals = [_meal(datetime(2026, 11, 10, 2, 0, tzinfo=UTC), "Овсянка", 1626)]
    assert today_caption(_stats(meals), GOAL) == (
        "📊 Сегодня: 1626 из 2200 ккал · осталось 574 · белок 81 из 183 г"
    )
    over = [_meal(datetime(2026, 11, 10, 2, 0, tzinfo=UTC), "Пицца", 2400)]
    assert "перебор 200" in today_caption(_stats(over), GOAL)
    assert today_caption(_stats([]), GOAL) == "📊 Сегодня: пока пусто · цель 2200 ккал"


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

    async def daily_series(self, user: User, days: int = 30):  # noqa: ANN201
        day = date(2026, 11, 10)
        rows = [(day - timedelta(days=i), Macros(1900, 1, 1, 1), 5) for i in range(days)]
        return list(reversed(rows)), GOAL


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    live._LIVE.clear()
    real = live.build

    async def fixed(assistant, user, now=None):  # noqa: ANN001, ANN202
        return await real(assistant, user, NOW)

    monkeypatch.setattr(live, "build", fixed)


async def test_чужой_и_без_флага_отказ(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-270: человек без флага / кривой запрос — `err`, данные не читаются, правки нет."""
    real = live.enabled
    monkeypatch.setattr(live, "enabled", lambda u, s=None: real(u, "owner"))
    bot = FakeBot()
    await live.answer(bot, _q("x"), OWNER, FakeAssistant([]))  # type: ignore[arg-type]
    await live.answer(bot, _q(), OTHER, FakeAssistant([]))  # type: ignore[arg-type]
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
    assert sent.allow_queries and sent.caption.startswith("📊 Сегодня: 420 из 2200")
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
    """AC-273: запись изменила день — правка данных и подписи; ничего не изменилось — правки нет."""
    monkeypatch.setattr(live, "enabled", lambda u, s=None: True)
    bot = FakeBot()
    a = FakeAssistant([_meal(datetime(2026, 11, 10, 2, 0, tzinfo=UTC), "Овсянка", 420)])
    await live.show_today(bot, 351362942, OWNER, a)  # type: ignore[arg-type]
    await live.push(bot, OWNER, a)  # type: ignore[arg-type]
    assert not bot.of(EditMessageWidgetData) and not bot.captions
    a.meals.append(_meal(datetime(2026, 11, 10, 6, 0, tzinfo=UTC), "Курица", 700))
    await live.push(bot, OWNER, a)  # type: ignore[arg-type]
    edit = bot.of(EditMessageWidgetData)
    assert len(edit) == 1 and json.loads(edit[0].chat_data)["e"][0] == 1120
    assert edit[0].message_id == live._LIVE[351362942].message_id
    assert bot.captions and "1120 из 2200" in bot.captions[-1]


async def test_тяга_свежий_вчерашний_старший(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-274: свежий — данные дня; вчерашний и старший того же дня — снимок без правки."""
    monkeypatch.setattr(live, "enabled", lambda u, s=None: True)
    bot = FakeBot()
    a = FakeAssistant([_meal(datetime(2026, 11, 10, 2, 0, tzinfo=UTC), "Овсянка", 420)])
    # После рестарта бот виджета не помнит — запрос учит его id и правит данные.
    await live.answer(bot, _q(message_id=90), OWNER, a)  # type: ignore[arg-type]
    r = json.loads(bot.of(AnswerWidgetQuery)[-1].data)
    assert r["v"] == 1 and r["e"][0] == 420 and live._LIVE[351362942].message_id == 90
    assert len(bot.of(EditMessageWidgetData)) == 1
    await live.answer(bot, _q("t:2026-11-09", message_id=60), OWNER, a)  # type: ignore[arg-type]
    assert json.loads(bot.of(AnswerWidgetQuery)[-1].data) == {"st": 1}
    await live.answer(bot, _q(message_id=80), OWNER, a)  # type: ignore[arg-type]
    assert json.loads(bot.of(AnswerWidgetQuery)[-1].data) == {"st": 2}
    assert len(bot.of(EditMessageWidgetData)) == 1  # снимки не правятся
    # Новее известного (виджет, отправленный до рестарта и показанный позже) — он и живой.
    await live.answer(bot, _q(message_id=95), OWNER, a)  # type: ignore[arg-type]
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
    await live.push(bot, OWNER, a)  # type: ignore[arg-type]
    assert not live.watching(OWNER)


async def test_сбой_сборки_всё_равно_ответ(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без ответа виджет ждал бы 15 с (TIMEOUT): сбой БД — ответ `err`."""
    monkeypatch.setattr(live, "enabled", lambda u, s=None: True)

    async def boom(*a: object, **k: object) -> object:
        raise RuntimeError("db down")

    monkeypatch.setattr(live, "build", boom)
    bot = FakeBot()
    await live.answer(bot, _q(), OWNER, FakeAssistant([]))  # type: ignore[arg-type]
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
    assert d["s"][0] == 8  # восемь полных дней подряд, сегодня ещё не прошло порог
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

    mw = live.WidgetQueryMiddleware(container=None)  # type: ignore[arg-type]
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
    from app.bot.keyboards import frequent_keyboard, set_key
    from app.bot.ui.widgets import frequent_data
    from app.domain.value_objects import Density

    tvorog = Product(canonical_key="tvorog", display_name="Творог 5%", id=12, is_estimate=False,
                     density=Density(121, 17, 5, 3))
    s = _meal(NOW, "Курица", 420, est=True)
    s.template_name = "Обед"
    markup = frequent_keyboard([(s, 5)], [(12, "Творог 5%", 180.0, 9)])
    assert markup is not None
    d = json.loads(frequent_data(markup, {12: tvorog}, {set_key("Обед"): s}))
    assert d["t"][0] == ["Творог 5%", "180 г", "p:12:180", 218, 0]  # 121 × 1.8 = 217.8
    assert d["t"][1] == ["Обед", "420 ккал", f"s:{set_key('Обед')}", 420, 1]
    assert len(json.dumps(d, ensure_ascii=False).encode()) <= LIMIT


@pytest.fixture
def freq(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Запись «Частого» без БД: `_log_frequent` подменён, запоминаем, что он получил."""
    from app.application.results import MealLogged
    from app.bot import handlers
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

    monkeypatch.setattr(handlers, "_log_frequent", fake_log)
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
    assert r["ok"] == 1 and r["k"] == 218 and r["l"] == 2200 - 218 and calls == ["fq:p:12:180"]
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

    mw = live.WidgetQueryMiddleware(container)  # type: ignore[arg-type]
    bot = FakeBot()

    async def handler(event: object, data: dict) -> None:
        raise AssertionError("widget_query не должен идти в роутер")

    upd = Update.model_validate({"update_id": 3, "widget_query": _q("w:p:12:180:abcd")})
    await mw(handler, upd, {"bot": bot})
    assert json.loads(bot.of(AnswerWidgetQuery)[-1].data)["ok"] == 1
    assert committed == [True] and pushed == [351362942] and calls == ["fq:p:12:180"]
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
