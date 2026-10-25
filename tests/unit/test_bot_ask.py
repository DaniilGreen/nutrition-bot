"""ИИ-ассистент в чате бота (FR-45, AC-148…AC-157, D-MA-27).

Держим: `/ask` и вопрос, который разбор счёл unknown, идут в ТОТ ЖЕ `Coach`, что у вкладки AI
(те же инструменты — объект данных `CoachData`); еда со «?» записывается, а не уходит в
ассистента; встроенный «сколько осталось» — как раньше; правки заглушки не чаще раза в 1,5 с,
последняя — полный текст; ответ длиннее 4096 режется; отказы — до заглушки и без модели; сбой
— на месте заглушки и без повтора; учёт — только в адаптере, один раз за раунд.
"""
from __future__ import annotations

import io
import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Chat, Message, Voice
from aiogram.types import User as TgUser

from app.application.assistant import Assistant
from app.application.coach import DISCLAIMER, LIMIT_MESSAGE, Coach, CoachChunk
from app.application.coach_data import CoachData
from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.meal_service import MealService
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.bot import coach_chat, handlers
from app.domain.goals import Goal
from app.domain.mentions import Candidate
from tests.fakes import (
    InMemoryMealRepo,
    InMemoryProductRepo,
    InMemorySpendRepo,
    InMemoryUserRepo,
    InMemoryWeightRepo,
    StubCoachPort,
    StubStt,
)


@pytest.fixture(autouse=True)
def _fresh():
    coach_chat.reset()
    handlers._taps.clear()
    yield
    coach_chat.reset()


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Nlu:
    """Сценарный разбор; считает вызовы — `/ask` в разбор не ходит."""

    def __init__(self, *answers: ParsedMessage) -> None:
        self.answers = list(answers)
        self.texts: list[str] = []

    async def parse(self, text, known_keys):
        raise AssertionError("бот зовёт parse_samples")

    async def parse_samples(self, text, known_keys, *, force=False):
        self.texts.append(text)
        nxt = self.answers.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return [nxt]


async def _world(port: StubCoachPort | None = None, *nlu: ParsedMessage, spend=None, stt=None):
    users, products, meals = InMemoryUserRepo(), InMemoryProductRepo(), InMemoryMealRepo()
    weights = InMemoryWeightRepo()
    stats = StatsService(meals, users, Goal(2000, 150, 65, 250))
    parser = _Nlu(*nlu)
    assistant = Assistant(parser, products, MealService(products, meals), WeightService(weights),
                          stats, stt)
    user = await users.get_or_create(telegram_id=1, name="T", locale="ru", timezone="UTC")
    spend = spend or InMemorySpendRepo()

    def coach_for(session):
        # Ровно как Container.coach_for: тот же порт, репозитории для инструментов (§6.21).
        if port is None:
            return None
        return Coach(port, spend, stats, 15.0, meals, weights, products)

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    return SimpleNamespace(
        user=user, assistant=assistant, nlu=parser, meals=meals, spend=spend, state=state,
        container=SimpleNamespace(coach_for=coach_for), coach=coach_for(None),
    )


# --- фейковый чат для потока: заглушка и её правки ----------------------------------------


class _Sent:
    def __init__(self, text: str, markup) -> None:
        self.text, self.markup = text, markup
        self.edits: list[tuple[str, object]] = []
        self.fail: list[Exception] = []   # исключения на ближайшие правки, по порядку

    async def edit_text(self, text, reply_markup=None):
        if self.fail:
            raise self.fail.pop(0)
        self.edits.append((text, reply_markup))


class _Chat:
    def __init__(self, message_id: int = 9) -> None:
        self.chat = SimpleNamespace(id=1)
        self.message_id = message_id
        self.sent: list[_Sent] = []

    async def answer(self, text, reply_markup=None):
        m = _Sent(text, reply_markup)
        self.sent.append(m)
        return m

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.sent]


async def _ask(w, msg: _Chat, question: str, clock=None, **kw) -> _Chat:
    await coach_chat.ask(msg, w.user, w.coach, question, clock=clock or _Clock(),
                         sleep=AsyncMock(), **kw)
    return msg


