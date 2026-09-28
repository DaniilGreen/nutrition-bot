"""настройки инсталляции: месячный потолок трат на модель (§6.5, MA-INV-65)

Отдельная таблица, а не колонка в `users`: потолок общий на всех, платит владелец. Ключ →
значение, потому что настройка пока одна, а таблица под одну колонку — это миграция на каждую
следующую. Пустая таблица — действует `AI_MONTH_LIMIT_USD` из окружения: так откат и
восстановление сервера ничего не ломают.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
