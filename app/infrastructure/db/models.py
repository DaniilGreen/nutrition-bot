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
    UniqueConstraint,
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
    # Когда последний раз напомнили о замере состава (§6.24). В памяти процесса
    # отметке не место: деплой перезапускает бота, и напоминание задублируется.
    scan_nudged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

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
    # FR-39.7: вес готового у рецепта; None — сумма граммов состава.
    recipe_yield_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RecipeItemORM(Base):
    """FR-39.7: строка состава рецепта. Ссылка на ингредиент + снимок его плотности — как у
    meal_items: удалили ингредиент — пересчёт берёт снимок, а не падает."""

    __tablename__ = "recipe_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    position: Mapped[int] = mapped_column(Integer)
    name_snapshot: Mapped[str] = mapped_column(String(255))
    grams: Mapped[float] = mapped_column(Float)
    kcal_per_100g: Mapped[float] = mapped_column(Float)
    protein_per_100g: Mapped[float] = mapped_column(Float)
    fat_per_100g: Mapped[float] = mapped_column(Float)
    carb_per_100g: Mapped[float] = mapped_column(Float)
    is_estimate: Mapped[bool] = mapped_column(Boolean, default=True)
    alcohol: Mapped[bool] = mapped_column(Boolean, default=False)


class MealORM(Base):
    __tablename__ = "meals"
    __table_args__ = (Index("ix_meals_user_day", "user_id", "eaten_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    eaten_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    input_type: Mapped[str] = mapped_column(String(16), default="text")
    raw_input: Mapped[str] = mapped_column(String(1024))
    source_confidence: Mapped[float] = mapped_column(Float, default=1.0)
    # FR-39.2: имя блюда своей сборки. Переносится на повтор — шаблон жив, пока блюдо едят.
    template_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
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


class BodyScanORM(Base):
    """Замер состава тела с бланка InBody (FR-36). Один бланк — одна строка."""

    __tablename__ = "body_scans"
    __table_args__ = (UniqueConstraint("user_id", "measured_at", name="uq_body_scans_user_time"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    weight_kg: Mapped[float] = mapped_column(Float)
    body_fat_kg: Mapped[float] = mapped_column(Float)
    fat_free_kg: Mapped[float] = mapped_column(Float)
    body_fat_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    skeletal_muscle_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    water_l: Mapped[float | None] = mapped_column(Float, nullable=True)
    protein_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    minerals_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    visceral_level: Mapped[float | None] = mapped_column(Float, nullable=True)
    bmr_kcal: Mapped[float | None] = mapped_column(Float, nullable=True)
    height_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sex: Mapped[str | None] = mapped_column(String(8), nullable=True)
    device: Mapped[str | None] = mapped_column(String(64), nullable=True)
    smi: Mapped[float | None] = mapped_column(Float, nullable=True)
    inbody_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Десять чисел сегментного анализа читаются только целиком и только на экран:
    # поштучно по ним не фильтруют и не агрегируют — граница та же, что у
    # `ui_prefs` в миграции 0010. NULL — «бланк читали без сегментов».
    segments: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    corrected_fields: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class GoalHistoryORM(Base):
    """Цель, действовавшая с момента `valid_from` (§6.23)."""

    __tablename__ = "goal_history"
    __table_args__ = (Index("ix_goal_history_user_time", "user_id", "valid_from"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    daily_kcal: Mapped[float] = mapped_column(Float)
    protein_g: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16), default="manual")


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


class AppSettingORM(Base):
    """Настройки всей инсталляции, а не человека (§6.5). Первая — месячный потолок трат на
    модель: платит владелец за всех, поэтому в строке `users` ему не место."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | float | int | str] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
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
