"""Сравнение названий еды: одна ли это еда.

Правила жили в `app/application/meal_service.py`, и `app/domain/duplicates.py`
импортировал их оттуда — домен зависел от вышележащего слоя, да ещё и от приватных
имён. CLAUDE.md §1 объявляет domain чистым; здесь он таким и стал.

Имена оставлены приватными по стилю (ведущее подчёркивание) намеренно: это деталь
метрики, а не публичный контракт. Меняешь их — меняешь смысл и слияния дублей,
и переиспользования плотностей (INV-4).
"""
from __future__ import annotations

import difflib
import re
from functools import lru_cache

# Доля токенов имени, нашедших пару, при которой это одна еда (INV-4.2).
_NAME_THRESHOLD = 0.72


def _names_compatible(parsed_name: str, pinned_name: str) -> bool:
    """Имя из ввода и имя запиннутого продукта — одна еда? Защита от reuse ЧУЖОГО ключа: LLM
    иногда отдаёт ключ другого продукта (молоко миндальное → macadamia_milk «молоко макадамии»).

    INV-5.1: асимметрично — ключ уже утверждает идентичность, имя ловит грубый промах, поэтому
    уточнение засчитывается («куриная грудка» ⊂ «куриная грудка отварная»). INV-4.3 бьёт первой:
    разошлись числа-квалификаторы («творог 5%»/«творог 9%») — разная еда при любом покрытии.
    """
    if _qualifiers(parsed_name) != _qualifiers(pinned_name):
        return False
    best = max(_coverage(parsed_name, pinned_name), _coverage(pinned_name, parsed_name))
    return best >= _NAME_THRESHOLD


def _norm(text: str) -> str:
    return text.strip().lower()


# Порционные числа — шум имени («йогурт 200 г» и «йогурт» одна еда), в отличие от жирности.
_PORTION_RE = re.compile(r"\b\d+[.,]?\d*\s*(?:г|гр|грамм\w*|мл|л|кг|g|ml|l|kg|шт|скуп\w*)\b")
# Предлоги: еду не различают, но разбавляют счёт («арбузный сок БЕЗ сахара» vs «сок гуавы БЕЗ
# сахара» — 3 общих токена из 4). «сахара» НЕ выбрасываем: «кола без сахара» ≠ «кола» по ккал.
_STOP_WORDS = frozenset({"с", "со", "из", "и", "в", "на", "для", "без", "по"})


def _name_tokens(name: str) -> list[str]:
    """Слова имени: ё→е, без предлогов и без цифр. Цифры целиком на совести `_qualifiers` —
    иначе общая «3.2» тянет совпадение вверх («кофе с молоком 3.2%» ≈ «молоко 3.2%»)."""
    return list(_tokens(name))


# Кэш по строке имени: группировка дублей сравнивает каталог попарно, и без него каждое имя
# резалось на слова заново в каждой паре — 529 продуктов, 400 тыс. разборов, 4.5 с на открытие
# «Итогов» (16.10.2026). Функции чистые, кэш смысла не меняет.
@lru_cache(maxsize=8192)
def _tokens(name: str) -> tuple[str, ...]:
    words = re.findall(r"[^\W\d_]+", _PORTION_RE.sub(" ", _norm(name).replace("ё", "е")))
    return tuple([w for w in words if w not in _STOP_WORDS] or words)


@lru_cache(maxsize=8192)
def _prefixes(name: str) -> frozenset[str]:
    """Первые три буквы каждого слова. `_same_word` совпадает только при общем начале из 3 букв
    (или полном равенстве, а у равных слов и начало общее), поэтому имена без общих начал
    точно не совпадут ни одним словом — сравнивать их незачем."""
    return frozenset(t[:3] for t in _tokens(name))


def _same_word(a: str, b: str) -> bool:
    """Словоформы одного слова: общий префикс ≥3 символов И ≥60% ДЛИННОГО токена (INV-4.2).

    По длинному, а не короткому: иначе «сыр» ⊂ «сырники», «суши» ⊂ «сушёный», «бур» ⊂ «бургер» —
    разная еда с общим началом склеивается. ponytail: префикс вместо стеммера; русский флексирует
    хвостом, этого хватает. Понадобится больше — pymorphy/pg_trgm.
    """
    if a == b:
        return True
    common = 0
    for x, y in zip(a, b, strict=False):  # слова разной длины — обрезка по короткому тут и нужна
        if x != y:
            break
        common += 1
    return common >= 3 and common >= 0.6 * max(len(a), len(b))


def _coverage(name: str, other: str) -> float:
    """Доля токенов `name`, нашедших пару в `other`. НАПРАВЛЕННАЯ: _coverage(a,b) != _coverage(b,a).

    Заменила difflib по склеенной строке (D-32): та зависела от порядка слов («пиво нефильтрованное
    светлое» vs «светлое пиво нефильтрованное» = 0.714 при пороге 0.72), а в русском порядок
    свободный; и наоборот, роднила разные слова с общими буквами («жареные»/«варёные» = 0.86).
    """
    tokens, pool = _tokens(name), list(_tokens(other))
    if not tokens or not pool:
        return 0.0
    hit = 0
    for token in tokens:
        match = next((w for w in pool if _same_word(token, w)), None)
        if match is not None:
            pool.remove(match)
            hit += 1
    return hit / len(tokens)


