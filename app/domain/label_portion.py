"""FR-27: сколько съели из того, что на этикетке.

Ответ на «сколько грамм?» после распознанной этикетки: число, «вся пачка»
(нетто с упаковки) или «N порций» (N × вес порции). Живёт в домене, потому
что путей к нему два — бот и мини-апп, — а правило одно. Раньше копия жила в
`app/bot/handlers.py`, и второй адаптер обязан был бы её продублировать.
"""
from __future__ import annotations

import re

_WHOLE = ("пачк", "упаков", "цел")
_WHOLE_EXACT = ("всё", "все", "всю")


def _first_number(text: str) -> float | None:
    m = re.search(r"\d+(?:[.,]\d+)?", text)
    return float(m.group().replace(",", ".")) if m else None


def label_grams(
    text: str, package_g: float | None, serving_g: float | None = None
) -> float | None:
    """Граммы из ответа. None — не разобрали: спрашиваем ещё раз, а не гадаем."""
    low = text.lower().strip()
    if package_g and (any(w in low for w in _WHOLE) or low in _WHOLE_EXACT):
        return package_g
    n = _first_number(text)
    if "порц" in low:
        # Без веса порции число порций в граммы не превращается — угадывать
        # тут значит записать выдуманный вес как факт.
        return (n or 1) * serving_g if serving_g else None
    return n if n and n > 0 else None


def demo() -> None:
    assert label_grams("250", 400, 30) == 250
    assert label_grams("вся пачка", 400, 30) == 400
    assert label_grams("всю", 400, 30) == 400
    assert label_grams("2 порции", 400, 30) == 60
    assert label_grams("1 порция", 400, 30) == 30
    # Нет веса порции — не выдумываем: «2 порции» без справки это не граммы.
    assert label_grams("2 порции", 400, None) is None
    # Нет веса пачки — «вся пачка» тоже не число.
    assert label_grams("вся пачка", None, 30) is None
    assert label_grams("0", 400, 30) is None
    assert label_grams("не знаю", 400, 30) is None
    assert label_grams("120,5", None, None) == 120.5
    print("label_portion ok")


if __name__ == "__main__":
    demo()
