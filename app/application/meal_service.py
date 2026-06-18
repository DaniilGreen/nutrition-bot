"""Use-case: запись/чтение/удаление приёмов пищи.

Сердце модели «LLM наполняет, БД фиксирует»:
- известный продукт → плотность из БД (LLM-числа игнорируются) → детерминизм;
- новый продукт → создаём из оценки LLM и фиксируем навсегда.
Финальные КБЖУ считает domain.nutrition, не LLM.
"""
from __future__ import annotations

from app.application.dto import ParsedItem, ParsedMessage
from app.application.entities import Meal, MealItem, Product, User
from app.application.ports import MealRepo, ProductRepo
from app.clock import Clock, utcnow
from app.domain.nutrition import macros_for_grams
from app.domain.portions import resolve_grams
from app.domain.value_objects import Density, sum_macros


class MealService:
    def __init__(self, products: ProductRepo, meals: MealRepo, clock: Clock = utcnow) -> None:
        self._products = products
        self._meals = meals
        self._clock = clock

    async def log(
        self, user: User, parsed: ParsedMessage, *, raw_input: str, input_type: str
    ) -> Meal:
        user_id = _require_id(user)

        items: list[MealItem] = []
        for parsed_item in parsed.items:
            product = await self._resolve_product(parsed_item)
            resolution = resolve_grams(
                unit=parsed_item.unit.value,
                quantity=parsed_item.quantity,
                grams_estimate=parsed_item.grams,
                portion_hint=parsed_item.portion_hint,
                parsed_unit_weight_g=parsed_item.unit_weight_g,
                product_unit_weight_g=product.unit_weight_g,
                product_default_portion_g=product.default_portion_g,
                product_portion_sizes=product.portion_sizes,
            )
            macros = macros_for_grams(product.density, resolution.grams)
            items.append(
                MealItem(
                    name_snapshot=parsed_item.name,
                    grams=round(resolution.grams, 1),
                    macros=macros,
                    product_id=product.id,
                )
            )

        meal = Meal(
            user_id=user_id,
            eaten_at=self._clock(),
            input_type=input_type,
            raw_input=raw_input,
            items=items,
            source_confidence=parsed.confidence,
            totals=sum_macros(item.macros for item in items),
        )
        return await self._meals.add(meal)

    async def recent(self, user: User, limit: int = 10) -> list[Meal]:
        return await self._meals.list_recent(_require_id(user), limit)

    async def delete_last(self, user: User) -> Meal | None:
        return await self._meals.delete_last(_require_id(user))

    async def delete(self, user: User, meal_id: int) -> Meal | None:
        return await self._meals.delete(_require_id(user), meal_id)

    async def _resolve_product(self, parsed_item: ParsedItem) -> Product:
        existing = await self._products.get_by_key(parsed_item.canonical_key)
        if existing is not None:
            return existing

        product = Product(
            canonical_key=parsed_item.canonical_key,
            display_name=parsed_item.name,
            density=Density(
                kcal_per_100g=parsed_item.kcal_per_100g,
                protein_per_100g=parsed_item.protein_per_100g,
                fat_per_100g=parsed_item.fat_per_100g,
                carb_per_100g=parsed_item.carb_per_100g,
            ),
            unit_weight_g=parsed_item.unit_weight_g,
            source="llm",
            is_estimate=True,
        )
        return await self._products.create(product)


def _require_id(user: User) -> int:
    assert user.id is not None, "user must be persisted before logging a meal"
    return user.id