# --- aiogram-сообщение для проверки маршрута в on_text --------------------------------------


_MSG_IDS = itertools.count(1)  # каждое сообщение — своё: одинаковый id — это повтор апдейта


def _tg(text: str) -> Message:
    ids = itertools.count(1000)
    chat = Chat.model_construct(id=1, type="private")
    bot = AsyncMock()

    async def reply(method, *a, **k):
        return Message.model_construct(message_id=next(ids), date=None, chat=chat,
                                       text=getattr(method, "text", None)).as_(bot)

    bot.side_effect = reply
    return Message.model_construct(
        message_id=next(_MSG_IDS), date=None, chat=chat, text=text,
        from_user=TgUser.model_construct(id=1, is_bot=False, first_name="T"),
    ).as_(bot)


def _bot_texts(msg: Message) -> list[str]:
    return [getattr(c.args[0], "text", "") or "" for c in msg.bot.await_args_list
            if isinstance(c.args[0], SendMessage | EditMessageText)]


async def _say(w, text: str) -> Message:
    msg = _tg(text)
    await handlers.on_text(msg, msg.bot, w.user, w.assistant, AsyncMock(), w.state,
                           container=w.container)
    return msg


def _meal(name: str) -> ParsedMessage:
    return ParsedMessage(intent=Intent.MEAL, items=[ParsedItem(
        name=name, canonical_key=name, unit=Unit.GRAM, grams=150, kcal_per_100g=340,
        protein_per_100g=12, fat_per_100g=1.3, carb_per_100g=70, confidence=0.9,
    )], confidence=0.9)


_UNKNOWN = ParsedMessage(intent=Intent.UNKNOWN, confidence=0.3)


# --- маршрут: /ask и свободный текст (AC-148…AC-151) ---------------------------------------


async def test_ask_идёт_в_тот_же_coach_с_инструментами_без_разбора():
    port = StubCoachPort()
    w = await _world(port)
    msg = _tg("/ask откуда калории?")
    await handlers.cmd_ask(msg, CommandObject(prefix="/", command="ask", args="откуда калории?"),
                           msg.bot, w.user, AsyncMock(), container=w.container)
    assert len(port.calls) == 1 and port.calls[0]["question"] == "откуда калории?"
    # Те же инструменты, что у аппки: объект данных привязан к человеку (MA-INV-39).
    assert isinstance(port.calls[0]["data"], CoachData)
    assert w.nlu.texts == []  # разбор фразы не звали
    texts = _bot_texts(msg)
    assert texts[0] == coach_chat.THINKING
    assert "молоко 3.2%" in texts[-1] and DISCLAIMER in texts[-1]


async def test_пустой_ask_подсказка_без_модели():
    port = StubCoachPort()
    w = await _world(port)
    msg = await _ask(w, _Chat(), "   ")
    assert msg.texts == [coach_chat.HINT] and port.calls == []


async def test_еда_со_знаком_вопроса_записывается_а_не_уходит_ассистенту():
    port = StubCoachPort()
    w = await _world(port, _meal("гречка"), _meal("банан"))
    await _say(w, "гречка 150 г?")
    await _say(w, "можно банан?")
    assert port.calls == []
    assert [m.items[0].name_snapshot for m in w.meals._meals] == ["гречка", "банан"]


async def test_встроенный_остаток_и_не_понял_как_раньше():
    port = StubCoachPort()
    w = await _world(port, ParsedMessage(intent=Intent.QUERY, confidence=0.9), _UNKNOWN)
    query = await _say(w, "сколько осталось?")
    hello = await _say(w, "привет")
    assert port.calls == []
    assert not any(coach_chat.THINKING in t for t in _bot_texts(query) + _bot_texts(hello))
    assert any("Не понял" in t for t in _bot_texts(hello))


