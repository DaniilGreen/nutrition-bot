"""add user_goals.target (cut|maintain|bulk) — раскладка макросов зависит от цели

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default="maintain" → существующие цели получают значение без ручного UPDATE.
    # maintain — безопасный дефолт: не задирает белок и не режет углеводы тем, кто цель не выбирал.
    op.add_column(
        "user_goals",
        sa.Column("target", sa.String(16), nullable=False, server_default="maintain"),
    )


def downgrade() -> None:
    op.drop_column("user_goals", "target")
