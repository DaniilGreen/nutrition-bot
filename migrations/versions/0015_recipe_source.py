"""products.source = 'recipe' для рецептов, заведённых до появления своей метки (FR-39.5)

Рецепт (FR-39.1) первые сутки пиннился с source='manual' — и его было не отличить от продукта,
который человек просто поправил руками (FR-15). Из-за этого «Моё» в каталоге нечем собрать.

Критерий: ручной источник, создан с 14.10.2026 (день появления рецептов), без фото этикетки.
Правка FR-15 меняет source у УЖЕ существующего продукта и created_at не трогает, поэтому
исправленные руками продукты старше даты. На момент написания под критерий попадает ровно
один продукт — рецепт владельца «курица с мазиком» (сверено на проде перед выкаткой).

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE products SET source = 'recipe' "
            "WHERE source = 'manual' AND created_at >= '2026-10-14' AND photo_file_id IS NULL"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("UPDATE products SET source = 'manual' WHERE source = 'recipe'"))
