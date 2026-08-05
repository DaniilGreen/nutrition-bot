"""Чистая логика миграции 0008: что склеиваем, что выживает, чего не трогаем (D-32/INV-15).

Строки — заглушки, БД не нужна. Проверяем на РЕАЛЬНЫХ парах из прод-каталога.
"""
from __future__ import annotations

import importlib.util
from collections import namedtuple
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations" / "versions" / "0008_merge_duplicate_products.py"
)
_spec = importlib.util.spec_from_file_location("migration_0008", _PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

Row = namedtuple("Row", "id base_key display_name source kcal_per_100g uses")


def test_groups_real_prod_duplicates() -> None:
    rows = [
        Row(1, "beer_unfiltered_light", "светлое пиво нефильтрованное", "llm", 45.0, 0),
        Row(2, "beer_unfiltered_light", "пиво нефильтрованное светлое", "llm", 42.0, 2),
        Row(3, "black_bread", "хлеб чёрный", "llm", 210.0, 1),
        Row(4, "black_bread", "сухари ржаные", "llm", 330.0, 1),  # ДРУГАЯ еда под тем же ключом
    ]
    groups = {g[0].id: [r.id for r in g] for g in m._group(rows)}

    assert groups[1] == [1, 2], "порядок слов — та же еда, дубль не заводим"
    assert groups[3] == [3], "сухари ржаные ≠ хлеб чёрный, слипаться не должны"
    assert groups[4] == [4]


def test_spread_gate_skips_groups_that_would_move_history() -> None:
    """Политика владельца: сливаем только < 15% разброса."""
    tight = [Row(1, "k", "а", "llm", 60.0, 1), Row(2, "k", "б", "llm", 62.0, 1)]
    wide = [Row(1, "k", "а", "llm", 155.0, 1), Row(2, "k", "б", "llm", 200.0, 1)]

    assert m._spread(tight) < m.MAX_SPREAD
    assert m._spread(wide) >= m.MAX_SPREAD  # яйцо варёное vs жареное — не наш случай


def test_winner_prefers_verified_then_most_used() -> None:
    curated = Row(1, "pasta_boiled", "Макароны отварные", "curated", 157.0, 3)
    guess = Row(2, "pasta_boiled", "отварные макароны", "llm", 130.0, 9)
    assert m._winner([guess, curated]).id == curated.id  # выверенное бьёт частоту

    old = Row(3, "egg_boiled", "яйцо варёное", "llm", 155.0, 1)
    frequent = Row(4, "egg_boiled", "варёные яйца", "llm", 155.0, 6)
    assert m._winner([old, frequent]).id == frequent.id  # среди равных — чаще используемый


def test_upgrade_touches_only_the_link_not_the_snapshot() -> None:
    """INV-3: миграция не имеет права переписать историю. Снапшот-колонки не должны появляться
    ни в одном SET — иначе день задним числом поедет."""
    source = _PATH.read_text()
    updates = [ln for ln in source.splitlines() if "UPDATE meal_items" in ln]

    assert updates, "миграция обязана перепривязывать product_id"
    for line in updates:
        assert "SET product_id" in line
        for snapshot in ("kcal", "protein", "fat", "carb", "grams", "is_estimate"):
            assert snapshot not in line, f"снапшот {snapshot} менять нельзя (INV-3)"
