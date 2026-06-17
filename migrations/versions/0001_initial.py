"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-06-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("telegram_id", sa.BigInteger, nullable=False),
        sa.Column("name", sa.String(255)),
        sa.Column("locale", sa.String(8), nullable=False, server_default="ru"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Europe/Moscow"),
        sa.Column("role", sa.String(16), nullable=False, server_default="user"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"], unique=True)

    op.create_table(
        "user_goals",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("daily_kcal", sa.Float, nullable=False),
        sa.Column("protein_g", sa.Float, nullable=False),
        sa.Column("fat_g", sa.Float, nullable=False),
        sa.Column("carb_g", sa.Float, nullable=False),
    )
    op.create_index("ix_user_goals_user_id", "user_goals", ["user_id"], unique=True)

    op.create_table(
        "products",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("canonical_key", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("category", sa.String(64)),
        sa.Column("kcal_per_100g", sa.Float, nullable=False),
        sa.Column("protein_per_100g", sa.Float, nullable=False),
        sa.Column("fat_per_100g", sa.Float, nullable=False),
        sa.Column("carb_per_100g", sa.Float, nullable=False),
        sa.Column("unit_weight_g", sa.Float),
        sa.Column("default_portion_g", sa.Float),
        sa.Column("portion_sizes", postgresql.JSONB),
        sa.Column("source", sa.String(32), nullable=False, server_default="llm"),
        sa.Column("is_estimate", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_products_canonical_key", "products", ["canonical_key"], unique=True)

    op.create_table(
        "meals",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("eaten_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_type", sa.String(16), nullable=False, server_default="text"),
        sa.Column("raw_input", sa.String(1024), nullable=False),
        sa.Column("source_confidence", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("total_kcal", sa.Float, nullable=False, server_default="0"),
        sa.Column("total_protein", sa.Float, nullable=False, server_default="0"),
        sa.Column("total_fat", sa.Float, nullable=False, server_default="0"),
        sa.Column("total_carb", sa.Float, nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_meals_user_id", "meals", ["user_id"])
    op.create_index("ix_meals_eaten_at", "meals", ["eaten_at"])
    op.create_index("ix_meals_user_day", "meals", ["user_id", "eaten_at"])

    op.create_table(
        "meal_items",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "meal_id",
            sa.Integer,
            sa.ForeignKey("meals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id", sa.Integer, sa.ForeignKey("products.id", ondelete="SET NULL")
        ),
        sa.Column("name_snapshot", sa.String(255), nullable=False),
        sa.Column("grams", sa.Float, nullable=False),
        sa.Column("kcal", sa.Float, nullable=False),
        sa.Column("protein", sa.Float, nullable=False),
        sa.Column("fat", sa.Float, nullable=False),
        sa.Column("carb", sa.Float, nullable=False),
    )
    op.create_index("ix_meal_items_meal_id", "meal_items", ["meal_id"])

    op.create_table(
        "weight_logs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("weight_kg", sa.Float, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_weight_logs_user_id", "weight_logs", ["user_id"])
    op.create_index("ix_weight_logs_measured_at", "weight_logs", ["measured_at"])


def downgrade() -> None:
    op.drop_table("weight_logs")
    op.drop_table("meal_items")
    op.drop_table("meals")
    op.drop_table("products")
    op.drop_table("user_goals")
    op.drop_table("users")
