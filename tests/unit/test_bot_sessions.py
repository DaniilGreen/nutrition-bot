"""«Приём пищи» в боте (FR-47 «Бот», AC-188…AC-195, AC-204…AC-211, D-MA-29).

Держим: блок суммы приёма пищи на карточке — только когда записей в нём больше одной, и на любой
карточке (✅, ✏️ Вес, ⏪, ответ FR-44); «📌 Приём пищи — в набор» сливает ровно то, что названо в
вопросе об имени, а вопрос не глотает еду; изменившийся за это время приём пищи — отказ без
слияния с кнопкой «Сохранить как «имя»»; карточки слитых записей ничего не пишут; экран дня рисует
приём пищи шапкой с вложенными позициями (и плоско, если Telegram отверг), у каждой позиции Б/Ж/У;
«⏪ Вернуть» после слияния отказывает (FR-41). Время записей — фиксированный прошлый день: граница
дня не должна зависеть от часа запуска.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message
from aiogram.types import User as TgUser

from app.application.assistant import Assistant
from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.entities import Meal, MealItem
from app.application.meal_service import MealService
from app.application.results import DayView, MealCorrected, MealLogged, MealSession
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.bot import handlers
from app.bot.keyboards import day_nav_keyboard, meal_actions_keyboard
from app.bot.states import MealStates
from app.bot.ui import blocks, screens
from app.bot.ui.kit import ROW_NARROW, SAFE, lines_of, plural, text_width, with_macros
from app.domain.goals import Goal
from app.domain.value_objects import Macros, sum_macros
from tests.fakes import InMemoryMealRepo, InMemoryProductRepo, InMemoryUserRepo, InMemoryWeightRepo
from tests.unit.test_ui_widths import _ctx

T0 = datetime(2026, 10, 20, 13, 5, tzinfo=UTC)
GONE = "Запись вошла в набор или удалена — см. 🗒 История"
DROPPED = "📌 Имя набора — вопрос снят."
PROMPT_ID = 77  # id, который «Telegram» отдаёт отправленному вопросу
_TAG = re.compile(r"<[^>]+>")


class _Nlu:
    """Модель-заглушка: «кофе 200 мл» — это кофе; больше она ничего не знает."""

    async def parse(self, *a, **kw):
        raise AssertionError("бот зовёт parse_samples")

    async def parse_samples(self, text, known_keys, *, force=False):
        assert "кофе" in text, text
        item = ParsedItem(name="кофе", canonical_key="coffee", unit=Unit.GRAM, grams=200,
                          kcal_per_100g=2, protein_per_100g=0.1, fat_per_100g=0,
                          carb_per_100g=0.3)
        return [ParsedMessage(intent=Intent.MEAL, items=[item])]


@pytest.fixture(autouse=True)
def _fresh():
    handlers._trash.clear()
    handlers._taps.clear()
    yield
    handlers._trash.clear()
    handlers._taps.clear()


async def _world():
    users, products, meals = InMemoryUserRepo(), InMemoryProductRepo(), InMemoryMealRepo()
    stats = StatsService(meals, users, Goal(2000, 150, 65, 250))
    service = MealService(products, meals)
    assistant = Assistant(_Nlu(), products, service, WeightService(InMemoryWeightRepo()), stats)
    user = await users.get_or_create(telegram_id=1, name="T", locale="ru", timezone="UTC")
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    return SimpleNamespace(assistant=assistant, user=user, meals=meals, state=state)


def _rec(uid: int, minutes: int, *items: tuple[str, float, float], est: bool = False) -> Meal:
    ms = [MealItem(name_snapshot=n, grams=g, macros=Macros(k, k / 10, k / 20, k / 5),
                   is_estimate=est) for n, g, k in items]
    return Meal(user_id=uid, eaten_at=T0 + timedelta(minutes=minutes), input_type="text",
                raw_input=" ".join(n for n, _, _ in items), items=ms, source_confidence=0.9,
                totals=sum_macros(i.macros for i in ms))


_PARTS = [("сыр", 20, 70), ("сыр", 20, 70), ("лаваш", 50, 137), ("помидор", 40, 8),
          ("курица", 120, 200)]


async def _burger(w, *, est: bool = False) -> list[Meal]:
    """Бургер пятью записями 13:05…13:45 — по сообщению на продукт, как пишет владелец."""
    return [await w.meals.add(_rec(w.user.id, 10 * k, p, est=est and k == 4))
            for k, p in enumerate(_PARTS)]


def _bot() -> AsyncMock:
    bot = AsyncMock()
    bot.return_value = Message.model_construct(  # то, что «вернул Telegram» на отправку
        message_id=PROMPT_ID, date=None, chat=Chat.model_construct(id=1, type="private"),
    )
    return bot


def _callback(data: str, message_id: int = 5) -> CallbackQuery:
    bot = _bot()
    msg = Message.model_construct(
        message_id=message_id, date=None, chat=Chat.model_construct(id=1, type="private"),
    ).as_(bot)
    cb = CallbackQuery.model_construct(
        id="1", from_user=TgUser.model_construct(id=1, is_bot=False, first_name="T"),
        chat_instance="ci", data=data, message=msg,
    )
    return cb.as_(bot)


def _text(text: str, reply_to: Message | None = None) -> Message:
    return Message.model_construct(
        message_id=9, date=None, chat=Chat.model_construct(id=1, type="private"), text=text,
        from_user=TgUser.model_construct(id=1, is_bot=False, first_name="T"),
        reply_to_message=reply_to,
    ).as_(_bot())


def _answers(cb: CallbackQuery) -> list[str]:
    return [getattr(c.args[0], "text", None) or "" for c in cb.bot.await_args_list
            if type(c.args[0]).__name__ == "AnswerCallbackQuery"]


def _sent_msgs(obj) -> list[SendMessage]:
    return [c.args[0] for c in obj.bot.await_args_list if isinstance(c.args[0], SendMessage)]


def _sent(obj) -> list[str]:
    return [m.text for m in _sent_msgs(obj)]


def _rich(msg: Message) -> tuple[str, InlineKeyboardMarkup]:
    kw = msg.bot.send_rich_message.await_args.kwargs
    return kw["rich_message"].html, kw["reply_markup"]


def _edits(msg: Message) -> list[dict]:
    """Правки сообщений по id (`bot.edit_message_text`): вопрос «снят», карточка «вошло»."""
    return [c.kwargs for c in msg.bot.edit_message_text.await_args_list]


def _plain(html: str) -> str:
    return _TAG.sub("", html)


def _buttons(kb: InlineKeyboardMarkup) -> list:
    return [b for row in kb.inline_keyboard for b in row]


async def _card(w, meal: Meal) -> tuple[str, InlineKeyboardMarkup]:
    msg = _text("x")
    logged = MealLogged(meal=meal, remaining=(await w.assistant.today(w.user)).remaining)
    await handlers._send_meal_card(msg, logged, w.user, w.assistant)
    return _rich(msg)


async def _tap_save(w, meal_id: int, tail: str = ":s", db=None) -> CallbackQuery:
    cb = _callback(f"dish_save:{meal_id}{tail}")
    await handlers.on_dish_save(cb, w.user, w.assistant, w.state, db or AsyncMock())
    return cb


async def _name(w, name: str, db=None, reply_to: Message | None = None) -> Message:
    msg = _text(name, reply_to)
    await handlers.on_dish_name_input(
        msg, msg.bot, w.user, w.assistant, w.state, db or AsyncMock()
    )
    return msg


async def _save(w, meal_id: int, name: str, *, whole: bool = True, db=None):
    """Тап «📌» и ответ именем — как человек: вопрос, затем слово."""
    cb = await _tap_save(w, meal_id, ":s" if whole else "", db)
    msg = _text(name)
    if await w.state.get_state() == MealStates.awaiting_dish_name.state:
        msg = await _name(w, name, db)
    return cb, msg


def _block(total: Macros, k: int, *, est: bool) -> tuple[str, str]:
    return f"🍽 Приём пищи с 13:05 · {k} {plural(k, 'запись', 'записи', 'записей')}", (
        f"{'≈' if est else ''}{round(total.kcal)} ккал · Б{round(total.protein)} "
        f"Ж{round(total.fat)} У{round(total.carb)}")


def _has_block(html: str, total: Macros, k: int, *, est: bool = False) -> bool:
    head, sums = _block(total, k, est=est)
    rows = [_plain(x).strip() for x in lines_of(html)]
    return head in rows and rows[rows.index(head) + 1] == sums


# --- карточка: блок приёма пищи (AC-188) ----------------------------------------------------


async def test_блок_приёма_пищи_на_карточке_и_его_нет_у_одиночной():
    w = await _world()
    burger = await _burger(w, est=True)
    html, _ = await _card(w, burger[-1])
    assert _has_block(html, sum_macros(m.totals for m in burger), 5, est=True)
    raw = lines_of(html)
    i = [_plain(x).strip() for x in raw].index("🍽 Приём пищи с 13:05 · 5 записей")
    assert text_width(raw[i]) <= SAFE and text_width(raw[i + 1]) <= SAFE  # без рваных строк
    assert "Запись: " not in html  # у записи одна позиция — её итог не дублируется

    apple = await w.meals.add(_rec(w.user.id, 300, ("яблоко", 150, 78)))  # 18:05 — отдельно
    html, kb = await _card(w, apple)
    assert "Приём пищи" not in html
    assert all("📌" not in b.text for b in _buttons(kb))


async def test_блок_и_на_карточках_правки_веса_вернуть_и_ответа(monkeypatch):
    """Карточки ✏️ Вес, ⏪ Вернул и правки ответом (FR-43, FR-41, FR-44) — тот же блок."""
    rich, at = AsyncMock(), AsyncMock(return_value=True)
    monkeypatch.setattr(handlers, "_rich_edit", rich)
    monkeypatch.setattr(handlers, "_rich_edit_at", at)
    w = await _world()
    a = await w.meals.add(_rec(w.user.id, 0, ("курица", 120, 200), ("лаваш", 50, 137)))
    b = await w.meals.add(_rec(w.user.id, 10, ("сыр", 20, 70)))

    def total() -> Macros:
        return sum_macros(m.totals for m in w.meals._meals)

    logged = MealLogged(meal=a, remaining=(await w.assistant.today(w.user)).remaining)
    html, kb = await handlers._weight_card(w.user, w.assistant, logged)
    assert _has_block(html, total(), 2)
    assert any(x.callback_data == f"dish_save:{a.id}:s" for x in _buttons(kb))

    fixed = MealCorrected("adjust", a, logged.remaining, [a.items[0]], [a.items[0]])
    await handlers._show_fix(_text("x"), w.user, w.assistant, 5, None, fixed)
    assert _has_block(at.await_args.args[3], total(), 2)

    await handlers.on_item_del(_callback(f"item_del:{a.id}:{a.items[1].id}"), w.user,
                               w.assistant, AsyncMock())
    token = handlers._trash[w.user.id].token
    await handlers.on_restore(_callback(f"rst:{token}:c"), w.user, w.assistant, AsyncMock())
    html = rich.await_args.args[1]
    assert _plain(html).startswith("⏪ Вернул") and _has_block(html, total(), 2)
    assert b in w.meals._meals


async def test_итог_записи_зовётся_записью():
    w = await _world()
    two = await w.meals.add(_rec(w.user.id, 0, ("курица", 120, 200), ("лаваш", 50, 137)))
    html, kb = await _card(w, two)
    assert "Запись: <b>337 ккал</b>" in html and "Приём:" not in html
    assert [b.callback_data for b in _buttons(kb) if b.text == "📌 Сохранить набор"] == [
        f"dish_save:{two.id}"
    ]


# --- кнопка (AC-189, AC-209, AC-210) --------------------------------------------------------


def _pins(kb: InlineKeyboardMarkup) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for b in _buttons(kb) if "📌" in b.text or "✏️ П" in b.text]


def test_кнопка_набора_приёма_пищи_только_если_у_всех_вес():
    meals = [_rec(1, 0, ("сыр", 20, 70)), _rec(1, 10, ("курица", 120, 200), ("лаваш", 50, 137))]
    for i, m in enumerate(meals):
        m.id = i + 1
    [sess] = MealSession.group(meals)
    assert _pins(meal_actions_keyboard(meals[1], None, sess)) == [
        ("📌 Приём пищи — в набор", "dish_save:2:s")
    ]
    meals[0].items[0].grams = 0  # «свои цифры» без веса: весь приём пищи набором не повторить
    # AC-210: запись сама годится в набор — её прежний «📌 Сохранить набор»; сыр — ничего.
    assert _pins(meal_actions_keyboard(meals[1], None, sess)) == [
        ("📌 Сохранить набор", "dish_save:2")
    ]
    assert _pins(meal_actions_keyboard(meals[0], None, sess)) == []


def test_набор_переименовывается_и_слитый_без_корзин():
    """AC-209: запись-набор — «✏️ Переименовать набор»; карточка слияния — без «🗑 позиция»."""
    meal = _rec(1, 0, ("курица", 120, 200), ("лаваш", 50, 137))
    meal.id, meal.template_name = 7, "шаурма"
    for j, it in enumerate(meal.items):
        it.id = j + 1
    assert _pins(meal_actions_keyboard(meal)) == [("✏️ Переименовать набор", "dish_save:7")]
    assert sum(b.text.startswith("🗑") for b in _buttons(meal_actions_keyboard(meal))) == 2
    trimmed = meal_actions_keyboard(meal, trash=False)
    assert not any(b.text.startswith("🗑") for b in _buttons(trimmed))
    assert _buttons(trimmed)[-1].text == "↩️ Отменить всё"


# --- «📌 Приём пищи — в набор» (AC-190…AC-192, AC-207, AC-208) -----------------------------


async def test_приём_пищи_в_набор_сливает_названное():
    w = await _world()
    burger = await _burger(w)
    day_kcal = sum(m.totals.kcal for m in w.meals._meals)
    db = AsyncMock()
    cb = await _tap_save(w, burger[-1].id, db=db)
    [ask] = _sent_msgs(cb.message)
    assert ask.text.startswith(
        "📌 Как назвать набор? В него войдёт приём пищи с 13:05 — 5 записей, 485"
    )
    assert "сыр, сыр, лаваш, помидор, курица" in ask.text and "Пришли имя" in ask.text
    assert [(b.text, b.callback_data) for b in _buttons(ask.reply_markup)] == [
        ("✖️ Отмена", f"dish_save:{burger[-1].id}:x")
    ]
    msg = await _name(w, "бургер", db)
    [one] = w.meals._meals
    assert one.eaten_at == burger[0].eaten_at and one.template_name == "бургер"
    assert sorted(i.name_snapshot for i in one.items) == sorted(n for n, _, _ in _PARTS)
    assert one.totals.kcal == pytest.approx(day_kcal)  # итоги дня не двинулись
    db.commit.assert_awaited()
    html, kb = _rich(msg)
    assert _plain(html).startswith("📌 Набор сохранён") and "13:05" in html.split("</h3>")[0]
    assert "<b>бургер</b> — дальше пиши «бургер»" in html
    assert all(text_width(x) <= SAFE for x in lines_of(html) if "Набор сохранён" in x)
    assert "Приём пищи с" not in html  # теперь это одна запись
    assert not any(b.text.startswith("🗑") for b in _buttons(kb))  # пять корзин — не нужны
    assert await w.state.get_state() is None
    # Карточка, где нажали «📌», — след «Вошло в набор» без кнопок; вопрос — имя, не «снят».
    edits = {e["message_id"]: e for e in _edits(msg)}
    assert _plain(edits[5]["rich_message"].html).startswith("📌 Вошло в набор «бургер»")
    assert edits[5]["reply_markup"] is None
    assert edits[PROMPT_ID]["text"] == "📌 Имя набора: «бургер»"


async def test_кнопки_карточек_слитых_записей_ничего_не_пишут():
    """AC-207 (AC-197 в боте): «🔁», «↩️», «🗑», «✏️ Вес», «📌» на карточке лаваша после слияния."""
    w = await _world()
    burger = await _burger(w)
    await _save(w, burger[0].id, "бургер")
    [merged] = w.meals._meals
    before = [(i.id, i.grams) for i in merged.items]
    old = burger[2]
    rep = _callback(f"meal_repeat:{old.id}")
    await handlers.on_meal_repeat(rep, w.user, w.assistant)
    assert _sent(rep.message) == [GONE]
    for handler, data in [
        (handlers.on_meal_undo, f"meal_undo:{old.id}"),
        (handlers.on_item_del, f"item_del:{old.id}:{old.items[0].id}"),
    ]:
        cb = _callback(data)
        await handler(cb, w.user, w.assistant, AsyncMock())
        assert _answers(cb) == [GONE], data
    cb = _callback(f"iw:{old.id}")
    await handlers.on_item_weight(cb, w.user, w.assistant, AsyncMock(), w.state)
    assert _answers(cb) == [GONE]
    assert _answers(await _tap_save(w, old.id)) == [GONE]
    assert w.meals._meals == [merged] and [(i.id, i.grams) for i in merged.items] == before


@pytest.mark.parametrize("change", ["added", "deleted"])
async def test_приём_пищи_изменился_пока_думал_над_именем(change):
    """AC-191, AC-208: отказ называет текущий состав, «Сохранить как «бургер»» его и сохраняет."""
    w = await _world()
    burger = await _burger(w)
    await _tap_save(w, burger[-1].id)
    if change == "added":  # дописал соус с другого устройства — к тому же приёму пищи
        await w.meals.add(_rec(w.user.id, 50, ("соус", 15, 40)))
    else:
        await w.assistant.delete_meal(w.user, burger[2].id)
    msg = await _name(w, "бургер")
    [refusal] = _sent_msgs(msg)
    k = 6 if change == "added" else 4
    assert refusal.text.startswith(
        "Приём пищи изменился — ничего не слил. Сейчас в нём: с 13:05 — "
        f"{k} {plural(k, 'запись', 'записи', 'записей')}"
    )
    assert all(m.template_name is None for m in w.meals._meals) and len(w.meals._meals) == k
    [again] = _buttons(refusal.reply_markup)
    assert again.text == "📌 Сохранить как «бургер»"
    assert again.callback_data == f"dish_save:{max(m.id for m in w.meals._meals)}:r"
    await _tap_save(w, int(again.callback_data.split(":")[1]), ":r")
    [one] = w.meals._meals
    assert one.template_name == "бургер" and len(one.items) == k


@pytest.mark.parametrize("left", [2, 1])
async def test_приём_пищи_сжался_до_одной_записи(left):
    """AC-208: осталась запись из двух взвешенных позиций — кнопка сохраняет её; из одной — нет."""
    w = await _world()
    parts = [("курица", 120, 200), ("лаваш", 50, 137)][:left]
    keep = await w.meals.add(_rec(w.user.id, 0, *parts))
    other = await w.meals.add(_rec(w.user.id, 10, ("сыр", 20, 70)))
    await _tap_save(w, other.id)
    await w.assistant.delete_meal(w.user, other.id)
    [refusal] = _sent_msgs(await _name(w, "шаурма"))
    if left == 2:
        [again] = _buttons(refusal.reply_markup)
        assert again.callback_data == f"dish_save:{keep.id}:r"
        await _tap_save(w, keep.id, ":r")
        assert [m.template_name for m in w.meals._meals] == ["шаурма"]
    else:
        assert refusal.reply_markup is None and refusal.text.endswith("Нажми «📌» ещё раз.")
        assert keep.template_name is None


async def test_кнопка_без_хвоста_сохраняет_только_свою_запись():
    """AC-192: старая карточка «📌 Сохранить набор» — кофе, прилипший позже, не сливается."""
    w = await _world()
    two = await w.meals.add(_rec(w.user.id, 0, ("курица", 120, 200), ("лаваш", 50, 137)))
    await w.meals.add(_rec(w.user.id, 10, ("кофе", 200, 4)))
    await _save(w, two.id, "шаурма", whole=False)
    assert len(w.meals._meals) == 2
    assert [m.template_name for m in w.meals._meals] == ["шаурма", None]


async def test_распавшийся_приём_пищи_или_пропавший_вес_отказ_сразу():
    w = await _world()
    one = await w.meals.add(_rec(w.user.id, 0, ("сыр", 20, 70)))
    other = await w.meals.add(_rec(w.user.id, 10, ("лаваш", 50, 137)))
    other.items[0].grams = 0  # вес убрали в аппке, пока карточка висела
    bad = "Набор не сохранил: нужно хотя бы две позиции, и у каждой — вес."
    cb = await _tap_save(w, one.id)
    assert _answers(cb) == [bad] and await w.state.get_state() is None
    await w.assistant.delete_meal(w.user, other.id)  # осталась запись из одной позиции
    cb = await _tap_save(w, one.id)
    assert _answers(cb) == [bad] and _sent(cb.message) == []
    await w.assistant.delete_meal(w.user, one.id)
    assert _answers(await _tap_save(w, one.id)) == [GONE]


# --- вопрос об имени не глотает еду (AC-205, AC-206) --------------------------------------


async def test_кофе_пока_ждём_имя_записан_кофе_а_не_набор():
    w = await _world()
    burger = await _burger(w)
    await _tap_save(w, burger[-1].id)
    msg = await _name(w, "кофе 200 мл")
    assert await w.state.get_state() is None
    dropped = {"text": DROPPED, "chat_id": 1, "message_id": PROMPT_ID}
    assert dropped.items() <= _edits(msg)[0].items()
    assert all(m.template_name is None for m in w.meals._meals)
    coffee = [m for m in w.meals._meals if m.items[0].name_snapshot == "кофе"]
    assert len(coffee) == 1 and len(w.meals._meals) == 6


async def test_ответ_на_карточку_пока_ждём_имя_уходит_в_правку(monkeypatch):
    on_text = AsyncMock()
    monkeypatch.setattr(handlers, "on_text", on_text)
    w = await _world()
    burger = await _burger(w)
    await _tap_save(w, burger[-1].id)
    _, kb = await _card(w, burger[-1])
    card = Message.model_construct(message_id=5, date=None, reply_markup=kb,
                                   chat=Chat.model_construct(id=1, type="private"))
    msg = await _name(w, "это была индейка", reply_to=card)
    assert on_text.await_args.args[0] is msg and await w.state.get_state() is None
    assert len(w.meals._meals) == 5


@pytest.mark.parametrize("kind", ["voice", "photo"])
async def test_голос_и_фото_снимают_вопрос(kind):
    w = await _world()
    burger = await _burger(w)
    await _tap_save(w, burger[-1].id)
    msg = _text("")
    bot = msg.bot
    if kind == "voice":
        await handlers.on_voice(msg, bot, w.user, w.assistant, AsyncMock(), w.state)
    else:
        await handlers.on_photo(msg, bot, w.user, w.assistant, AsyncMock(), w.state, None)
    assert await w.state.get_state() is None
    assert _edits(msg)[0]["text"] == DROPPED
    assert not any("жду ответ" in t for t in _sent(msg))


async def test_отмена_и_длинное_имя():
    w = await _world()
    burger = await _burger(w)
    await _tap_save(w, burger[-1].id)
    long = await _name(w, "б" * 65)  # AC-206: не обрезаем молча — отказ, вопрос ждёт
    assert _sent(long) == ["Имя набора — от 1 до 64 знаков. Нажми «📌» ещё раз."]
    assert await w.state.get_state() == MealStates.awaiting_dish_name.state
    cb = _callback(f"dish_save:{burger[-1].id}:x", message_id=PROMPT_ID)
    await handlers.on_dish_save(cb, w.user, w.assistant, w.state, AsyncMock())
    assert _answers(cb) == ["Отменил."] and await w.state.get_state() is None
    assert len(w.meals._meals) == 5 and all(m.template_name is None for m in w.meals._meals)
    again = _callback(f"dish_save:{burger[-1].id}:x", message_id=PROMPT_ID)
    await handlers.on_dish_save(again, w.user, w.assistant, w.state, AsyncMock())
    assert _answers(again) == ["Уже не жду имя."]


# --- «⏪ Вернуть» после слияния (AC-194) ----------------------------------------------------


@pytest.mark.parametrize("which", ["ранняя", "поздняя"])
async def test_вернуть_после_слияния_отказывает(which, monkeypatch):
    monkeypatch.setattr(handlers, "_rich_edit", AsyncMock())
    w = await _world()
    a = await w.meals.add(_rec(w.user.id, 0, ("курица", 120, 200), ("лаваш", 50, 137)))
    await w.meals.add(_rec(w.user.id, 10, ("сыр", 20, 70)))
    c = await w.meals.add(_rec(w.user.id, 20, ("помидор", 40, 8), ("огурец", 60, 9)))
    src = a if which == "ранняя" else c
    await handlers.on_item_del(
        _callback(f"item_del:{src.id}:{src.items[1].id}"), w.user, w.assistant, AsyncMock()
    )
    token = handlers._trash[w.user.id].token
    await _save(w, c.id, "бургер")
    [merged] = w.meals._meals
    before = [(i.id, i.grams) for i in merged.items]
    cb = _callback(f"rst:{token}:c")
    await handlers.on_restore(cb, w.user, w.assistant, AsyncMock())
    assert _answers(cb) == ["Приём уже поправили — вернуть нельзя."]
    assert w.meals._meals == [merged] and [(i.id, i.grams) for i in merged.items] == before


# --- экран дня (AC-193, AC-204, AC-211) и «Сегодня» (AC-195) ------------------------------


def _dayview(meals: list[Meal]) -> DayView:
    for i, m in enumerate(meals):
        m.id = 100 + i
        for j, it in enumerate(m.items):
            it.id = 10 * i + j + 1
    return DayView(meals=meals, timezone="UTC", days_ago=1, day=date(2026, 10, 20),
                   sessions=MealSession.group(meals))


_REM = SimpleNamespace(consumed=Macros(563, 56, 28, 113), goal=Goal(2000, 150, 65, 250))


def _burger_day() -> tuple[list[Meal], DayView]:
    meals = [_rec(1, 10 * k, p, est=k == 4) for k, p in enumerate(_PARTS)]
    meals.append(_rec(1, 300, ("яблоко", 150, 78)))
    return meals, _dayview(meals)


def test_экран_дня_приём_пищи_шапкой_позиции_под_ней():
    meals, dv = _burger_day()
    html = screens.day(dv, _REM)
    total = sum_macros(m.totals for m in meals[:5])
    head = (f"🍽 13:05–13:45 · ≈{round(total.kcal)} ккал · Б{round(total.protein)} "
            f"Ж{round(total.fat)} У{round(total.carb)}")
    [h] = [x for x in lines_of(html) if x.lstrip().startswith("🍽")]
    assert _plain(h).strip() == head and text_width(h) <= SAFE
    # Позиции бургера — вложенным списком под шапкой, яблоко — строкой верхнего уровня.
    nested = re.search(r"<li>🍽.*?<ul>(.*?)</ul></li>", html, re.S)
    assert nested and nested.group(1).count("<li>") == 5
    assert "яблоко" not in nested.group(1) and "яблоко" in html.split("</ul></li>", 1)[1]
    # AC-204: у каждой позиции Б/Ж/У (в строку или второй строкой курсивом), ≈ — у оценки.
    macros = r"ккал(?: · |<br><i>)"
    assert len(re.findall(macros + "Б7 Ж4 У14", html)) == 2
    assert re.search(macros + "≈Б20 Ж10 У40", html)
    assert re.search(r"яблоко — 150 г · <b>78</b> " + macros + "Б8 Ж4 У16", html)
    # Кнопки — по-прежнему на позицию, callback-и прежние.
    kb = day_nav_keyboard(1, dv.meals, dv.timezone, day=dv.day)
    dels = [b.callback_data for b in _buttons(kb) if b.text.startswith("🗑")]
    assert dels == [f"day_del:1:{m.id}:{m.items[0].id}" for m in meals]
    rep = day_nav_keyboard(1, dv.meals, dv.timezone, repeat=True, day=dv.day)
    reps = [b.callback_data for b in _buttons(rep) if b.text.startswith("🔁 1")]
    assert reps and all(d.startswith("day_rep:d20261020:") for d in reps)


def test_длинная_позиция_уносит_бжу_на_вторую_строку_с_учётом_отступа():
    """AC-204: на 320 px не рвать «Б25 Ж27» посередине; вложенной строке места меньше."""
    m = Macros(200, 20, 10, 40)
    row = "13:05 · к — 120 г · <b>200</b> ккал"
    while text_width(f"{row} · Б20 Ж10 У40") <= ROW_NARROW - 30:  # между «вложенной» и «верхней»
        row = row.replace("к", "кк", 1)
    assert with_macros(row, m) == f"{row} · Б20 Ж10 У40"  # верхний уровень — влезло
    assert with_macros(row, m, nested=True) == f"{row}<br><i>Б20 Ж10 У40</i>"  # отступ съел место
    assert with_macros(row, m, est=True, nested=True).endswith("<i>≈Б20 Ж10 У40</i>")
    long = "13:05 · куриное филе гриль с пармезаном — 120 г · <b>200</b> ккал"
    assert with_macros(long, m) == f"{long}<br><i>Б20 Ж10 У40</i>"


async def test_экран_дня_плоско_если_telegram_отверг_вложенный(monkeypatch):
    """AC-211: BadRequest на вложенный список — тот же день плоско, а не упавший экран."""
    sent: list[str] = []

    async def rich(message, html, markup=None):
        sent.append(html)
        if "<ul><li>" in html.split("🍽", 1)[-1][:400]:
            raise TelegramBadRequest(method=None, message="can't parse rich message")

    monkeypatch.setattr(handlers, "_rich", rich)
    w = await _world()
    w.meals._meals.clear()
    start = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    for k in range(2):
        m = _rec(w.user.id, 0, ("сыр", 20, 70))
        m.eaten_at = start + timedelta(minutes=10 * k)
        await w.meals.add(m)
    await handlers._show_history(_text("x"), w.user, w.assistant)
    assert len(sent) == 2 and "↳ " in sent[1] and sent[1].count("<ul>") == 1


def test_экран_дня_без_групп_как_раньше():
    dv = _dayview([_rec(1, 0, ("сыр", 20, 70)), _rec(1, 120, ("лаваш", 50, 137))])
    html = screens.day(dv, _REM)
    assert "🍽" not in html and html.count("<ul>") == 1


def test_сегодня_считает_приёмы_пищи_и_даёт_бжу():
    burger = [_rec(1, 10 * k, p) for k, p in enumerate(_PARTS)]
    apple = _rec(1, 300, ("яблоко", 150, 78))
    eaten = blocks.eaten(_ctx([*burger, apple]))
    head = re.search(r"<summary>(.*?)</summary>", eaten)
    assert head and head.group(1) == "🍽 Съедено · 2 приёма пищи · 6 позиций"
    assert re.search(r"курица — 120 г · <b>200</b> ккал(?: · |<br><i>)Б20 Ж10 У40", eaten)


async def test_тексты_про_одну_запись_не_зовут_её_приёмом(monkeypatch):
    """AC-195: «↩️ Отменить всё» — «Отменил запись», не «приём»."""
    rich = AsyncMock()
    monkeypatch.setattr(handlers, "_rich_edit", rich)
    w = await _world()
    meal = await w.meals.add(_rec(w.user.id, 0, ("курица", 120, 200), ("лаваш", 50, 137)))
    cb = _callback(f"meal_undo:{meal.id}")
    await handlers.on_meal_undo(cb, w.user, w.assistant, AsyncMock())
    assert _answers(cb) == ["🗑 Отменил запись (337 ккал)"]
    assert _plain(rich.await_args.args[1]).startswith("🗑 Отменил запись · 13:05")