async def test_вопрос_из_unknown_уходит_ассистенту_без_ключа_не_понял():
    port = StubCoachPort()
    w = await _world(port, _UNKNOWN, _UNKNOWN)
    await _say(w, "почему я не худею?")
    await _say(w, "что съесть на ужин")
    assert [c["question"] for c in port.calls] == ["почему я не худею?", "что съесть на ужин"]

    off = await _world(None, _UNKNOWN)
    msg = await _say(off, "почему я не худею?")
    assert any("Не понял" in t for t in _bot_texts(msg))


# --- поток (AC-152, AC-153) ------------------------------------------------------------------


async def test_правки_не_чаще_раза_в_полторы_секунды_и_последняя_полная():
    clock = _Clock()

    def tick():
        clock.t += 0.2

    chunks = [CoachChunk(kind="delta", text="преамбула "), CoachChunk(kind="reset")] + [
        CoachChunk(kind="delta", text=f"слово{i} ") for i in range(20)]
    port = StubCoachPort(chunks, tick=tick)
    w = await _world(port)
    msg = await _ask(w, _Chat(), "разбери неделю", clock=clock)
    placeholder = msg.sent[0]
    *drafts, final = placeholder.edits
    assert 1 <= len(drafts) <= 3                  # 22 куска за ~4,4 с → не больше трёх правок
    assert all(t.endswith(" …") for t, _ in drafts)
    assert "преамбула" not in final[0]            # reset стёр напечатанное
    assert final[0].startswith(" ".join(f"слово{i}" for i in range(20)))
    assert final[0].endswith(f"<i>{DISCLAIMER}</i>")
    assert len(msg.sent) == 1                     # всё — в заглушке


async def test_строка_состояния_инструмента():
    clock = _Clock()

    def tick():
        clock.t += 2

    port = StubCoachPort([CoachChunk(kind="tool", text="meals_on"),
                          CoachChunk(kind="tool", text="accuracy"),
                          CoachChunk(kind="delta", text="Ответ.")], tick=tick)
    w = await _world(port)
    msg = await _ask(w, _Chat(), "что я ел вчера?", clock=clock)
    edits = [t for t, _ in msg.sent[0].edits]
    assert edits[:2] == ["🔎 Смотрю записи…", "🔎 Смотрю данные…"]


async def test_длинный_ответ_режется_дисклеймер_у_последнего():
    long = ("**белок** <b>x</b> " + "слово " * 600 + "\n\n") * 3   # ~11 000 знаков
    port = StubCoachPort([CoachChunk(kind="delta", text=long)])
    w = await _world(port)
    msg = await _ask(w, _Chat(), "разложи")
    first = msg.sent[0].edits[-1][0]
    pieces = [first, *(m.text for m in msg.sent[1:])]
    assert len(pieces) == 3
    assert all(len(p) <= 4096 for p in pieces)
    assert "<b>белок</b>" in first and "&lt;b&gt;x&lt;/b&gt;" in first   # HTML модели экранирован
    assert DISCLAIMER in pieces[-1] and DISCLAIMER not in first


def test_нарезка_по_границам_и_без_разрыва_жирного():
    text = "а" * 50 + " **жирный текст** " + "б" * 50
    parts = coach_chat.split(text, size=60)
    assert "".join(parts).replace(" ", "") == text.replace(" ", "")
    assert all(len(p) <= 60 for p in parts)
    assert all(p.count("**") % 2 == 0 for p in parts)
    assert coach_chat.split("коротко") == ["коротко"]


def test_кнопки_записать_на_названные_продукты():
    kb = coach_chat.actions_keyboard([Candidate(7, "творог 5%", 180.0),
                                      Candidate(8, "очень длинное название продукта гриль", 95.55),
                                      Candidate(9, "ноль", 0.0)])
    assert kb is not None
    rows = [b for row in kb.inline_keyboard for b in row]
    assert [b.callback_data for b in rows] == ["fq:p:7:180", "fq:p:8:95.5"]  # тап — «⚡ Частое»
    assert rows[0].text == "Записать творог 5% · 180 г" and "…" in rows[1].text
    assert coach_chat.actions_keyboard([]) is None


# --- отказы до заглушки (AC-154) -----------------------------------------------------------


