"""настройки интерфейса одной колонкой JSONB (§6.5)

Колонка на пункт означала бы восемь миграций на группу «Вид», причём эти
настройки вообще не влияют на данные — только на то, как они нарисованы.
JSONB даёт одну миграцию и свободу добавлять пункты без схемы.

Что сюда НЕ кладём: цели и часовой пояс. У них есть свои колонки и своя
семантика — они меняют расчёты, а не оформление.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "ui_prefs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "ui_prefs")
