"""recipe_items + products.recipe_yield_g — состав рецепта (FR-39.7)

Рецепт хранил только итоговую плотность; владелец попросил править именно продукты в рецепте.
Таблица по образцу meal_items: ссылка на ингредиент с ON DELETE SET NULL плюс снимок его
плотности — чтобы слияние дублей перепривязывало ссылку тем же UPDATE, что и историю, а
удалённый ингредиент не ронял пересчёт (D-38, пересмотр 15.10.2026).

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("recipe_yield_g", sa.Float(), nullable=True))
    op.create_table(
        "recipe_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "recipe_id", sa.Integer(),
            sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True,
        ),
        sa.Column(
            "product_id", sa.Integer(),
            sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name_snapshot", sa.String(255), nullable=False),
        sa.Column("grams", sa.Float(), nullable=False),
        sa.Column("kcal_per_100g", sa.Float(), nullable=False),
        sa.Column("protein_per_100g", sa.Float(), nullable=False),
        sa.Column("fat_per_100g", sa.Float(), nullable=False),
        sa.Column("carb_per_100g", sa.Float(), nullable=False),
        sa.Column("is_estimate", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("alcohol", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_table("recipe_items")
    op.drop_column("products", "recipe_yield_g")
