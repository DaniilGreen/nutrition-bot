"""Поиск групп дублей в каталоге и выбор выжившего.

Логика жила только внутри миграции `0008` и была одноразовой: слить дубли,
появившиеся позже, было нечем. Здесь она становится доступной рантайму —
и боту, и аппке, — а миграция остаётся историческим слепком.

Чего здесь НЕТ намеренно: самого слияния. Оно трогает БД, значит живёт
в репозитории; домен только решает, что с чем считать одной едой.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.application.meal_service import _coverage, _names_compatible, _qualifiers

# Порог, при котором две записи считаются одной едой по симметричному покрытию имён.
RAG_THRESHOLD = 0.72

# Разброс плотности, выше которого слияние заметно двинуло бы цифры. Такие группы
# показываем, но не сливаем автоматически — решает человек (политика владельца, D-32).
SAFE_SPREAD = 0.15

SOURCE_RANK = {"curated": 3, "label": 3, "manual": 2, "llm": 0}

_NUM_SUFFIX = re.compile(r"_[0-9]+$")


def base_key(canonical_key: str) -> str:
    """`борщ_3` и `борщ` — одна еда: суффикс появился от гарда имени, не от смысла."""
    return _NUM_SUFFIX.sub("", canonical_key)


@dataclass
class Candidate:
    """Минимум, нужный для группировки. Не `Product`, чтобы не тащить сюда весь
    каталог: репозиторий отдаёт только эти поля плюс счётчик использований."""

    id: int
    canonical_key: str
    display_name: str
    source: str
    kcal_per_100g: float
    uses: int


def group(rows: list[Candidate]) -> list[list[Candidate]]:
    """Разложить каталог на группы одной еды.

    Порядок обхода — по id, то есть по хронологии пиннинга: голова группы та,
    что появилась раньше. Это дух INV-2 — первый пин задаёт истину.
    """
    groups: list[list[Candidate]] = []
    for row in sorted(rows, key=lambda r: r.id):
        head = next(
            (
                g
                for g in groups
                if base_key(g[0].canonical_key) == base_key(row.canonical_key)
                and _names_compatible(row.display_name, g[0].display_name)
            ),
            None,
        )
        if head is None:
            # Ключ не подтверждает — сравниваем имена симметрично (INV-5.1).
            candidates = [
                g
                for g in groups
                if _qualifiers(row.display_name) == _qualifiers(g[0].display_name)
                and min(
                    _coverage(row.display_name, g[0].display_name),
                    _coverage(g[0].display_name, row.display_name),
                )
                >= RAG_THRESHOLD
            ]
            head = max(
                candidates,
                key=lambda g: _coverage(row.display_name, g[0].display_name),
                default=None,
            )
        (groups.append([row]) if head is None else head.append(row))
    return [g for g in groups if len(g) > 1]


def spread(members: list[Candidate]) -> float:
    """Относительный разброс калорийности внутри группы."""
    kcals = [m.kcal_per_100g for m in members]
    lo, hi = min(kcals), max(kcals)
    return (hi - lo) / lo if lo > 0 else float("inf")


def winner(members: list[Candidate]) -> Candidate:
    """Кто переживёт слияние: выверенное > ручное > оценка; при равенстве —
    чаще используемый, затем старший по id."""
    return max(members, key=lambda m: (SOURCE_RANK.get(m.source, 0), m.uses, -m.id))


def demo() -> None:
    """Runnable-чек: группировка и выбор выжившего на понятном примере."""
    rows = [
        Candidate(1, "borscht", "борщ", "llm", 60.0, 3),
        Candidate(2, "borscht_2", "борщ", "manual", 62.0, 1),
        Candidate(3, "borscht_3", "борщ", "llm", 130.0, 0),
        Candidate(4, "chicken_breast", "куриная грудка", "curated", 165.0, 9),
    ]
    gs = group(rows)
    assert len(gs) == 1, gs                      # грудка одна, в группу не попала
    g = gs[0]
    assert [m.id for m in g] == [1, 2, 3]
    assert winner(g).id == 2                     # manual бьёт llm, хоть и реже используется
    assert spread(g) > SAFE_SPREAD               # 60 → 130 слишком далеко, руками
    assert spread(g[:2]) < SAFE_SPREAD           # 60 → 62 безопасно
    assert base_key("borscht_12") == "borscht"
    print("группировка дублей: ок")


if __name__ == "__main__":
    demo()