async def test_потолок_месяца_без_модели():
    port = StubCoachPort()
    w = await _world(port, spend=InMemorySpendRepo(preloaded_usd=20.0))
    msg = await _ask(w, _Chat(), "чем добрать белок?")
    assert msg.texts == [LIMIT_MESSAGE] and port.calls == []


async def test_седьмой_вопрос_за_минуту_слишком_часто():
    port = StubCoachPort()
    w = await _world(port)
    for i in range(7):
        msg = await _ask(w, _Chat(message_id=i), "вопрос?")
    assert msg.texts == [coach_chat.TOO_OFTEN] and len(port.calls) == 6


async def test_второй_вопрос_пока_первый_в_работе():
    port = StubCoachPort()
    w = await _world(port)
    coach_chat._asking.add(w.user.id)
    msg = await _ask(w, _Chat(), "вопрос?")
    assert msg.texts == [coach_chat.BUSY] and port.calls == []


async def test_длинный_вопрос_и_нет_ключа():
    w = await _world(StubCoachPort())
    assert (await _ask(w, _Chat(1), "я" * 501)).texts == [coach_chat.TOO_LONG]
    off = await _world(None)
    assert (await _ask(off, _Chat(2), "вопрос?")).texts == [coach_chat.UNAVAILABLE]


# --- сбои (AC-155) -------------------------------------------------------------------------


async def test_обрыв_потока_на_месте_заглушки_без_повтора():
    port = StubCoachPort([CoachChunk(kind="delta", text="нача"),
                          CoachChunk(kind="delta", text="ло")], fail=TimeoutError())
    w = await _world(port)
    msg = await _ask(w, _Chat(), "вопрос?")
    assert msg.sent[0].edits[-1][0] == coach_chat.BROKEN
    assert len(port.calls) == 1 and coach_chat.history(w.user.id, 1000.0) == []


async def test_пустой_баланс_человеку_и_одно_сообщение_владельцу():
    class NoCredits(Exception):
        code = "insufficient_quota"

    port = StubCoachPort(fail=NoCredits())
    w = await _world(port)
    alert = AsyncMock()
    msg = await _ask(w, _Chat(), "вопрос?", on_no_credits=alert)
    assert msg.sent[0].edits[-1][0] == coach_chat.NO_CREDITS
    assert alert.await_count == 1 and len(port.calls) == 1


async def test_перегрузка_openai():
    import httpx
    from openai import RateLimitError

    resp = httpx.Response(429, request=httpx.Request("POST", "https://api.openai.com"))
    port = StubCoachPort(fail=RateLimitError("tpm", response=resp, body=None))
    w = await _world(port)
    msg = await _ask(w, _Chat(), "вопрос?")
    assert msg.sent[0].edits[-1][0] == coach_chat.OVERLOADED


# --- учёт и повторы (AC-156) ----------------------------------------------------------------


async def test_расход_один_раз_за_раунд_бот_в_учёт_не_пишет():
    port = StubCoachPort(rounds=2)
    w = await _world(port)
    await _ask(w, _Chat(), "откуда калории?")
    assert port.noted == 2                        # адаптер: раунд — запись
    assert await w.spend.spent_this_month() == 0  # бот сам расход не пишет


async def test_сбой_последней_правки_ответ_новым_сообщением_без_повтора_модели():
    port = StubCoachPort()
    w = await _world(port)
    msg = _Chat()
    real = msg.answer

    async def answer(text, reply_markup=None):
        sent = await real(text, reply_markup)
        if text == coach_chat.THINKING:   # заглушка не принимает ни одной правки
            sent.fail = [TelegramRetryAfter(method=None, message="flood", retry_after=1),
                         TelegramBadRequest(method=None, message="message to edit not found")]
        return sent

    msg.answer = answer
    await _ask(w, msg, "вопрос?")
    assert len(port.calls) == 1
    assert "молоко 3.2%" in msg.sent[-1].text and len(msg.sent) == 2


# --- память разговора (AC-157) --------------------------------------------------------------


