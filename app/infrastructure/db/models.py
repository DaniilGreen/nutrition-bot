"""ORM-модели (SQLAlchemy 2.0). Отделены от доменных сущностей; маппинг — в репозиториях."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base


class UserORM(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    locale: Mapped[str] = mapped_column(String(8), default="ru")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    role: Mapped[str] = mapped_column(String(16), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    onboarded_at: Mapped[datetime | None] = mapped_column(  # прошёл ли первый визард (FR-24)
        DateTime(timezone=True), nullable=True
    )
    # Настройки оформления (§6.5). Одной колонкой, а не восемью: они не влияют
    # на данные, только на то, как эти данные нарисованы.
    ui_prefs: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), default=dict)

    goal: Mapped[UserGoalORM | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )


class UserGoalORM(Base):
    __tablename__ = "user_goals"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    daily_kcal: Mapped[float] = mapped_column(Float)
    protein_g: Mapped[float] = mapped_column(Float)
    fat_g: Mapped[float] = mapped_column(Float)
    carb_g: Mapped[float] = mapped_column(Float)
    # cut | maintain | bulk — от цели зависит раскладка макросов (domain/goals.rebalance).
    # Единственное, что переживает визард: пол/рост/возраст он в БД не пишет.
    target: Mapped[str] = mapped_column(String(16), server_default="maintain")

    user: Mapped[UserORM] = relationship(back_populates="goal")


class ProductORM(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kcal_per_100g: Mapped[float] = mapped_column(Float)
    protein_per_100g: Mapped[float] = mapped_column(Float)
    fat_per_100g: Mapped[float] = mapped_column(Float)
    carb_per_100g: Mapped[float] = mapped_column(Float)
    unit_weight_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    default_portion_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    portion_sizes: Mapped[dict[str, float] | None] = mapped_column(JSONB, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="llm")
    is_estimate: Mapped[bool] = mapped_column(Boolean, default=True)
    photo_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MealORM(Base):
    __tablename__ = "meals"
    __table_args__ = (Index("ix_meals_user_day", "user_id", "eaten_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    eaten_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    input_type: Mapped[str] = mapped_column(String(16), default="text")
    raw_input: Mapped[str] = mapped_column(String(1024))
    source_confidence: Mapped[float] = mapped_column(Float, default=1.0)
    total_kcal: Mapped[float] = mapped_column(Float, default=0.0)
    total_protein: Mapped[float] = mapped_column(Float, default=0.0)
    total_fat: Mapped[float] = mapped_column(Float, default=0.0)
    total_carb: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    items: Mapped[list[MealItemORM]] = relationship(
        back_populates="meal", cascade="all, delete-orphan", lazy="selectin"
    )


class MealItemORM(Base):
    __tablename__ = "meal_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    meal_id: Mapped[int] = mapped_column(ForeignKey("meals.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    name_snapshot: Mapped[str] = mapped_column(String(255))
    grams: Mapped[float] = mapped_column(Float)
    # Снапшот КБЖУ на момент логирования — история не «плывёт» при правках каталога.
    kcal: Mapped[float] = mapped_column(Float)
    protein: Mapped[float] = mapped_column(Float)
    fat: Mapped[float] = mapped_column(Float)
    carb: Mapped[float] = mapped_column(Float)
    is_estimate: Mapped[bool] = mapped_column(  # AI-оценка (снапшот) → метка «≈» (INV-13)
        Boolean, default=False, server_default="false"
    )

    meal: Mapped[MealORM] = relationship(back_populates="items")


class WeightLogORM(Base):
    __tablename__ = "weight_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    weight_kg: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BugReportORM(Base):
    """Репорт о баге (FR-26, D-17): заметка юзера + контекст (последние приёмы)."""

    __tablename__ = "bug_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    note: Mapped[str] = mapped_column(String(2000))
    context: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB, nullable=True)  # приёмы
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(  # NULL = новый/открытый репорт
        DateTime(timezone=True), nullable=True
    )


class LlmSpendORM(Base):
    """Один вызов LLM — одна строка. Агрегат не дал бы разобраться, что съело бюджет."""

    __tablename__ = "llm_spend"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    model: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(32), server_default="nlu")
    input_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cost_usd: Mapped[float] = mapped_column(Float, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
