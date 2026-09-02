"""Разбор ответа на вопрос «какая порция?».

Логика жила только в `app/bot/handlers.py`, и аппка не могла довести уточнение
порции до записи — ответ «300 г» уходил в путь переименования и становился
названием продукта. Приём терялся молча, а это ровно то, против чего написан
§4.0 («провал — это не записать»).

Домен: чистый разбор строки, без сущностей и репозиториев.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")

# Порядок важен: «небольшая» содержит «больш», поэтому «мал» проверяется первым,
# а «больш» — последним.
_WORDS = (("мал", "small"), ("средн", "medium"), ("больш", "large"))


@dataclass(frozen=True)
class PortionAnswer:
    """Что человек ответил: размер словом или вес числом. Ровно одно из двух."""

    size: str | None = None      # small | medium | large
    grams: float | None = None

    @property
    def understood(self) -> bool:
        return self.size is not None or self.grams is not None


def parse(text: str) -> PortionAnswer:
    low = (text or "").lower()
    if "небольш" in low:
        # «небольшая» это «маленькая», а не «большая» — подстрока обманула бы.
        return PortionAnswer(size="small")
    for stem, size in _WORDS:
        if stem in low:
            return PortionAnswer(size=size)
    m = _NUMBER.search(low)
    if m:
        grams = float(m.group().replace(",", "."))
        # Ноль и отрицательное невозможны, а вес больше пяти килограммов за приём
        # означает опечатку, а не еду.
        if 0 < grams <= 5000:
            return PortionAnswer(grams=grams)
    return PortionAnswer()


def demo() -> None:
    """Runnable-чек: слова, числа и ловушки подстрок."""
    assert parse("средняя").size == "medium"
    assert parse("Большая порция").size == "large"
    assert parse("маленькую").size == "small"
    assert parse("небольшая").size == "small"       # ловушка: содержит «больш»
    assert parse("250").grams == 250
    assert parse("250,5 г").grams == 250.5
    assert parse("грамм 300").grams == 300
    assert not parse("не знаю").understood
    assert not parse("").understood
    assert not parse("0").understood                # ноль это не порция
    assert not parse("99999").understood            # опечатка, а не еда
    print("разбор ответа о порции: ок")


if __name__ == "__main__":
    demo()