def _qualifiers(name: str) -> list[str]:
    """Числа-квалификаторы имени (жирность «3.2%», «творог 5%») — часть идентичности (INV-4.3).
    Порционные числа вырезаны: «200 г» продукт не различает."""
    return list(_qualifiers_t(name))


@lru_cache(maxsize=8192)
def _qualifiers_t(name: str) -> tuple[str, ...]:
    stripped = _PORTION_RE.sub(" ", _norm(name).replace(",", "."))
    return tuple(sorted(re.findall(r"\d+[.,]?\d*", stripped)))


_KEY_RE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)+")


def looks_like_key(name: str) -> bool:
    """Модель иногда отдаёт в имени каталожный ключ («grapes_red_fresh»). Такое имя
    уезжает в ленту и в каталог как есть — человек его не писал и не узнает."""
    return bool(_KEY_RE.fullmatch((name or "").strip()))


def phrase_names(text: str) -> list[str]:
    """Имена из фразы-списка «имя N г, имя N г» — той, что собирает фото (FR-34)."""
    out = []
    for part in (text or "").split(","):
        name = re.sub(r"\s*\d+(?:[.,]\d+)?\s*(?:г|гр|грамм\w*|мл)?\s*$", "", part.strip())
        if name:
            out.append(name)
    return out


def _name_matches(query: str, candidate: str) -> bool:
    """Совпадение имени для «удали X» / «гречки было 150»: целыми словами со словоформами (ё=е,
    `_coverage`) — все слова короткого имени нашлись в длинном. Подстрокой «рис» ⊂ «рисовая
    лапша» и «ирис» склеивалась разная еда, и правка ответом попадала не в ту позицию (FR-44).
    """
    if not _norm(query):
        return False
    return _coverage(query, candidate) == 1.0 or _coverage(candidate, query) == 1.0


_TYPO_RATIO = 0.8  # «гречика» ≈ «гречка» (0,92); «рис» ≈ «ирис» (0,86) отсекает длина ≥ 4
_TYPO_MIN_LEN = 4  # короткое слово опечатку не опознать: три буквы — уже другое слово
_ENDINGS = "аеёиоуыэюяйь"  # гласная/й/ь на конце — падеж, а не опечатка


def _stem(word: str) -> str:
    return word[:-1] if word[-1:] in _ENDINGS else word  # одна буква: «куриная» ≠ «курицу»


def _typo_matches(query: str, candidate: str) -> bool:
    """Второй проход после строгого (FR-14.1): опечатка ЦЕЛОГО слова — каждое слово короткого
    имени нашло в длинном словоформу (`_same_word`) или слово с отличием в букву-две (difflib
    ≥ 0,8, оба ≥ 4 букв). Не подстрока: «рис» не попадает ни в «рисовую лапшу», ни в «ирис»."""
    def close(a: str, b: str) -> bool:
        if _same_word(a, b):
            return True
        if min(len(a), len(b)) < _TYPO_MIN_LEN:
            return False
        # Без окончания: «гречику» — опечатка И падеж, по целому слову 0,77, по основе 0,91.
        return any(difflib.SequenceMatcher(None, x, y).ratio() >= _TYPO_RATIO
                   for x, y in ((a, b), (_stem(a), _stem(b))))

    def covered(xs: tuple[str, ...], ys: tuple[str, ...]) -> bool:
        return bool(xs) and all(any(close(x, y) for y in ys) for x in xs)

    q, c = _tokens(query), _tokens(candidate)
    return covered(q, c) or covered(c, q)


def match_names(names: list[str], query: str) -> list[int]:
    """Индексы имён, совпавших с названным: строго (`_name_matches`), а нет строгих — опечатка
    целого слова, если она указывает на одно имя. Общее у ответа на карточку приёма (FR-44)
    и правки карточки фото (FR-34.1)."""
    hits = [i for i, n in enumerate(names) if _name_matches(query, n)]
    if hits:
        return hits
    typo = [i for i, n in enumerate(names) if _typo_matches(query, n)]
    return typo if len({_norm(names[i]) for i in typo}) == 1 else []


def demo() -> None:
    """Runnable-чек: одна еда против разной с общим началом."""
    assert looks_like_key("grapes_red_fresh")
    assert not looks_like_key("виноград красный")
    assert not looks_like_key("apple")
    assert phrase_names("виноград красный 79 г, яблоко целое 158 г") == [
        "виноград красный", "яблоко целое"
    ]
    assert _names_compatible("творог 5%", "Творог 5%")
    assert _names_compatible("куриная грудка", "грудка куриная")
    # Разная еда с общим префиксом склеиваться не должна (INV-4.2).
    assert not _names_compatible("сыр", "сырники")
    assert not _names_compatible("суши", "сушёный банан")
    # Числовые квалификаторы различают жирность.
    assert _qualifiers("творог 5%") != _qualifiers("творог 9%")
    assert _coverage("грудка", "куриная грудка отварная") > 0
    print("сравнение имён: ок")


if __name__ == "__main__":
    demo()
