"""add products.photo_file_id (фото этикетки, FR-27)

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("photo_file_id", sa.String(length=256), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "photo_file_id")
