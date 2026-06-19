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
from app.domain.nutrition import macros_for_grams, reconcile_density
from app.domain.plausibility import (
    FALLBACK_DENSITY,
    clamp_density,
    clamp_grams,
    clamp_macros,
    vet_macros,
)
from app.domain.portions import resolve_grams
from app.domain.value_objects import Density, Macros, sum_macros

# Ключи, которые LLM возвращает «от неуверенности» — не идентичность продукта.
_JUNK_KEYS = frozenset({"", "unknown", "none", "null", "na", "n_a", "item", "product", "food", "еда"})
_MIN_KEY_LEN = 3
_MATCH_THRESHOLD = 0.82  # порог имени для RAG-совпадения с каталогом
_CORRECTION_LOOKBACK = 10  # сколько недавних записей просматриваем для «удали X»
_CORRECTION_NAME_THRESHOLD = 0.8  # порог нечёткого совпадения имени при удалении
# Квалификаторы вариантов семейств (FR-20): подстрока в тексте → юзер назвал вариант явно.
# Детерминированный детект specific/generic (вместо нестабильного флага LLM `ambiguous`).
# ponytail: ведём руками рядом с семействами из seed.py; для нескольких семейств — ок.
_VARIANT_QUALIFIERS = {
    "tvorog_5": "5%",
    "tvorog_9": "9%",
    "chicken_breast_boiled": "отварн",
    "chicken_breast_fried": "жарен",
    "chicken_grilled": "гриль",
    "oatmeal_water": "вод",
    "oatmeal_milk": "молок",
    "cutlet_chicken": "курин",  # стем «куриная» (котлета)
}


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
            if parsed_item.total_kcal:  # truthy: не None и не 0 (0 → обычный путь, INV-11.3)
                # FR-22: пользователь явно назвал КБЖУ (меню/этикетка) — доверяем как есть,
                # без каталога/оценки. INV-11.2: только границы (clamp), БЕЗ Атвотер-сверки —
                # его явные цифры не перетираем (в т.ч. частичный набор «400 ккал 30 белка»).
                items.append(
                    MealItem(
                        name_snapshot=parsed_item.name,
                        grams=round(parsed_item.grams or 0.0, 1),
                        macros=clamp_macros(
                            Macros(
                                kcal=parsed_item.total_kcal,
                                protein=parsed_item.total_protein or 0.0,
                                fat=parsed_item.total_fat or 0.0,
                                carb=parsed_item.total_carb or 0.0,
                            )
                        ),
                        product_id=None,
                    )
                )
                continue
            product = await self._resolve_product_for(user_id, parsed_item)
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
                parsed_portion_sizes=parsed_item.portion_options,
            )
            grams = clamp_grams(resolution.grams)  # INV-11.2: граммы в санитарные границы
            macros = vet_macros(macros_for_grams(product.density, grams))  # INV-11 gate
            # для выверенного каталога показываем чистое имя продукта (не искажённый ввод/STT)
            display_name = (
                product.display_name
                if product.source in ("curated", "manual")
                else parsed_item.name
            )
            items.append(
                MealItem(
                    name_snapshot=display_name,
                    grams=round(grams, 1),
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

    async def remove_item_by_name(self, user: User, name: str) -> MealItem | None:
        """Коррекция «удали X»: снять самую свежую позицию, чьё имя совпало с name."""
        user_id = _require_id(user)
        recent = await self._meals.list_recent(user_id, _CORRECTION_LOOKBACK)  # newest-first
        for meal in recent:
            for item in meal.items:
                if _name_matches(name, item.name_snapshot):
                    assert meal.id is not None and item.id is not None
                    await self._meals.remove_item(user_id, meal.id, item.id)
                    return item
        return None

    async def variant_options_if_needed(
        self, user: User, item: ParsedItem
    ) -> list[Product] | None:
        """FR-20: если generic-имя неоднозначно и истории нет — варианты для вопроса; иначе None."""
        kind, products = await self._variant_resolution(_require_id(user), item)
        return products if kind == "clarify" else None

    async def _resolve_product_for(self, user_id: int, item: ParsedItem) -> Product:
        kind, products = await self._variant_resolution(user_id, item)
        if kind == "use" and products:
            return products[0]  # вариант выбран по истории/единственный в семействе
        return await self._resolve_product(item)

    async def _variant_resolution(
        self, user_id: int, item: ParsedItem
    ) -> tuple[str, list[Product]]:
        """Разрешение варианта generic-имени (FR-20). Возвращает (kind, products):

        normal — обычный путь; use — взять products[0]; clarify — спросить (products = варианты).
        Детект specific/generic — детерминированный: квалификатор варианта в тексте (5%/гриль),
        НЕ флаг LLM (он нестабилен — творог/овсянка не флажились).
        """
        if item.variant_locked:
            return ("normal", [])  # вариант выбран юзером (callback) — не переспрашивать
        key = _canonical_key(item.canonical_key, item.name)
        concrete = await self._products.get_by_key(key)
        category = concrete.category if concrete is not None else key
        if not category:
            return ("normal", [])
        variants = await self._products.list_by_category(category)
        if len(variants) < 2:
            return ("normal", [])  # семейства/выбора нет — обычный путь (LLM-оценка/RAG)
        text = _norm(item.name)
        matched = [
            v for v in variants
            if (q := _VARIANT_QUALIFIERS.get(v.canonical_key)) and q in text
        ]
        if len(matched) == 1:
            return ("use", matched)  # квалификатор → ровно этот вариант (переопределяет LLM)
        return ("clarify", variants)  # нет квалификатора → ВСЕГДА вопрос, без памяти

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
        # gate INV-11: Атвотер-сверка ккал↔БЖУ + санитарные границы плотности
        density = clamp_density(
            reconcile_density(
                Density(
                    kcal_per_100g=parsed_item.kcal_per_100g,
                    protein_per_100g=parsed_item.protein_per_100g,
                    fat_per_100g=parsed_item.fat_per_100g,
                    carb_per_100g=parsed_item.carb_per_100g,
                )
            )
        )
        if _is_blank_density(density):
            # Пустая оценка (после ретрая INV-9) — НЕ пиним в каталог (иначе мусор навсегда:
            # баг «пангасиус 0 ккал»). Что писать в саму запись — решает INV-11.3: вода/чёрный
            # кофе → 0 как есть; реальная еда → консервативный фолбэк ≈150/100г, а не молчаливый 0.
            fallback = density if _looks_zero_calorie(parsed_item.name) else FALLBACK_DENSITY
            return Product(
                canonical_key=unique_key,
                display_name=parsed_item.name,
                density=fallback,
                unit_weight_g=parsed_item.unit_weight_g,
                source="llm",
                is_estimate=True,
            )
        product = Product(
            canonical_key=unique_key,
            display_name=parsed_item.name,
            density=density,
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


def _is_blank_density(d: Density) -> bool:
    """Пустая оценка — все макросы 0 (промах LLM или 0-кал напиток). Такую не пиним в каталог."""
    return (
        d.kcal_per_100g == 0
        and d.protein_per_100g == 0
        and d.fat_per_100g == 0
        and d.carb_per_100g == 0
    )


# Калорийные основы: если есть в названии — продукт НЕ нулевой (страховка для _looks_zero_calorie).
_CALORIC_BASE = (
    "молок", "слив", "кокос", "сироп", "мёд", "мед", "орех", "какао",
    "миндал", "овс", "соя", "соев", "латте", "раф", "банан", "сахар",
)


def _looks_zero_calorie(name: str) -> bool:
    """Осознанно нулевые: вода, чёрный кофе/чай без добавок (FR-25) — им фолбэк не нужен.

    Узкая граница для INV-11.3, чтобы вода не стала 150 ккал. Калорийная основа в названии
    («кофе с молоком без сахара») → НЕ нулевой (иначе ложно занулим реальный напиток).
    ponytail: пара ключей; станет шире — выносить в каталог zero-продуктов.
    """
    n = name.strip().lower()
    if "вода" in n or "water" in n:
        return True
    if any(base in n for base in _CALORIC_BASE):
        return False  # есть калорийная основа — точно не нулевой
    return ("кофе" in n or "чай" in n) and ("чёрн" in n or "черн" in n or "без" in n)


def _name_matches(query: str, candidate: str) -> bool:
    """Совпадение имени для «удали X»: подстрока или нечёткое сходство.

    ponytail: substring+difflib по имени; если мажет (ирис/рис) — матчить по canonical_key.
    """
    q, c = _norm(query), _norm(candidate)
    if not q:
        return False
    if q in c or c in q:
        return True
    return difflib.SequenceMatcher(None, q, c).ratio() >= _CORRECTION_NAME_THRESHOLD


def _norm(text: str) -> str:
    return text.strip().lower()


def _require_id(user: User) -> int:
    assert user.id is not None, "user must be persisted before logging a meal"
    return user.id
