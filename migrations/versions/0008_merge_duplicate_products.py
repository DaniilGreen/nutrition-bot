"""merge duplicate products (D-32/INV-15): один продукт — одна запиннутая плотность

97 из 258 продуктов прода имели суффикс `_2…_16`: гард имени отказывал на честной
переформулировке, и `_free_key` плодил `key_N`. Одна еда — несколько независимых плотностей,
INV-2 и INV-4 сломаны оба.

Группируем ТЕМИ ЖЕ правилами, что и рантайм (`meal_service`), чтобы миграция сливала ровно то,
что впредь не создастся. Политика владельца — консервативная: трогаем только группы с разбросом
плотности < 15%. Где разброс больше, слияние заметно двинуло бы цифры — такое чинится руками
через `/products`, где владелец видит число.

INV-15/INV-3: `meal_items` перепривязываются на выжившего, но их снапшот КБЖУ и граммов НЕ
трогается ни одной строкой SQL ниже. История не движется — меняется только связь с каталогом.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-05
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# ponytail: тянем предикаты из рантайма, а не копируем их сюда — иначе миграция и вход разъедутся
# по определению «одна еда». Цена: правка метрики меняет смысл этой миграции при накатке на
# восстановленный из бэкапа старый дамп. Разъедется на практике — заморозить копией правил здесь.
from app.application.meal_service import _coverage, _names_compatible, _qualifiers

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

MAX_SPREAD = 0.15  # политика владельца: сливаем только там, где плотности почти совпали
SOURCE_RANK = {"curated": 3, "label": 3, "manual": 2, "llm": 0}  # выверенное > ручное > оценка
RAG_THRESHOLD = 0.72

_PRODUCTS = sa.text(
    """
    SELECT p.id, regexp_replace(p.canonical_key, '_[0-9]+$', '') AS base_key,
           p.display_name, p.source, p.kcal_per_100g,
           (SELECT count(*) FROM meal_items mi WHERE mi.product_id = p.id) AS uses
    FROM products p
    ORDER BY p.id
    """
)


def _group(rows: list) -> list[list]:
    """Дубли одной еды. Обход по id — то есть по хронологии пиннинга: голова группы та, что
    появилась раньше, ровно как на входе."""
    groups: list[list] = []
    for row in rows:
        head = next(
            (g for g in groups
             if g[0].base_key == row.base_key
             and _names_compatible(row.display_name, g[0].display_name)),
            None,
        )
        if head is None:  # RAG-путь: ключ не подтверждает — сравниваем симметрично (INV-5.1)
            candidates = [
                g for g in groups
                if _qualifiers(row.display_name) == _qualifiers(g[0].display_name)
                and min(_coverage(row.display_name, g[0].display_name),
                        _coverage(g[0].display_name, row.display_name)) >= RAG_THRESHOLD
            ]
            head = max(
                candidates,
                key=lambda g: _coverage(row.display_name, g[0].display_name),
                default=None,
            )
        if head is None:
            groups.append([row])
        else:
            head.append(row)
    return groups


def _spread(members: list) -> float:
    kcals = [m.kcal_per_100g for m in members]
    lo, hi = min(kcals), max(kcals)
    return (hi - lo) / lo if lo > 0 else float("inf")


def _winner(members: list):
    """Выживший: выверенное > ручное > оценка; при равенстве — чаще используемый, затем старший
    по id (первый пин — дух INV-2)."""
    return max(members, key=lambda m: (SOURCE_RANK.get(m.source, 0), m.uses, -m.id))


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(_PRODUCTS).all()

    for members in _group(rows):
        if len(members) < 2 or _spread(members) >= MAX_SPREAD:
            continue
        winner = _winner(members)
        loser_ids = [m.id for m in members if m.id != winner.id]
        # Только связь. Снапшот (kcal/protein/fat/carb/grams/is_estimate) в SET не входит — INV-3.
        conn.execute(
            sa.text("UPDATE meal_items SET product_id = :win WHERE product_id = ANY(:losers)"),
            {"win": winner.id, "losers": loser_ids},
        )
        conn.execute(
            sa.text("DELETE FROM products WHERE id = ANY(:losers)"), {"losers": loser_ids}
        )


def downgrade() -> None:
    """Необратимо: проигравшие строки каталога удалены, обратно их взять неоткуда.
    Откат — только восстановлением из бэкапа (`scripts/restore_db.sh`, крон в /opt/backups)."""
