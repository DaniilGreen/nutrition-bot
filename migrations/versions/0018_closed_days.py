"""add closed_days — день закрыт кнопкой «✅ всё записал» (D-57)

Сигнал полноты дня из вечернего итога (FR-49.6): серия записанных дней (D-44) засчитывает день
с хотя бы одним приёмом или закрытый этой кнопкой. Дата — местная дата человека (с учётом начала
суток), как её видит сам итог. Повтор кнопки ничего не пишет (первичный ключ user_id + day).

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "closed_days",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )


def downgrade() -> None:
    op.drop_table("closed_days")
