"""«⚡ Частое» виджетом (FR-49.2, D-54): тап по плитке — запись, «⏪ Вернуть» — там же.

Запись — тем же путём, что кнопка FR-40 (`handlers._log_frequent` → `log_quick` / `repeat_meal`),
без модели. Граница доверия (W-INV-4): `data` — строго по формату, набор ищется среди своих,
продукт — в общем каталоге (как у кнопки FR-40), граммы в (0; 5000]. Идемпотентность (W-INV-5):
у каждого тапа свой случайный ключ, повтор с тем же ключом получает прежний ответ и не пишет.

ponytail: ключи и «что записал этот виджет» — в памяти процесса, окно 10 минут (как у «Вернуть»
FR-41). Рестарт бота стирает окно: повтор после рестарта — новая запись, «Вернуть» — отказ.
"""
from __future__ import annotations

import re
import time
from typing import Any

from aiogram import Bot

from app.application.assistant import Assistant
from app.application.entities import User
from app.application.meal_service import meal_fingerprint
from app.bot.keyboards import FREQ_PREFIX, FREQ_TOP, frequent_keyboard, open_in_app, set_key
from app.bot.ui.widgets import frequent_data
from app.bot.widgets import live, template
from app.bot.widgets.transport import SendWidget

TTL_S = 600
_WRITE = re.compile(r"w:(p:[0-9]{1,10}:[0-9]{1,4}(?:\.[0-9])?|s:[0-9a-f]{10}):([0-9a-z]{4,10})")
_UNDO = re.compile(r"u:([0-9]{1,10}):([0-9a-z]{4,10})")

_done: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}  # (кто, ключ) → (когда, ответ)
# (кто, id записи) → (когда записал этот виджет, отпечаток записи): «Вернуть» не снесёт правку.
_wrote: dict[tuple[int, int], tuple[float, object]] = {}
_now = time.monotonic  # отдельное имя — тест двигает часы виджета, не цикла asyncio


def _sweep(now: float) -> None:
    for key in [k for k, (at, _) in _done.items() if now - at > TTL_S]:
        del _done[key]
    for wkey in [k for k, (at, _) in _wrote.items() if now - at > TTL_S]:
        del _wrote[wkey]


async def on_write(
    uid: int, mid: int, data: str, user: User, assistant: Assistant
) -> live.Outcome:
    """`w:<операция>:<ключ>` — записать плитку (AC-276, AC-277)."""
    from app.bot.handlers import _log_frequent  # тот же путь, что у кнопки FR-40

    m = _WRITE.fullmatch(data)
    if m is None:
        return live.Outcome({"err": "bad"})
    now = _now()
    _sweep(now)
    key = (uid, m.group(2))
    seen = _done.get(key)
    if seen is not None:
        return live.Outcome(seen[1])  # повтор того же тапа — прежний ответ, второй записи нет
    op = m.group(1)
    if op.startswith("p:") and not 0 < float(op.split(":")[2]) <= 5000:
        return live.Outcome({"err": "bad"})
    # Ключ занят ДО записи: второй такой же запрос, пришедший, пока первый пишет, не запишет.
    _done[key] = (now, {"err": "busy"})
    ok = False
    try:
        logged = await _log_frequent(user, assistant, f"{FREQ_PREFIX}:{op}")
        ok = logged is not None and logged.meal.id is not None
    finally:
        if not ok:
            _done.pop(key, None)
    if logged is None or logged.meal.id is None:
        return live.Outcome({"err": "gone"})  # продукта/набора больше нет — плитка устарела
    meal, meal_id = logged.meal, logged.meal.id
    names = ", ".join(i.name_snapshot for i in meal.items[:2])
    reply: dict[str, Any] = {
        "ok": 1, "m": meal_id, "n": names[:40], "k": round(meal.totals.kcal),
        "l": round(logged.remaining.kcal_left),
    }

    def commit() -> None:
        _done[key] = (now, reply)
        _wrote[(uid, meal_id)] = (now, meal_fingerprint(meal))

    def abort() -> None:
        _done.pop(key, None)

    return live.Outcome(reply, commit=commit, abort=abort)


async def on_undo(
    uid: int, mid: int, data: str, user: User, assistant: Assistant
) -> live.Outcome:
    """`u:<id записи>:<ключ>` — «⏪ Вернуть» записи этого виджета за 10 минут (AC-278).

    Запись успели поправить («✏️ Вес», дописали позицию) — не удаляем: «Вернуть» снёс бы правку
    (тот же отпечаток, что у «⏪ Вернуть» FR-41)."""
    m = _UNDO.fullmatch(data)
    if m is None:
        return live.Outcome({"err": "bad"})
    now = _now()
    _sweep(now)
    key = (uid, m.group(2))
    seen = _done.get(key)
    if seen is not None:
        return live.Outcome(seen[1])
    meal_id = int(m.group(1))
    wrote = _wrote.get((uid, meal_id))
    if wrote is None:
        return live.Outcome({"err": "bad"})  # чужая, давняя или записанная не этим виджетом
    if meal_fingerprint(await assistant.get_meal(user, meal_id)) != wrote[1]:
        return live.Outcome({"err": "edited"})
    deleted = await assistant.delete_meal(user, meal_id)
    if deleted.meal is None:
        return live.Outcome({"err": "gone"})
    today = await assistant.today(user)
    reply: dict[str, Any] = {"ok": 1, "u": meal_id, "l": round(today.remaining.kcal_left)}

    def commit() -> None:
        _done[key] = (now, reply)
        _wrote.pop((uid, meal_id), None)

    return live.Outcome(reply, commit=commit)


live.ROUTES["w"] = on_write
live.ROUTES["u"] = on_undo


async def show(bot: Bot, chat_id: int, user: User, assistant: Assistant) -> bool:
    """Отправить виджет «⚡ Частое». False — показывать нечего или не вышло: вызывающий шлёт
    прежнюю клавиатуру FR-40 (W-INV-2)."""
    try:
        sets = await assistant.recent_sets(user, days=30)
        rows, products = [], {}
        for pid, _name, grams, times in await assistant.frequent(user, days=30, limit=FREQ_TOP):
            product = await assistant.get_product(pid) if pid else None
            if product is not None and pid is not None:
                rows.append((pid, product.display_name, grams, times))
                products[pid] = product
        markup = frequent_keyboard(sets, rows)
        if markup is None:
            return False
        by_key: dict[str, Any] = {}
        for s, _ in sets:  # свежие первыми — как берёт `_log_frequent`
            if s.template_name:
                by_key.setdefault(set_key(s.template_name), s)
        await bot(SendWidget(
            chat_id=chat_id, html=template("frequent"),
            chat_data=frequent_data(markup, products, by_key),
            caption="⚡ Частое — тап по плитке запишет сразу", allow_queries=True,
            reply_markup=open_in_app("cat"),
        ))
    except Exception as exc:  # noqa: BLE001
        live.logger.warning("widget: «Частое» не ушло (%s) — фолбэк на клавиатуру", exc)
        return False
    return True