async def test_память_шесть_реплик_и_тридцать_минут_тишины():
    clock = _Clock()
    port = StubCoachPort()
    w = await _world(port)
    await _ask(w, _Chat(1), "откуда калории?", clock=clock)
    clock.t += 5 * 60
    await _ask(w, _Chat(2), "разложи", clock=clock)
    assert port.calls[1]["history"] == [("user", "откуда калории?"),
                                        ("assistant", "Больше всего дают молоко 3.2%.")]
    for i in range(3, 6):
        await _ask(w, _Chat(i), f"ещё {i}", clock=clock)
    assert len(port.calls[-1]["history"]) == 6   # тот же потолок, что у аппки (MA-INV-66)
    clock.t += 31 * 60
    await _ask(w, _Chat(9), "а теперь?", clock=clock)
    assert port.calls[-1]["history"] == []


# --- после ревью: явный вопрос до разбора, продолжение, отказ разбора (AC-158…AC-161) -------


async def test_явный_вопрос_про_еду_к_ассистенту_без_разбора():
    port = StubCoachPort()
    w = await _world(port)
    for i, q in enumerate(["можно ли есть банан на ночь?", "Стоит ли есть гречку вечером?",
                           "сколько белка мне нужно?", "почему я не худею?"]):
        await _say(w, q)
        coach_chat._threads.clear()          # без продолжения: каждый вопрос — сам по себе
        coach_chat._limiter._hits.clear()
        assert len(port.calls) == i + 1
    assert w.nlu.texts == [] and w.meals._meals == []   # ни разбора, ни фантомного приёма


async def test_количество_без_зачина_и_зачин_без_вопроса_остаются_записью():
    port = StubCoachPort()
    w = await _world(port, _meal("гречка"), _meal("творог"), _meal("банан"))
    await _say(w, "гречка 150 г?")
    await _say(w, "творог 200 г?")
    await _say(w, "можно ли есть банан на ночь")     # без «?» — через разбор
    assert port.calls == [] and len(w.nlu.texts) == 3 and len(w.meals._meals) == 3


async def test_короткое_продолжение_в_двух_минутах_иначе_запись():
    port = StubCoachPort()
    w = await _world(port, _meal("гречка"))
    await _say(w, "почему я не худею?")
    await _say(w, "а гречка?")                      # сразу после ответа — продолжение
    assert len(port.calls) == 2 and len(port.calls[1]["history"]) == 2 and w.nlu.texts == []
    coach_chat._threads[w.user.id].at -= 3 * 60     # прошло 3 минуты
    await _say(w, "а гречка?")
    assert len(port.calls) == 2 and w.nlu.texts == ["а гречка?"] and len(w.meals._meals) == 1


async def test_сбой_разбора_до_ассистента_не_доходит():
    port = StubCoachPort()
    w = await _world(port, RuntimeError("nlu down"))
    with pytest.raises(RuntimeError):
        await _say(w, "что это было?")             # не явный вопрос — идёт разбором
    assert port.calls == []


async def test_отказ_разбора_вопрос_к_ассистенту_прочее_не_понял():
    refused = ParsedMessage(intent=Intent.UNKNOWN, confidence=0.0)   # parsed is None у адаптера
    port = StubCoachPort()
    w = await _world(port, refused, refused)
    await _say(w, "а это нормально?")
    assert len(port.calls) == 1
    coach_chat._threads.clear()
    msg = await _say(w, "ну такое")
    assert len(port.calls) == 1 and any("Не понял" in t for t in _bot_texts(msg))


async def test_голосовой_вопрос_к_ассистенту():
    port = StubCoachPort()
    w = await _world(port, stt=StubStt("можно ли есть банан на ночь?"))
    msg = _tg("")
    msg = msg.model_copy(update={"text": None, "voice": Voice.model_construct(
        file_id="f", file_unique_id="u", duration=2)}).as_(msg.bot)
    msg.bot.download = AsyncMock(return_value=io.BytesIO(b"ogg"))
    await handlers.on_voice(msg, msg.bot, w.user, w.assistant, AsyncMock(), w.state,
                            container=w.container)
    assert [c["question"] for c in port.calls] == ["можно ли есть банан на ночь?"]
    assert w.nlu.texts == [] and w.meals._meals == []


