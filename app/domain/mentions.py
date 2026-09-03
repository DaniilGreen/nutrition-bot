"""Что из названного ассистентом можно записать одним тапом (§4.3, AC-MA-40).

«Совет, который нельзя выполнить в один тап, остаётся советом» — поэтому ответ
модели надо превратить в действия. Ищем в тексте продукты, которые человек уже
ел: у них известна и плотность, и типичная граммовка, значит запись не потребует
ни модели, ни вопросов.

Домен: только сопоставление строк, ни репозиториев, ни сущностей.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.naming import _name_tokens, _norm, _same_word

# Больше трёх кнопок под ответом — это уже меню, а не подсказка.
MAX_ACTIONS = 3
# Односложные имена («рис», «чай») ловятся почти в любом тексте и дают ложные
# срабатывания. Требуем, чтобы совпало не меньше двух слов, либо одно длинное.
MIN_TOKEN_LEN = 5


@dataclass(frozen=True)
class Candidate:
    product_id: int
    name: str
    grams: float


def mentioned(text: str, candidates: list[Candidate]) -> list[Candidate]:
    """Кандидаты, чьё имя встречается в тексте. Порядок — как пришли.

    Сопоставляем по словоформам (`_same_word`), а не по подстроке: «творог»
    в тексте должен ловить «Творог 5%», но «сыр» не должен ловить «сырники».
    """
    words = _name_tokens(_norm(text))
    if not words:
        return []
    found: list[Candidate] = []
    for c in candidates:
        tokens = _name_tokens(c.name)
        if not tokens:
            continue
        hits = sum(1 for t in tokens if any(_same_word(t, w) for w in words))
        strong = hits >= 2 or (hits == 1 and max(len(t) for t in tokens) >= MIN_TOKEN_LEN)
        if strong and not any(f.product_id == c.product_id for f in found):
            found.append(c)
        if len(found) == MAX_ACTIONS:
            break
    return found


def demo() -> None:
    """Runnable-чек: ловим названное, не ловим похожее."""
    cands = [
        Candidate(1, "куриная грудка", 200),
        Candidate(2, "творог 5%", 180),
        Candidate(3, "сырники", 150),
        Candidate(4, "рис", 100),
    ]
    got = mentioned("Добавь белка: подойдёт куриная грудка или творог.", cands)
    assert [c.product_id for c in got] == [1, 2], got

    # «сыр» внутри «сырники» не должен срабатывать: разная еда с общим началом.
    assert mentioned("возьми сыр", cands) == []

    # Короткое односложное имя не ловится по одному совпадению — иначе «рис»
    # находился бы в половине ответов.
    assert mentioned("это хороший рис", cands) == []

    # Не больше трёх кнопок.
    many = [Candidate(i, f"продукт{i} длинный", 100) for i in range(10)]
    assert len(mentioned(" ".join(c.name for c in many), many)) == MAX_ACTIONS

    assert mentioned("", cands) == []
    print("упоминания продуктов: ок")


if __name__ == "__main__":
    demo()
