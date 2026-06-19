"""add users.onboarded_at (first-run onboarding, FR-24/D-12)

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("onboarded_at", sa.DateTime(timezone=True), nullable=True))
    # существующие юзеры уже пользуются ботом → помечаем онбординнутыми, чтобы визард не приставал
    op.execute("UPDATE users SET onboarded_at = COALESCE(created_at, now())")


def downgrade() -> None:
    op.drop_column("users", "onboarded_at")
