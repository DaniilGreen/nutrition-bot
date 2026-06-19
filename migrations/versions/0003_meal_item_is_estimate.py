"""add meal_items.is_estimate (per-item «≈» transparency, INV-13)

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default="false" → существующие строки получают значение без ручного UPDATE.
    op.add_column(
        "meal_items",
        sa.Column(
            "is_estimate", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )


def downgrade() -> None:
    op.drop_column("meal_items", "is_estimate")
