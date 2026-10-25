"""add meals.template_at — когда состав явно назван набором (FR-47)

Набором была запись с самым поздним `eaten_at`. «📌 Сохранить приём пищи набором» на вчерашнем
дне тогда проигрывал сегодняшнему повтору старого состава — новое имя не становилось набором.
`created_at` не годится: слитая запись берёт его от самой ранней (ради «↩️ Отменить»), а
«Вернуть» его обновляет. Колонка пустая у старых записей: для них порядок прежний — по
`eaten_at` (`coalesce(template_at, eaten_at)`), так что бэкфилл не нужен.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("meals", sa.Column("template_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("meals", "template_at")
