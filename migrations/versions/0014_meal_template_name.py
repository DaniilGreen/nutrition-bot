"""add meals.template_name — имя блюда своей сборки (FR-39.2)

Сборка (свёрток, бургер) хранится не продуктом с плотностью, а ИМЕНЕМ приёма: дальше бот
находит свой последний приём с этим именем и повторяет его позиции. Отдельная таблица рецептов
не нужна — повтор, удаление позиции и правка граммов уже работают (D-38).

Индекс по (user_id, template_name) — по нему идёт поиск на каждое входящее сообщение, ДО
разбора в LLM: знакомое блюдо не должно стоить ни денег, ни задержки.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("meals", sa.Column("template_name", sa.String(64), nullable=True))
    op.create_index(
        "ix_meals_template", "meals", ["user_id", "template_name"],
        postgresql_where=sa.text("template_name IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_meals_template", table_name="meals")
    op.drop_column("meals", "template_name")