# --- флуд-контроль и потолок в потоке (AC-162, AC-163) ---------------------------------------


async def test_флуд_retry_after_10_пауза_целиком_не_вылетает_память_пуста():
    port = StubCoachPort()
    w = await _world(port)
    msg = _Chat()
    real = msg.answer
    flood = TelegramRetryAfter(method=None, message="flood", retry_after=10)

    async def answer(text, reply_markup=None):
        if text != coach_chat.THINKING:
            raise flood                              # запасная отправка — тоже 429
        sent = await real(text, reply_markup)
        sent.fail = [flood, flood]                   # последняя правка — 429 дважды
        return sent

    msg.answer = answer
    sleep = AsyncMock()
    await coach_chat.ask(msg, w.user, w.coach, "вопрос?", clock=_Clock(), sleep=sleep)
    assert [c.args[0] for c in sleep.await_args_list] == [10.0, 10.0]   # весь retry_after
    assert len(port.calls) == 1
    assert coach_chat.history(w.user.id, 1000.0) == []                 # не доставлено


async def test_потолок_между_проверкой_и_потоком():
    class Spend(InMemorySpendRepo):
        def __init__(self):
            super().__init__()
            self.n = 0

        async def spent_this_month(self, *, purpose=None):
            self.n += 1
            return 0.0 if self.n == 1 else 99.0      # второй взгляд — уже за потолком

    port = StubCoachPort()
    w = await _world(port, spend=Spend())
    msg = await _ask(w, _Chat(), "вопрос?")
    assert msg.sent[0].edits[-1][0] == LIMIT_MESSAGE and port.calls == []


# --- дисклеймер раз на разговор, пустой ответ не в памяти (AC-164) -------------------------


async def test_дисклеймер_раз_на_разговор_и_снова_после_истечения():
    clock = _Clock()
    port = StubCoachPort()
    w = await _world(port)
    first = await _ask(w, _Chat(), "откуда калории?", clock=clock)
    second = await _ask(w, _Chat(), "а белок?", clock=clock)
    assert DISCLAIMER in first.sent[0].edits[-1][0]
    assert DISCLAIMER not in second.sent[0].edits[-1][0]
    clock.t += 31 * 60
    third = await _ask(w, _Chat(), "а теперь?", clock=clock)
    assert DISCLAIMER in third.sent[0].edits[-1][0]


async def test_пустой_ответ_не_становится_репликой():
    port = StubCoachPort([CoachChunk(kind="delta", text="   ")])
    w = await _world(port)
    msg = await _ask(w, _Chat(), "вопрос?")
    assert msg.sent[0].edits[-1][0].startswith(coach_chat.EMPTY)
    assert coach_chat.history(w.user.id, 1000.0) == []


async def test_количество_сразу_после_ответа_идёт_записью():
    """AC-165: правило продолжения не глотает «гречка 150 г?» — с количеством всегда разбор."""
    port = StubCoachPort()
    w = await _world(port, _meal("гречка"), _meal("гречка"), _meal("творог"))
    await _say(w, "почему я не худею?")
    await _say(w, "а гречка?")                          # без количества — продолжение
    assert len(port.calls) == 2 and w.nlu.texts == []
    await _say(w, "гречка 150 г?")                      # сразу после ответа — всё равно запись
    await _say(w, "а полпорции?")
    await _say(w, "можно ли съесть 200 г творога?")     # количество снимает зачин
    assert len(port.calls) == 2
    assert w.nlu.texts == ["гречка 150 г?", "а полпорции?", "можно ли съесть 200 г творога?"]
    assert len(w.meals._meals) == 3
    coach_chat._threads.clear()
    await _say(w, "сколько белка в 150 г гречки?")      # «сколько» — количество часть вопроса
    assert len(port.calls) == 3 and len(w.nlu.texts) == 3
