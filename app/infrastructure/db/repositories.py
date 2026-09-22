"""Реализация портов на SQLAlchemy. Маппинг ORM <-> доменные dataclass'ы."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.entities import Meal, MealItem, Product, User, WeightLog
from app.domain.body_scan import SEGMENT_FIELDS, BodyScan
from app.domain.goals import Goal
from app.domain.value_objects import Density, Macros
from app.infrastructure.db.models import (
    BodyScanORM,
    BugReportORM,
    GoalHistoryORM,
    LlmSpendORM,
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
        onboarded_at=o.onboarded_at,
        ui_prefs=dict(o.ui_prefs or {}),
        created_at=o.created_at,
        scan_nudged_at=o.scan_nudged_at,
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
        photo_file_id=o.photo_file_id,
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
                is_estimate=i.is_estimate,
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
        return Goal(g.daily_kcal, g.protein_g, g.fat_g, g.carb_g, g.target)

    async def set_goal(self, user_id: int, goal: Goal, source: str = "manual") -> None:
        # История — ради недели (§6.23): новая цель не переписывает прошлые дни.
        self._s.add(GoalHistoryORM(
            user_id=user_id, valid_from=datetime.now(UTC), daily_kcal=goal.daily_kcal,
            protein_g=goal.protein_g, source=source[:16],
        ))
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
                    target=goal.target,
                )
            )
        else:
            g.daily_kcal = goal.daily_kcal
            g.protein_g = goal.protein_g
            g.fat_g = goal.fat_g
            g.carb_g = goal.carb_g
            g.target = goal.target
        await self._s.flush()

    async def goal_history(self, user_id: int) -> list[tuple[datetime, float]]:
        res = await self._s.execute(
            select(GoalHistoryORM.valid_from, GoalHistoryORM.daily_kcal)
            .where(GoalHistoryORM.user_id == user_id)
            .order_by(GoalHistoryORM.valid_from)
        )
        return [(t, float(k)) for t, k in res.all()]

    async def set_timezone(self, user_id: int, tzname: str) -> None:
        orm = await self._s.get(UserORM, user_id)
        if orm is not None:
            orm.timezone = tzname
            await self._s.flush()

    async def set_onboarded(self, user_id: int) -> None:
        orm = await self._s.get(UserORM, user_id)
        if orm is not None and orm.onboarded_at is None:
            orm.onboarded_at = datetime.now(UTC)  # реальный datetime, не SQL-выражение
            await self._s.flush()

    async def set_prefs(self, user_id: int, prefs: dict) -> None:
        orm = await self._s.get(UserORM, user_id)
        if orm is None:
            return
        orm.ui_prefs = prefs
        await self._s.flush()

    async def mark_scan_nudged(self, user_id: int, at: datetime) -> None:
        orm = await self._s.get(UserORM, user_id)
        if orm is None:
            return
        orm.scan_nudged_at = at
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
            photo_file_id=product.photo_file_id,
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

    async def search(self, query: str, limit: int) -> list[Product]:
        # Ищем среди ВСЕХ продуктов, не только is_estimate: выверенный тоже правится, если в него
        # внесли опечатку. Экранируем %/_ — иначе ввод «100%» стал бы маской «что угодно».
        esc = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{esc}%"
        res = await self._s.execute(
            select(ProductORM)
            .where(
                ProductORM.display_name.ilike(pattern, escape="\\")
                | ProductORM.canonical_key.ilike(pattern, escape="\\")
            )
            .order_by(ProductORM.is_estimate.desc(), ProductORM.created_at.desc())
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

    async def rename(self, product_id: int, name: str) -> Product | None:
        orm = await self._s.get(ProductORM, product_id)
        if orm is None:
            return None
        orm.display_name = name
        await self._s.flush()
        return _product_to_domain(orm)

    @staticmethod
    def _like(query: str) -> str:
        """Экранируем %/_ — иначе ввод «100%» стал бы маской «что угодно»."""
        esc = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{esc}%"

    async def list_page(
        self, *, offset: int, limit: int, query: str | None = None
    ) -> tuple[list[Product], int]:
        stmt = select(ProductORM)
        count_stmt = select(func.count()).select_from(ProductORM)
        if query:
            pattern = self._like(query)
            cond = ProductORM.display_name.ilike(pattern, escape="\\") | (
                ProductORM.canonical_key.ilike(pattern, escape="\\")
            )
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)
        # Сортировка по имени, а не по id: каталог листают глазами, а не по времени пиннинга.
        stmt = stmt.order_by(ProductORM.display_name).offset(offset).limit(limit)
        rows = (await self._s.execute(stmt)).scalars().all()
        total = (await self._s.execute(count_stmt)).scalar_one()
        return [_product_to_domain(o) for o in rows], int(total)

    async def usage_counts(
        self, product_ids: list[int], *, user_id: int | None = None
    ) -> dict[int, int]:
        if not product_ids:
            return {}
        stmt = select(MealItemORM.product_id, func.count()).where(
            MealItemORM.product_id.in_(product_ids)
        )
        if user_id is not None:
            # Без джойна счётчик смешал бы обоих пользователей: «ел 9 раз»
            # рассказало бы, сколько раз это ел другой человек.
            stmt = stmt.join(MealORM, MealORM.id == MealItemORM.meal_id).where(
                MealORM.user_id == user_id
            )
        res = await self._s.execute(stmt.group_by(MealItemORM.product_id))
        return {int(pid): int(cnt) for pid, cnt in res.all() if pid is not None}

    async def merge(self, winner_id: int, loser_ids: list[int]) -> int:
        """Перепривязать историю на выжившего и удалить проигравших.

        В UPDATE входит ТОЛЬКО product_id. Снапшот (kcal/protein/fat/carb/grams/
        is_estimate) не трогается ни одной строкой — иначе поехала бы вся история
        (INV-3), а слияние каталога не имеет права двигать съеденное вчера.
        """
        losers = [i for i in loser_ids if i != winner_id]
        if not losers:
            return 0
        res = await self._s.execute(
            update(MealItemORM)
            .where(MealItemORM.product_id.in_(losers))
            .values(product_id=winner_id)
        )
        await self._s.execute(delete(ProductORM).where(ProductORM.id.in_(losers)))
        await self._s.flush()
        return int(res.rowcount or 0)


class SqlMealRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def contributions(
        self, user_id: int, *, days: int, limit: int, by: str = "kcal"
    ) -> list[tuple[int | None, str, float, int]]:
        since = datetime.now(UTC) - timedelta(days=days)
        # Один запрос на две панели: «куда уходят калории» и «откуда приходит
        # белок» — это одна и та же выборка, отличается только колонка.
        col = MealItemORM.protein if by == "protein" else MealItemORM.kcal
        res = await self._s.execute(
            select(
                MealItemORM.product_id,
                func.max(MealItemORM.name_snapshot).label("name"),
                func.sum(col).label("amount"),
                func.count().label("times"),
            )
            .join(MealORM, MealORM.id == MealItemORM.meal_id)
            .where(MealORM.user_id == user_id, MealORM.eaten_at >= since)
            .group_by(MealItemORM.product_id, func.coalesce(MealItemORM.product_id, -1))
            .order_by(func.sum(col).desc())
            .limit(limit)
        )
        return [
            (pid, name, float(kcal or 0), int(times)) for pid, name, kcal, times in res.all()
        ]

    async def items_of_product(
        self, user_id: int, product_id: int, *, limit: int
    ) -> list[tuple[datetime, float, float]]:
        res = await self._s.execute(
            select(MealORM.eaten_at, MealItemORM.grams, MealItemORM.kcal)
            .join(MealORM, MealORM.id == MealItemORM.meal_id)
            .where(MealORM.user_id == user_id, MealItemORM.product_id == product_id)
            .order_by(MealORM.eaten_at.desc())
            .limit(limit)
        )
        return [(at, float(g or 0), float(k or 0)) for at, g, k in res.all()]

    async def estimate_share(self, user_id: int, *, days: int) -> tuple[float, float]:
        since = datetime.now(UTC) - timedelta(days=days)
        res = await self._s.execute(
            select(
                func.coalesce(
                    func.sum(
                        case((MealItemORM.is_estimate.is_(True), MealItemORM.kcal), else_=0.0)
                    ),
                    0.0,
                ),
                func.coalesce(func.sum(MealItemORM.kcal), 0.0),
            )
            .join(MealORM, MealORM.id == MealItemORM.meal_id)
            .where(MealORM.user_id == user_id, MealORM.eaten_at >= since)
        )
        estimated, total = res.one()
        return float(estimated), float(total)

    async def frequent_items(
        self, user_id: int, *, days: int, limit: int
    ) -> list[tuple[int | None, str, float, int]]:
        since = datetime.now(UTC) - timedelta(days=days)
        res = await self._s.execute(
            select(
                MealItemORM.product_id,
                # Имя — самое свежее из группы: каталог могли переименовать,
                # и подсказка должна называться так, как называется сейчас.
                func.max(MealItemORM.name_snapshot).label("name"),
                # percentile_cont даёт медиану одним запросом: тащить граммовки
                # в питон ради сортировки — лишний трафик на ровном месте.
                func.percentile_cont(0.5)
                .within_group(MealItemORM.grams)
                .label("median_grams"),
                func.count().label("times"),
            )
            .join(MealORM, MealORM.id == MealItemORM.meal_id)
            .where(MealORM.user_id == user_id, MealORM.eaten_at >= since)
            # coalesce: записи без продукта (LLM не запиннил) группируются
            # по имени — другого ключа у них нет.
            .group_by(MealItemORM.product_id, func.coalesce(MealItemORM.product_id, -1))
            .order_by(func.count().desc())
            .limit(limit)
        )
        return [
            (pid, name, float(grams or 0), int(times)) for pid, name, grams, times in res.all()
        ]

    async def hourly_kcal(
        self, user_id: int, *, days: int, tz: str, day_start: int = 0
    ) -> list[tuple[str, int, float]]:
        """§6.20: калории по местным дням и часам — сырьё для нормы темпа дня.

        При `day_start` сутки и часы сдвинуты: час 0 — это начало суток человека."""
        since = datetime.now(UTC) - timedelta(days=days)
        local = func.timezone(tz, MealORM.eaten_at) - timedelta(hours=day_start)
        day = func.to_char(local, "YYYY-MM-DD").label("day")
        hour = func.extract("hour", local).label("hour")
        res = await self._s.execute(
            select(day, hour, func.sum(MealORM.total_kcal).label("kcal"))
            .where(MealORM.user_id == user_id, MealORM.eaten_at >= since)
            .group_by(day, hour)
            .order_by(day, hour)
        )
        return [(str(d), int(h), float(k or 0)) for d, h, k in res.all()]

    async def evening_items(
        self, user_id: int, *, days: int, limit: int, tz: str, since_hour: int
    ) -> list[tuple[int | None, str, float, int]]:
        """§6.20: типичная еда после указанного часа — чипы карточки добора."""
        since = datetime.now(UTC) - timedelta(days=days)
        local_hour = func.extract("hour", func.timezone(tz, MealORM.eaten_at))
        res = await self._s.execute(
            select(
                MealItemORM.product_id,
                func.max(MealItemORM.name_snapshot).label("name"),
                func.percentile_cont(0.5)
                .within_group(MealItemORM.grams)
                .label("median_grams"),
                func.count().label("times"),
            )
            .join(MealORM, MealORM.id == MealItemORM.meal_id)
            .where(
                MealORM.user_id == user_id,
                MealORM.eaten_at >= since,
                local_hour >= since_hour,
                # Без продукта чип пишется через текст и модель — в быстрой
                # подсказке это уже не «одно касание», поэтому не предлагаем.
                MealItemORM.product_id.is_not(None),
                MealItemORM.grams > 0,
            )
            .group_by(MealItemORM.product_id)
            .order_by(func.count().desc())
            .limit(limit)
        )
        return [
            (pid, name, float(grams or 0), int(times)) for pid, name, grams, times in res.all()
        ]

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
                is_estimate=i.is_estimate,
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
        # Последний по времени ЗАПИСИ, а не по времени еды. С появлением бэкдейта (FR-33) это
        # разные приёмы: записал «вчера курица» → «отменить» обязано убрать её, а не сегодняшний
        # обед с более поздним eaten_at. id в тай-брейке — на случай одинаковых created_at.
        res = await self._s.execute(
            select(MealORM)
            .where(MealORM.user_id == user_id)
            .order_by(MealORM.created_at.desc(), MealORM.id.desc())
            .limit(1)
        )
        return await self._delete_orm(res.scalar_one_or_none())

    async def get(self, user_id: int, meal_id: int) -> Meal | None:
        res = await self._s.execute(
            select(MealORM).where(MealORM.id == meal_id, MealORM.user_id == user_id)
        )
        orm = res.scalar_one_or_none()
        return _meal_to_domain(orm) if orm is not None else None

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

    async def update_item(
        self, user_id: int, meal_id: int, item_id: int, *, grams: float, macros: Macros
    ) -> Meal | None:
        res = await self._s.execute(
            select(MealORM).where(MealORM.id == meal_id, MealORM.user_id == user_id)
        )
        orm = res.scalar_one_or_none()
        if orm is None:
            return None
        item = next((i for i in orm.items if i.id == item_id), None)
        if item is None:
            return _meal_to_domain(orm)
        item.grams = grams
        item.kcal, item.protein, item.fat, item.carb = (
            macros.kcal, macros.protein, macros.fat, macros.carb,
        )
        orm.total_kcal = round(sum(i.kcal for i in orm.items), 1)
        orm.total_protein = round(sum(i.protein for i in orm.items), 1)
        orm.total_fat = round(sum(i.fat for i in orm.items), 1)
        orm.total_carb = round(sum(i.carb for i in orm.items), 1)
        await self._s.flush()
        return _meal_to_domain(orm)

    async def _delete_orm(self, orm: MealORM | None) -> Meal | None:
        if orm is None:
            return None
        meal = _meal_to_domain(orm)
        await self._s.delete(orm)
        await self._s.flush()
        return meal


class SqlBugRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(
        self, *, user_id: int, note: str, context: list[dict[str, object]] | None
    ) -> None:
        self._s.add(BugReportORM(user_id=user_id, note=note, context=context))
        await self._s.flush()


_SCAN_COLUMNS = (
    "measured_at", "weight_kg", "body_fat_kg", "fat_free_kg", "body_fat_pct",
    "skeletal_muscle_kg", "water_l", "protein_kg", "minerals_kg", "visceral_level",
    "bmr_kcal", "height_cm", "age", "sex", "device", "smi", "inbody_score",
)


def _scan_to_domain(o: BodyScanORM) -> BodyScan:
    """Сегменты лежат одним JSONB, а в домене они плоские (§6.24 «Данные»).

    Фильтр по известным именам: база может оказаться старше или новее кода.
    """
    segments = {k: v for k, v in (o.segments or {}).items() if k in SEGMENT_FIELDS}
    return BodyScan(
        id=o.id, user_id=o.user_id,
        **{c: getattr(o, c) for c in _SCAN_COLUMNS}, **segments,
    )


class SqlBodyScanRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_at(self, user_id: int, measured_at: datetime) -> BodyScan | None:
        res = await self._s.execute(
            select(BodyScanORM).where(
                BodyScanORM.user_id == user_id, BodyScanORM.measured_at == measured_at
            )
        )
        orm = res.scalar_one_or_none()
        return _scan_to_domain(orm) if orm is not None else None

    async def upsert(
        self, scan: BodyScan, corrected: list[str] | None = None
    ) -> tuple[BodyScan, bool]:
        assert scan.user_id is not None
        existed = await self.get_at(scan.user_id, scan.measured_at) is not None
        # ON CONFLICT, а не «прочитал — решил»: двойной тап по «Сохранить» в боте
        # иначе давал две вставки и IntegrityError вместо обновления (MA-INV-51).
        values = {c: getattr(scan, c) for c in _SCAN_COLUMNS}
        # Блок сегментов заменяется целиком: повторный бланк не сливает ключи.
        segments = {f: getattr(scan, f) for f in SEGMENT_FIELDS if getattr(scan, f) is not None}
        values.update(user_id=scan.user_id, corrected_fields=sorted(corrected or []),
                      segments=segments or None)
        stmt = pg_insert(BodyScanORM).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_body_scans_user_time",
            set_={k: stmt.excluded[k] for k in values if k not in ("user_id", "measured_at")},
        ).returning(BodyScanORM.id)
        scan_id = (await self._s.execute(stmt)).scalar_one()
        orm = await self._s.get(BodyScanORM, scan_id, populate_existing=True)
        assert orm is not None
        return _scan_to_domain(orm), existed

    async def delete(self, user_id: int, scan_id: int) -> bool:
        res = await self._s.execute(
            delete(BodyScanORM).where(
                BodyScanORM.id == scan_id, BodyScanORM.user_id == user_id
            )
        )
        await self._s.flush()
        return bool(res.rowcount)

    async def list_for(self, user_id: int) -> list[BodyScan]:
        res = await self._s.execute(
            select(BodyScanORM)
            .where(BodyScanORM.user_id == user_id)
            .order_by(BodyScanORM.measured_at)
        )
        return [_scan_to_domain(o) for o in res.scalars().all()]


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

    async def delete(self, user_id: int, log_id: int) -> WeightLog | None:
        res = await self._s.execute(
            select(WeightLogORM).where(
                WeightLogORM.id == log_id, WeightLogORM.user_id == user_id
            )
        )
        orm = res.scalar_one_or_none()
        if orm is None:
            return None
        gone = WeightLog(
            id=orm.id, user_id=orm.user_id, measured_at=orm.measured_at, weight_kg=orm.weight_kg
        )
        await self._s.delete(orm)
        await self._s.flush()
        return gone

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


class SqlSpendRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(
        self,
        *,
        user_id: int | None,
        model: str,
        purpose: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        self._s.add(
            LlmSpendORM(
                user_id=user_id,
                model=model,
                purpose=purpose,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
            )
        )
        await self._s.flush()

    async def spent_this_month(self, *, purpose: str | None = None) -> float:
        now = datetime.now(UTC)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        stmt = select(func.coalesce(func.sum(LlmSpendORM.cost_usd), 0.0)).where(
            LlmSpendORM.created_at >= start
        )
        if purpose:
            stmt = stmt.where(LlmSpendORM.purpose == purpose)
        return float((await self._s.execute(stmt)).scalar_one())
