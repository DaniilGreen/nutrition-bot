"""Ассистент в чате бота (FR-45, D-MA-27).

Тот же `Coach.ask_stream`, что у SSE-ручки вкладки AI: промпт, инструменты только на чтение,
потолок раундов, учёт по раундам и потолок месяца живут там и здесь не повторяются. Своё у
бота — только транспорт: вместо SSE заглушка «🤔 Думаю…», которую правим по мере ответа.

Ответ модели не перезапрашивается никогда. Всё, что падает после первого байта, — потеря
показа, а не повод платить второй раз (AI-PRINCIPLES §1–2).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from html import escape
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from openai import RateLimitError

from app.application.coach import (
    DISCLAIMER,
    LIMIT_MESSAGE,
    QUESTION_MAX_CHARS,
    RATE_LIMIT,
    RATE_WINDOW_S,
    Coach,
    LimitReached,
    Turn,
    clean_history,
)
from app.application.entities import User
from app.application.rate_limit import RateLimiter
from app.bot.keyboards import FREQ_NAME_MAX, FREQ_PREFIX
from app.domain.mentions import Candidate
from app.domain.question import has_amount, strong_question
from app.infrastructure.openai_compat import out_of_credits

log = logging.getLogger(__name__)

# У Telegram около сообщения в секунду на чат, правки считаются тоже: чаще — ловим 429.
EDIT_EVERY_S = 1.5
RETRY_CAP_S = 30.0  # дольше 30 с человек ждать не станет — шлём, что есть, или сдаёмся
# Telegram режет на 4096 знаках ПОСЛЕ разбора разметки; запас — под дисклеймер и «…».
CHUNK_CHARS = 3800
THREAD_IDLE_S = 30 * 60  # тишина дольше — разговор заново, как «закрыл экран» в аппке
FOLLOWUP_S = 2 * 60      # «а гречка?» сразу после ответа — продолжение, а не запись (FR-45 в)

THINKING = "🤔 Думаю…"
# Строка состояния — куда полезла модель (AI-PRINCIPLES §6), те же слова, что у аппки.
TOOL_SAYS = {"day_totals": "дни", "meals_on": "приёмы", "top_products": "продукты",
             "weight_log": "вес"}
HINT = ("🤖 Спроси про свои данные: <code>/ask чем добрать белок?</code>\n"
        "Или просто напиши вопрос со знаком «?».")
TOO_LONG = "Слишком длинный вопрос."
UNAVAILABLE = "Ассистент недоступен: не настроен ключ."
TOO_OFTEN = "Слишком часто. Подожди немного."
BUSY = "⏳ Ещё отвечаю на прошлый вопрос — дождись ответа."
NO_CREDITS = "🔌 Нейронка временно недоступна — владелец уже знает. Спроси чуть позже."
OVERLOADED = "⏳ ИИ перегружен. Подожди ~10 секунд и спроси ещё раз."
BROKEN = "Ответ оборвался. Попробуй ещё раз."
EMPTY = "Не нашёл, что ответить по этим данным."

_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")


@dataclass
class _Thread:
    turns: list[Turn]
    at: float  # когда доставлен последний ответ


# ponytail: всё — память процесса бота. Рестарт (деплой) обнуляет разговор и окна; потолок —
# один вопрос без контекста. Хранить разговор в базе — пересмотр D-MA-17 ради двух человек.
_threads: dict[int, _Thread] = {}
_asking: set[int] = set()
_limiter = RateLimiter()


def reset() -> None:
    """Для тестов: чистая память между сценариями."""
    global _limiter
    _threads.clear()
    _asking.clear()
    _limiter = RateLimiter()


def direct(user_id: int | None, text: str, now: float | None = None) -> bool:
    """FR-45 (б, в): к ассистенту без разбора — явный вопрос или «…?» сразу после ответа."""
    if strong_question(text):
        return True
    # С количеством — всегда разбор: «гречка 150 г?» сразу после ответа — запись, а не вопрос.
    if user_id is None or not text.strip().endswith("?") or has_amount(text):
        return False
    found = _threads.get(user_id)
    now = time.monotonic() if now is None else now
    return found is not None and now - found.at <= FOLLOWUP_S


def render(text: str) -> str:
    """Как у аппки (§6.21.2): экранируем всё, из разметки модели — только **жирный**. Замена
    после escape — в тексте уже нет ни одного «<», HTML модели в чат не попадёт."""
    return _BOLD.sub(r"<b>\1</b>", escape(text, quote=False))


def split(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Ответ — кусками ≤ size: по абзацу, строке, предложению, пробелу; иначе — жёстко.
    Пару ** не рвём: непарная звёздочка в конце куска уезжает в следующий."""
    rest = text.strip()
    out: list[str] = []
    while len(rest) > size:
        cut = size
        for sep, keep in (("\n\n", 0), ("\n", 0), (". ", 1), (" ", 0)):
            i = rest.rfind(sep, size // 2, size)
            if i > 0:
                cut = i + keep
                break
        piece = rest[:cut]
        if piece.count("**") % 2:
            j = piece.rfind("**")
            if j > 0:
                cut = j
                piece = rest[:cut]
        out.append(piece.rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        out.append(rest)
    return out


def history(user_id: int, now: float) -> list[Turn]:
    found = _threads.get(user_id)
    if found is None or now - found.at > THREAD_IDLE_S:
        _threads.pop(user_id, None)
        return []
    return list(found.turns)


def _remember(user_id: int, question: str, answer: str, now: float) -> None:
    turns = [*history(user_id, now), ("user", question), ("assistant", answer)]
    # Тот же фильтр, что у истории от аппки (MA-INV-66): 6 реплик, 2000 знаков на реплику.
    _threads[user_id] = _Thread(clean_history([{"role": r, "text": t} for r, t in turns]), now)


def actions_keyboard(actions: Sequence[Candidate]) -> InlineKeyboardMarkup | None:
    """«Записать …» на продукты, которые человек уже ел (AC-MA-40) — кнопкой «⚡ Частое»
    (FR-40): та же запись `log_quick`, без модели. Граммы — обычная порция человека (медиана
    его записей), как у чипа аппки, а не число из текста ответа. Ассистент сам не пишет."""
    rows = []
    for a in actions:
        grams = round(a.grams, 1)
        if not 0 < grams <= 5000:
            continue
        name = a.name if len(a.name) <= FREQ_NAME_MAX else a.name[: FREQ_NAME_MAX - 1] + "…"
        rows.append([InlineKeyboardButton(
            text=f"Записать {name} · {grams:g} г",
            callback_data=f"{FREQ_PREFIX}:p:{a.product_id}:{grams:g}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _failure(exc: BaseException) -> str:
    if isinstance(exc, LimitReached):   # потолок достигнут между проверкой и потоком
        return LIMIT_MESSAGE
    if out_of_credits(exc):
        return NO_CREDITS
    if isinstance(exc, RateLimitError):
        return OVERLOADED
    return BROKEN


async def ask(
    message: Any,
    user: User,
    coach: Coach | None,
    question: str,
    *,
    on_no_credits: Callable[[], Awaitable[None]] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Вопрос → ответ в чате. Всё, что может отказать, — до заглушки (как MA-INV-41)."""
    question = question.strip()
    if not question:
        await message.answer(HINT)
        return
    if len(question) > QUESTION_MAX_CHARS:
        await message.answer(TOO_LONG)
        return
    if coach is None or user.id is None:
        await message.answer(UNAVAILABLE)
        return
    uid = user.id
    # Проверка и отметка — без await между ними: два апдейта подряд не проскочат оба.
    if uid in _asking:
        await message.answer(BUSY)
        return
    _asking.add(uid)
    try:
        if not _limiter.allow(uid, "coach", limit=RATE_LIMIT, window=RATE_WINDOW_S):
            await message.answer(TOO_OFTEN)
            return
        try:
            await coach.ensure_budget()
        except LimitReached:
            await message.answer(LIMIT_MESSAGE)
            return
        await _stream(message, user, coach, question, on_no_credits, clock, sleep)
    finally:
        _asking.discard(uid)


async def _stream(
    message: Any, user: User, coach: Coach, question: str,
    on_no_credits: Callable[[], Awaitable[None]] | None,
    clock: Callable[[], float], sleep: Callable[[float], Awaitable[None]],
) -> None:
    assert user.id is not None
    turns = history(user.id, clock())
    try:
        placeholder = await message.answer(THINKING)
    except TelegramRetryAfter:
        placeholder = None  # без заглушки: ответ уйдёт новым сообщением в конце
    parts: list[str] = []
    status = THINKING
    shown = THINKING
    last = clock()
    final = None
    try:
        async for chunk in coach.ask_stream(user, question, turns):
            if chunk.kind == "delta":
                parts.append(chunk.text)
            elif chunk.kind == "reset":
                parts.clear()       # преамбула перед вызовом инструмента — стираем
            elif chunk.kind == "tool":
                status = f"🔎 Смотрю {TOOL_SAYS.get(chunk.text, 'данные')}…"
            elif chunk.kind == "end":
                final = chunk
                continue
            if placeholder is None or clock() - last < EDIT_EVERY_S:
                continue
            view = _draft(parts, status)
            if view != shown:
                last = await _soft_edit(placeholder, view, clock)
                shown = view
    except Exception as exc:  # noqa: BLE001 — отказ показываем на месте заглушки
        if not isinstance(exc, LimitReached):
            log.exception("ассистент в боте: поток оборвался")
        if out_of_credits(exc) and on_no_credits is not None:
            try:
                await on_no_credits()
            except Exception:  # noqa: BLE001 — алерт владельцу не важнее ответа человеку
                log.warning("не ушёл алерт о балансе", exc_info=True)
        await _settle(placeholder, message, _failure(exc), None, sleep)
        return

    answer = (final.text if final is not None else "".join(parts)).strip()
    pieces = split(answer or EMPTY)
    markup = actions_keyboard(final.actions) if final is not None else None
    # Дисклеймер — раз на разговор: под каждым ответом он съедает экран и перестаёт читаться.
    footer = f"\n\n<i>{escape(DISCLAIMER)}</i>" if not turns else ""
    delivered = True
    for i, piece in enumerate(pieces):
        lastp = i == len(pieces) - 1
        html = render(piece) + (footer if lastp else "")
        kb = markup if lastp else None
        if i == 0:
            delivered &= await _settle(placeholder, message, html, kb, sleep)
        else:
            delivered &= await _send(message, html, kb, sleep) is not None
    # Память — только доставленному и непустому: иначе модель «помнила» бы то, чего человек
    # не видел, а «не нашёл, что ответить» — не слова ассистента.
    if final is not None and answer and delivered:
        _remember(user.id, question, answer, clock())


def _draft(parts: list[str], status: str) -> str:
    text = "".join(parts).strip()
    if not text:
        return status
    return render(split(text)[0]) + " …"


async def _soft_edit(placeholder: Any, html: str, clock: Callable[[], float]) -> float:
    """Промежуточная правка: не прошла — не беда, следующая догонит. Возвращает момент, с
    которого считать паузу; 429 от Telegram отодвигает его на сколько велели."""
    try:
        await placeholder.edit_text(html)
    except TelegramRetryAfter as exc:
        return clock() + exc.retry_after
    except Exception:  # noqa: BLE001
        log.debug("промежуточная правка ответа не прошла", exc_info=True)
    return clock()


async def _pause(exc: TelegramRetryAfter, sleep: Callable[[float], Awaitable[None]]) -> None:
    await sleep(min(float(exc.retry_after), RETRY_CAP_S))


async def _send(
    message: Any, html: str, markup: InlineKeyboardMarkup | None,
    sleep: Callable[[float], Awaitable[None]],
) -> Any | None:
    """Новое сообщение; 429 — пауза на весь `retry_after` и ещё раз. Второй 429 — None, а не
    исключение: 429 из обработчика не вылетает (FR-45, флуд-контроль)."""
    for attempt in range(2):
        try:
            return await message.answer(html, reply_markup=markup)
        except TelegramRetryAfter as exc:
            if attempt == 0:
                await _pause(exc, sleep)
    log.warning("ответ ассистента не доставлен: флуд-контроль Telegram")
    return None


async def _settle(
    placeholder: Any, message: Any, html: str, markup: InlineKeyboardMarkup | None,
    sleep: Callable[[float], Awaitable[None]],
) -> bool:
    """Последняя правка обязана дойти: 429 — пауза и ещё раз; не вышло — новым сообщением.
    Модель при этом не зовём: ответ уже оплачен и лежит у нас. True — доставлено."""
    if placeholder is not None:
        for attempt in range(2):
            try:
                await placeholder.edit_text(html, reply_markup=markup)
                return True
            except TelegramRetryAfter as exc:
                if attempt == 0:
                    await _pause(exc, sleep)
                    continue
            except TelegramBadRequest as exc:
                if "not modified" in str(exc).lower():
                    return True
            except Exception:  # noqa: BLE001
                log.warning("последняя правка ответа не прошла", exc_info=True)
            break
    return await _send(message, html, markup, sleep) is not None
