"""мышечный индекс, балл прибора и сегменты бланка; отметка напоминания (FR-37)

Сегменты — одна колонка JSONB, а не десять колонок. Граница та же, что в 0010
(`ui_prefs`): одиночное подписанное число, которое может попасть на график или в
проверку, — колонка (`smi`, `inbody_score`); блок однородных чисел, который
читается только целиком и только на экран, — JSONB. По сегментам поштучно не
фильтруют и не агрегируют, а следующая модель прибора принесёт ещё по пять чисел
на сегмент — это были бы всё новые миграции.

NULL, а не `{}` по умолчанию: «бланк читали без сегментов» и «читали и не нашли» —
разные вещи, и разница видна в метрике качества чтения. Существующий замер не
досчитываем: фото не хранится, а вывести индекс из мышц нельзя (MA-INV-56).

`users.scan_nudged_at` — когда последний раз напомнили о замере. В памяти
процесса отметке не место: деплой перезапускает бота (MA-INV-63).

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("body_scans", sa.Column("smi", sa.Float(), nullable=True))
    op.add_column("body_scans", sa.Column("inbody_score", sa.Float(), nullable=True))
    op.add_column(
        "body_scans",
        sa.Column("segments", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "users", sa.Column("scan_nudged_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("users", "scan_nudged_at")
    op.drop_column("body_scans", "segments")
    op.drop_column("body_scans", "inbody_score")
    op.drop_column("body_scans", "smi")
