"""учёт расходов на LLM (OQ-M3): лимит $5/мес неисполним без счётчика

Пишем по вызову, а не агрегатом: агрегат не даёт разобраться, что именно съело
бюджет, а строк тут единицы в день на двух пользователей.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_spend",
        sa.Column("id", sa.Integer(), primary_key=True),
        # Расход может быть и не привязан к пользователю (фоновые задачи), поэтому
        # nullable. SET NULL, а не CASCADE: удалять историю трат вместе с юзером
        # неправильно — деньги потрачены независимо от того, кто остался в базе.
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False, server_default="nlu"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # Индекс под единственный горячий запрос: «сколько потрачено за месяц».
    op.create_index("ix_llm_spend_created", "llm_spend", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_llm_spend_created", table_name="llm_spend")
    op.drop_table("llm_spend")
