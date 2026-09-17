"""замеры состава тела InBody и история целей (FR-35, FR-36)

body_scans — отдельная таблица, а не колонки в weight_logs: замер редкий (раз в
месяц) и широкий, а вес — частый и узкий. Уникальность по (user_id, measured_at):
два фото одного бланка — один замер (MA-INV-51).

goal_history — цель, действовавшая в конкретный день. Без неё «Применить цели»
по замеру пересчитывало бы прошлые недели задним числом (MINIAPP-SPEC §6.23).
Текущие цели переносятся с valid_from в далёком прошлом: до истории цель одна.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "body_scans",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("weight_kg", sa.Float, nullable=False),
        sa.Column("body_fat_kg", sa.Float, nullable=False),
        sa.Column("fat_free_kg", sa.Float, nullable=False),
        sa.Column("body_fat_pct", sa.Float),
        sa.Column("skeletal_muscle_kg", sa.Float),
        sa.Column("water_l", sa.Float),
        sa.Column("protein_kg", sa.Float),
        sa.Column("minerals_kg", sa.Float),
        sa.Column("visceral_level", sa.Float),
        sa.Column("bmr_kcal", sa.Float),
        sa.Column("height_cm", sa.Float),
        sa.Column("age", sa.Integer),
        sa.Column("sex", sa.String(8)),
        sa.Column("device", sa.String(64)),
        sa.Column("corrected_fields", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "measured_at", name="uq_body_scans_user_time"),
    )
    op.create_table(
        "goal_history",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("daily_kcal", sa.Float, nullable=False),
        sa.Column("protein_g", sa.Float, nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
    )
    op.create_index("ix_goal_history_user_time", "goal_history", ["user_id", "valid_from"])
    op.execute(
        "INSERT INTO goal_history (user_id, valid_from, daily_kcal, protein_g, source) "
        "SELECT user_id, TIMESTAMPTZ '2000-01-01 00:00+00', daily_kcal, protein_g, 'initial' "
        "FROM user_goals"
    )


def downgrade() -> None:
    op.drop_index("ix_goal_history_user_time", table_name="goal_history")
    op.drop_table("goal_history")
    op.drop_table("body_scans")
