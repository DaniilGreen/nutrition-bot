"""Use-case: запись/чтение/удаление приёмов пищи.

Сердце модели «LLM наполняет, БД фиксирует»:
- известный продукт → плотность из БД (LLM-числа игнорируются) → детерминизм;
- RAG-страховка: совпало имя с каталогом → берём выверенную строку, не оценку LLM;
- иначе → создаём из оценки LLM (флаг is_estimate) и фиксируем навсегда.
Финальные КБЖУ считает domain.nutrition, не LLM.

Идентичность продукта — canonical_key. LLM иногда возвращает мусор ("unknown");
такой ключ нормализуем в slug из названия, иначе разные продукты сливаются.
"""
from __future__ import annotations

import difflib
import re
from collections.abc import Sequence

from app.application.dto import ParsedItem, ParsedMessage
from app.application.entities import Meal, MealItem, Product, User
from app.application.ports import MealRepo, ProductRepo
from app.clock import Clock, utcnow
from app.domain.nutrition import macros_for_grams
from app.domain.portions import resolve_grams
from app.domain.value_objects import Density, sum_macros

# Ключи, которые LLM возвращает «от неуверенности» — не идентичность продукта.
_JUNK_KEYS = frozenset({"", "unknown", "none", "null", "na", "n_a", "item", "product", "food", "еда"})
_MIN_KEY_LEN = 3
_MATCH_THRESHOLD = 0.82  # порог имени для RAG-совпадения с каталогом


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
        estimated = False
        for parsed_item in parsed.items:
            product = await self._resolve_product(parsed_item)
            estimated = estimated or product.is_estimate
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
            estimated=estimated,
        )
        return await self._meals.add(meal)

    async def recent(self, user: User, limit: int = 10) -> list[Meal]:
        return await self._meals.list_recent(_require_id(user), limit)

    async def delete_last(self, user: User) -> Meal | None:
        return await self._meals.delete_last(_require_id(user))

    async def delete(self, user: User, meal_id: int) -> Meal | None:
        return await self._meals.delete(_require_id(user), meal_id)

    async def remove_item(self, user: User, meal_id: int, item_id: int) -> Meal | None:
        return await self._meals.remove_item(_require_id(user), meal_id, item_id)

    async def _resolve_product(self, parsed_item: ParsedItem) -> Product:
        key = _canonical_key(parsed_item.canonical_key, parsed_item.name)
        existing = await self._products.get_by_key(key)
        if existing is not None:
            return existing  # детерминизм: плотность из БД, LLM-числа игнорим

        # RAG-страховка: имя совпало с реальной строкой каталога → берём её, не оценку LLM
        match = best_product_match(parsed_item.name, await self._products.list_all())
        if match is not None:
            return match

        unique_key = await self._free_key(key)
        product = Product(
            canonical_key=unique_key,
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

    async def _free_key(self, base: str) -> str:
        key, suffix = base, 2
        while await self._products.get_by_key(key) is not None:
            key = f"{base}_{suffix}"
            suffix += 1
        return key


def best_product_match(
    name: str, products: Sequence[Product], threshold: float = _MATCH_THRESHOLD
) -> Product | None:
    """Лучшее имя-совпадение из каталога (RAG: предпочесть реальную строку выдумке LLM).

    ponytail: difflib-ratio, грубо для русского — если начнёт мазать, на pg_trgm/эмбеддинги.
    Порог высокий: матчим только почти точные имена, чтобы не путать варианты.
    """
    q = _norm(name)
    best: Product | None = None
    best_score = 0.0
    for product in products:
        score = difflib.SequenceMatcher(None, q, _norm(product.display_name)).ratio()
        if score > best_score:
            best, best_score = product, score
    return best if best_score >= threshold else None


def _canonical_key(llm_key: str, name: str) -> str:
    key = (llm_key or "").strip().lower()
    if key in _JUNK_KEYS or len(key) < _MIN_KEY_LEN:
        return _slug(name)
    return key


def _slug(name: str) -> str:
    slug = re.sub(r"[^\w]+", "_", name.strip().lower()).strip("_")
    return slug or "item"


def _norm(text: str) -> str:
    return text.strip().lower()


def _require_id(user: User) -> int:
    assert user.id is not None, "user must be persisted before logging a meal"
    return user.id
