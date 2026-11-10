"""«⚡ Частое» (FR-40): общее для кнопок и виджета (FR-49.2) — что показать и как записать.

Одно место на оба канала: плитка виджета и кнопка клавиатуры — один топ, одни граммы и одна
запись (`log_quick` / `repeat_meal`), без модели. Канал добавляет только свой префикс
(`fq:` у кнопки, `w:` у запроса виджета) к операции `p:<product_id>:<граммы>` / `s:<ключ набора>`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.application.assistant import Assistant
from app.application.entities import Meal, Product, User
from app.application.results import MealLogged
from app.bot.keyboards import FREQ_TOP, FreqTile, frequent_tiles, set_key

OP = re.compile(r"p:([0-9]{1,10}):([0-9]{1,4}(?:\.[0-9])?)|s:([0-9a-f]{10})")
MAX_G = 5000


@dataclass
class Frequent:
    """Что показать в «⚡ Частое»: плитки и то, из чего они собраны (ккал плитки — отсюда)."""

    tiles: list[FreqTile]
    products: dict[int, Product]
    sets: dict[str, Meal]  # ключ набора → самый свежий приём с этим именем


async def frequent(user: User, assistant: Assistant) -> Frequent:
    """Тот же топ, что у чипов аппки: бот не показывает продукт, которого нет в её чипах. Имя —
    из каталога (так его запишет log_quick и покажет карточка), а не из снапшотов; продукт,
    которого в каталоге уже нет (слили/удалили), не показываем."""
    sets = await assistant.recent_sets(user, days=30)
    rows, products = [], {}
    for pid, _name, grams, times in await assistant.frequent(user, days=30, limit=FREQ_TOP):
        product = await assistant.get_product(pid) if pid else None
        if product is not None and pid is not None:
            rows.append((pid, product.display_name, grams, times))
            products[pid] = product
    by_key: dict[str, Meal] = {}
    for s, _ in sets:  # свежие первыми — как берёт `log_frequent`
        if s.template_name:
            by_key.setdefault(set_key(s.template_name), s)
    return Frequent(frequent_tiles(sets, rows), products, by_key)


def parse_op(op: str) -> tuple[int, float] | str | None:
    """Операция плитки строго по формату: (product_id, граммы) у продукта, ключ — у набора.
    None — кривая операция или граммы вне (0; 5000] (граница доверия, W-INV-4)."""
    m = OP.fullmatch(op)
    if m is None:
        return None
    if m.group(3):
        return m.group(3)
    grams = float(m.group(2))
    return (int(m.group(1)), grams) if 0 < grams <= MAX_G else None


async def log_frequent(user: User, assistant: Assistant, op: str) -> MealLogged | None:
    """Плитка → запись в сегодня тем же use-case, что у чипа аппки. None — плитка устарела
    (продукт слили или удалили, набор переименован) или операция кривая."""
    parsed = parse_op(op)
    if parsed is None:
        return None
    if isinstance(parsed, tuple):
        result = await assistant.log_quick(user, *parsed)
        return result if isinstance(result, MealLogged) else None
    # Набор — по имени (его идентичность), а не по id приёма: тот меняется с каждым повтором.
    # Повтор самого свежего приёма с этим именем — ровно то, что делает и чип аппки (FR-39.2).
    found = next(
        (m for m in await assistant.sets(user)
         if m.template_name and set_key(m.template_name) == parsed),
        None,
    )
    if found is None or found.id is None:
        return None
    return await assistant.repeat_meal(user, found.id)
