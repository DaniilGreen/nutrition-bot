"""Реализация портов на SQLAlchemy. Маппинг ORM <-> доменные dataclass'ы."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.entities import Meal, MealItem, Product, User, WeightLog
from app.domain.goals import Goal
from app.domain.value_objects import Density, Macros
from app.infrastructure.db.models import (
    MealItemORM,
    MealORM,
    ProductORM,
    UserGoalORM,
    UserORM,
    WeightLogORM,
)


def _user_to_domain(o: UserORM) -> User:
    return User(
        id=o.id,
        telegram_id=o.telegram_id,
        name=o.name,
        locale=o.locale,
        timezone=o.timezone,
        role=o.role,
        is_active=o.is_active,
    )


def _product_to_domain(o: ProductORM) -> Product:
    return Product(
        id=o.id,
        canonical_key=o.canonical_key,
        display_name=o.display_name,
        density=Density(o.kcal_per_100g, o.protein_per_100g, o.fat_per_100g, o.carb_per_100g),
        category=o.category,
        unit_weight_g=o.unit_weight_g,
        default_portion_g=o.default_portion_g,
        portion_sizes=o.portion_sizes,
        source=o.source,
        is_estimate=o.is_estimate,
    )


def _meal_to_domain(o: MealORM) -> Meal:
    return Meal(
        id=o.id,
        user_id=o.user_id,
        eaten_at=o.eaten_at,
        input_type=o.input_type,
        raw_input=o.raw_input,
        source_confidence=o.source_confidence,
        totals=Macros(o.total_kcal, o.total_protein, o.total_fat, o.total_carb),
        items=[
            MealItem(
                id=i.id,
                product_id=i.product_id,
                name_snapshot=i.name_snapshot,
                grams=i.grams,
                macros=Macros(i.kcal, i.protein, i.fat, i.carb),
            )
            for i in o.items
        ],
        created_at=o.created_at,
    )


class SqlUserRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_or_create(
        self, *, telegram_id: int, name: str | None, locale: str, timezone: str
    ) -> User:
        res = await self._s.execute(select(UserORM).where(UserORM.telegram_id == telegram_id))
        orm = res.scalar_one_or_none()
        if orm is None:
            orm = UserORM(telegram_id=telegram_id, name=name, locale=locale, timezone=timezone)
            self._s.add(orm)
            await self._s.flush()
        return _user_to_domain(orm)

    async def get_goal(self, user_id: int) -> Goal | None:
        res = await self._s.execute(select(UserGoalORM).where(UserGoalORM.user_id == user_id))
        g = res.scalar_one_or_none()
        if g is None:
            return None
        return Goal(g.daily_kcal, g.protein_g, g.fat_g, g.carb_g)

    async def set_goal(self, user_id: int, goal: Goal) -> None:
        res = await self._s.execute(select(UserGoalORM).where(UserGoalORM.user_id == user_id))
        g = res.scalar_one_or_none()
        if g is None:
            self._s.add(
                UserGoalORM(
                    user_id=user_id,
                    daily_kcal=goal.daily_kcal,
                    protein_g=goal.protein_g,
                    fat_g=goal.fat_g,
                    carb_g=goal.carb_g,
                )
            )
        else:
            g.daily_kcal = goal.daily_kcal
            g.protein_g = goal.protein_g
            g.fat_g = goal.fat_g
            g.carb_g = goal.carb_g
        await self._s.flush()

    async def list_active(self) -> list[User]:
        res = await self._s.execute(select(UserORM).where(UserORM.is_active.is_(True)))
        return [_user_to_domain(o) for o in res.scalars().all()]


class SqlProductRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_by_key(self, canonical_key: str) -> Product | None:
        res = await self._s.execute(
            select(ProductORM).where(ProductORM.canonical_key == canonical_key)
        )
        orm = res.scalar_one_or_none()
        return _product_to_domain(orm) if orm is not None else None

    async def get_by_id(self, product_id: int) -> Product | None:
        orm = await self._s.get(ProductORM, product_id)
        return _product_to_domain(orm) if orm is not None else None

    async def create(self, product: Product) -> Product:
        orm = ProductORM(
            canonical_key=product.canonical_key,
            display_name=product.display_name,
            category=product.category,
            kcal_per_100g=product.density.kcal_per_100g,
            protein_per_100g=product.density.protein_per_100g,
            fat_per_100g=product.density.fat_per_100g,
            carb_per_100g=product.density.carb_per_100g,
            unit_weight_g=product.unit_weight_g,
            default_portion_g=product.default_portion_g,
            portion_sizes=product.portion_sizes,
            source=product.source,
            is_estimate=product.is_estimate,
        )
        self._s.add(orm)
        await self._s.flush()
        return _product_to_domain(orm)

    async def all_keys(self) -> list[str]:
        res = await self._s.execute(select(ProductORM.canonical_key))
        return list(res.scalars().all())

    async def list_all(self) -> list[Product]:
        # ponytail: полный скан каталога; добавить pg_trgm/фильтр, если вырастет за тысячи
        res = await self._s.execute(select(ProductORM))
        return [_product_to_domain(o) for o in res.scalars().all()]

    async def list_by_category(self, category: str) -> list[Product]:
        res = await self._s.execute(
            select(ProductORM).where(ProductORM.category == category)
        )
        return [_product_to_domain(o) for o in res.scalars().all()]

    async def list_estimated(self, limit: int) -> list[Product]:
        res = await self._s.execute(
            select(ProductORM)
            .where(ProductORM.is_estimate.is_(True))
            .order_by(ProductORM.created_at.desc())
            .limit(limit)
        )
        return [_product_to_domain(o) for o in res.scalars().all()]

    async def update_density(self, product_id: int, density: Density) -> Product | None:
        orm = await self._s.get(ProductORM, product_id)
        if orm is None:
            return None
        orm.kcal_per_100g = density.kcal_per_100g
        orm.protein_per_100g = density.protein_per_100g
        orm.fat_per_100g = density.fat_per_100g
        orm.carb_per_100g = density.carb_per_100g
        orm.is_estimate = False  # вручную выверено — больше не оценка
        orm.source = "manual"
        await self._s.flush()
        return _product_to_domain(orm)


class SqlMealRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, meal: Meal) -> Meal:
        orm = MealORM(
            user_id=meal.user_id,
            eaten_at=meal.eaten_at,
            input_type=meal.input_type,
            raw_input=meal.raw_input,
            source_confidence=meal.source_confidence,
            total_kcal=meal.totals.kcal,
            total_protein=meal.totals.protein,
            total_fat=meal.totals.fat,
            total_carb=meal.totals.carb,
        )
        orm.items = [
            MealItemORM(
                product_id=i.product_id,
                name_snapshot=i.name_snapshot,
                grams=i.grams,
                kcal=i.macros.kcal,
                protein=i.macros.protein,
                fat=i.macros.fat,
                carb=i.macros.carb,
            )
            for i in meal.items
        ]
        self._s.add(orm)
        await self._s.flush()
        return _meal_to_domain(orm)

    async def list_between(self, user_id: int, start: datetime, end: datetime) -> list[Meal]:
        res = await self._s.execute(
            select(MealORM)
            .where(
                MealORM.user_id == user_id,
                MealORM.eaten_at >= start,
                MealORM.eaten_at < end,
            )
            .order_by(MealORM.eaten_at)
        )
        return [_meal_to_domain(o) for o in res.scalars().all()]

    async def list_recent(self, user_id: int, limit: int) -> list[Meal]:
        res = await self._s.execute(
            select(MealORM)
            .where(MealORM.user_id == user_id)
            .order_by(MealORM.eaten_at.desc())
            .limit(limit)
        )
        return [_meal_to_domain(o) for o in res.scalars().all()]

    async def delete_last(self, user_id: int) -> Meal | None:
        res = await self._s.execute(
            select(MealORM)
            .where(MealORM.user_id == user_id)
            .order_by(MealORM.eaten_at.desc())
            .limit(1)
        )
        return await self._delete_orm(res.scalar_one_or_none())

    async def delete(self, user_id: int, meal_id: int) -> Meal | None:
        res = await self._s.execute(
            select(MealORM).where(MealORM.id == meal_id, MealORM.user_id == user_id)
        )
        return await self._delete_orm(res.scalar_one_or_none())

    async def remove_item(self, user_id: int, meal_id: int, item_id: int) -> Meal | None:
        res = await self._s.execute(
            select(MealORM).where(MealORM.id == meal_id, MealORM.user_id == user_id)
        )
        orm = res.scalar_one_or_none()
        if orm is None:
            return None
        item = next((i for i in orm.items if i.id == item_id), None)
        if item is None:
            return _meal_to_domain(orm)

        orm.items.remove(item)  # delete-orphan удалит строку при flush
        if not orm.items:
            await self._s.delete(orm)
            await self._s.flush()
            return None

        orm.total_kcal = round(sum(i.kcal for i in orm.items), 1)
        orm.total_protein = round(sum(i.protein for i in orm.items), 1)
        orm.total_fat = round(sum(i.fat for i in orm.items), 1)
        orm.total_carb = round(sum(i.carb for i in orm.items), 1)
        await self._s.flush()
        return _meal_to_domain(orm)

    async def most_frequent_item(
        self, user_id: int, product_ids: Sequence[int], since: datetime
    ) -> int | None:
        """Самый частый product_id из набора в записях юзера с since (tie-break — свежесть)."""
        if not product_ids:
            return None
        res = await self._s.execute(
            select(MealItemORM.product_id)
            .join(MealORM, MealItemORM.meal_id == MealORM.id)
            .where(
                MealORM.user_id == user_id,
                MealItemORM.product_id.in_(list(product_ids)),
                MealORM.eaten_at >= since,
            )
            .group_by(MealItemORM.product_id)
            .order_by(func.count().desc(), func.max(MealORM.eaten_at).desc())
            .limit(1)
        )
        return res.scalar_one_or_none()

    async def _delete_orm(self, orm: MealORM | None) -> Meal | None:
        if orm is None:
            return None
        meal = _meal_to_domain(orm)
        await self._s.delete(orm)
        await self._s.flush()
        return meal


class SqlWeightRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, log: WeightLog) -> WeightLog:
        orm = WeightLogORM(
            user_id=log.user_id, measured_at=log.measured_at, weight_kg=log.weight_kg
        )
        self._s.add(orm)
        await self._s.flush()
        return WeightLog(
            id=orm.id, user_id=orm.user_id, measured_at=orm.measured_at, weight_kg=orm.weight_kg
        )

    async def history(self, user_id: int, limit: int) -> list[WeightLog]:
        res = await self._s.execute(
            select(WeightLogORM)
            .where(WeightLogORM.user_id == user_id)
            .order_by(WeightLogORM.measured_at.desc())
            .limit(limit)
        )
        return [
            WeightLog(id=o.id, user_id=o.user_id, measured_at=o.measured_at, weight_kg=o.weight_kg)
            for o in res.scalars().all()
        ]

    async def latest(self, user_id: int, before: datetime | None = None) -> WeightLog | None:
        stmt = select(WeightLogORM).where(WeightLogORM.user_id == user_id)
        if before is not None:
            stmt = stmt.where(WeightLogORM.measured_at < before)
        stmt = stmt.order_by(WeightLogORM.measured_at.desc()).limit(1)
        res = await self._s.execute(stmt)
        orm = res.scalar_one_or_none()
        if orm is None:
            return None
        return WeightLog(
            id=orm.id, user_id=orm.user_id, measured_at=orm.measured_at, weight_kg=orm.weight_kg
        )
