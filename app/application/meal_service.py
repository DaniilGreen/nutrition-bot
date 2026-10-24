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
from datetime import datetime, timedelta

from app.application.dto import ParsedItem, ParsedMessage, SetPick, Unit
from app.application.entities import (
    Meal,
    MealItem,
    Product,
    Recipe,
    RecipeItem,
    StatedUse,
    User,
)
from app.application.ports import MealRepo, ProductRepo
from app.clock import Clock, utcnow
from app.domain.inedible import explain
from app.domain.naming import (
    _NAME_THRESHOLD,
    _coverage,
    _names_compatible,
    _norm,
    _qualifiers,
)
from app.domain.nutrition import macros_for_grams, reconcile_density, split_macros_from_kcal
from app.domain.plausibility import (
    FALLBACK_DENSITY,
    MAX_YIELD_G,
    clamp_batch_grams,
    clamp_density,
    clamp_grams,
    clamp_macros,
    density_in_bounds,
    gate_density,
    is_alcoholic,
    vet_macros,
)
from app.domain.portions import GramsResolution, resolve_grams
from app.domain.stated_amount import stated_grams
from app.domain.value_objects import Density, Macros, sum_macros

# Ключи, которые LLM возвращает «от неуверенности» — не идентичность продукта.
_JUNK_KEYS = frozenset(
    {"", "unknown", "none", "null", "na", "n_a", "item", "product", "food", "еда"}
)
_MIN_KEY_LEN = 3
_CORRECTION_LOOKBACK = 10  # сколько недавних записей просматриваем для «удали X»
_CORRECTION_NAME_THRESHOLD = 0.8  # порог нечёткого совпадения имени при удалении
# FR-39.2: «2 курицы в лаваше», «курица в лаваше 2 шт». Только 1–2 цифры: «… 150 г» — вес, не штуки.
_COUNT_RE = re.compile(r"(?:^|(?<=\s))(\d{1,2})\s*(?:шт\.?)?(?=\s|$)")
_MAX_TIMES = 10  # больше — скорее опечатка, чем десять порций за раз
class MealService:
    def __init__(self, products: ProductRepo, meals: MealRepo, clock: Clock = utcnow) -> None:
        self._products = products
        self._meals = meals
        self._clock = clock

    async def log(
        self, user: User, parsed: ParsedMessage, *, raw_input: str, input_type: str,
        day_offset: int = 0,
    ) -> Meal:
        user_id = _require_id(user)
        items, estimated = await self._build_items(parsed, raw_input)

        # FR-33: «вчера»/«завтра» сдвигают ДЕНЬ приёма, время суток берём текущее (владелец: время
        # неважно). Кламп — страховка от галлюцинации LLM (день из будущего/древности).
        # Слово в тексте старше открытого экрана (`day_offset`): человек, листающий вчерашний
        # день и сказавший «позавчера», имел в виду позавчера, а не «ещё на день раньше».
        offset = max(-30, min(7, parsed.day_offset or day_offset))
        meal = Meal(
            user_id=user_id,
            eaten_at=self._clock() + timedelta(days=offset),
            input_type="photo" if parsed.photo else input_type,
            raw_input=raw_input,
            items=items,
            source_confidence=parsed.confidence,
            totals=sum_macros(item.macros for item in items),
            estimated=estimated,
        )
        return await self._meals.add(meal)

    async def _build_items(
        self, parsed: ParsedMessage, raw_input: str
    ) -> tuple[list[MealItem], bool]:
        """Позиции разбора → снапшоты записи: каталог (INV-2), граммовка (INV-7), сверки (INV-11,
        INV-14). Общее у записи и у «поправить словами» (§6.27)."""
        # FR-34: фото без веса — каждая позиция оценка, и это видно в ленте.
        guessed = parsed.weight_guessed

        items: list[MealItem] = []
        estimated = False
        for parsed_item in parsed.items:
            if parsed_item.total_kcal:  # truthy: не None и не 0 (0 → обычный путь, INV-11.3)
                # FR-22: пользователь явно назвал КБЖУ (меню/этикетка) — доверяем как есть,
                # без каталога/оценки. INV-11.2: только границы (clamp), БЕЗ Атвотер-сверки.
                p, f, c = parsed_item.total_protein, parsed_item.total_fat, parsed_item.total_carb
                if not p and not f and not c:
                    # INV-11.4: названа ТОЛЬКО ккал (LLM не оценил БЖУ) — НЕ пишем Б0Ж0У0,
                    # раскладываем ккал по долям и помечаем оценкой. Частичный набор сюда не идёт.
                    stated_macros = split_macros_from_kcal(parsed_item.total_kcal)
                    stated_estimate = True
                else:
                    # частичный/полный явный набор — доверяем цифрам юзера, не перетираем Атвотером.
                    stated_macros = Macros(parsed_item.total_kcal, p or 0.0, f or 0.0, c or 0.0)
                    stated_estimate = False
                grams = clamp_grams(parsed_item.grams) if parsed_item.grams else 0.0
                items.append(
                    MealItem(
                        name_snapshot=parsed_item.name,
                        grams=round(grams, 1),
                        macros=clamp_macros(stated_macros),
                        product_id=None,
                        is_estimate=stated_estimate,
                    )
                )
                estimated = estimated or stated_estimate
                continue
            product = await self._resolve_product(parsed_item)
            estimated = estimated or product.is_estimate
            # INV-7, шаг «явный вес»: объём или вес, названный прямо в имени,
            # старше любой оценки модели. Реальный случай — «Пиво Сайгон 0.3 л»
            # записалось как 500 г: модель поставила объём в имя, а граммовку
            # взяла свою «кружку пива», и цифра вышла вдвое больше названной.
            said = stated_grams(parsed_item.name)
            if said is not None and said != parsed_item.grams:
                resolution = GramsResolution(grams=said, assumed=False)
            else:
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
            raw_macros = macros_for_grams(product.density, grams)
            # INV-14: алкоголь — ккал от этанола, доверяем (только границы); иначе Атвотер-сверка
            macros = (
                clamp_macros(raw_macros)
                if is_alcoholic(parsed_item.name)
                else vet_macros(raw_macros)
            )
            # Для выверенного каталога показываем чистое имя продукта, а не искажённый ввод/STT
            # («суп фубуо большой» → «Фо бо»). НО не когда пользователь конкретнее каталога:
            # если его имя содержит каталожное целиком, он добавил бренд/вкус/линейку, и стирать
            # это нельзя — иначе «Optimum Nutrition … Double Rich Chocolate» превращается в
            # «Gold Standard Whey», и по записи уже не отличить верное совпадение от ошибочного
            # (баг #30). Мусор от STT каталожное имя не покрывает — его по-прежнему заменяем.
            user_is_specific = _coverage(product.display_name, parsed_item.name) >= 0.99
            display_name = (
                product.display_name
                if product.source in ("curated", "manual", "recipe") and not user_is_specific
                else parsed_item.name
            )
            # INV-7.1: вычли несъедобное — скажи об этом. Человек назвал 400 г с костью,
            # увидел в ленте 260 и прочитал это как поломку (багрепорт #34). Пояснение
            # идёт в СНАПШОТ позиции, а не в имя продукта: оно про одну запись, и
            # засорять им каталог нельзя (INV-3).
            note = explain(raw_input, display_name, grams)
            if guessed:
                note = f"{note} · вес на глаз" if note else "вес на глаз"
            items.append(
                MealItem(
                    name_snapshot=f"{display_name} · {note}" if note else display_name,
                    grams=round(grams, 1),
                    macros=macros,
                    product_id=product.id,
                    # INV-13: метка «≈» на оценочной позиции; вес на глаз — тоже оценка
                    is_estimate=product.is_estimate or guessed,
                )
            )
            estimated = estimated or guessed

        return items, estimated

    async def fix_item(
        self, user: User, meal_id: int, item_id: int, parsed: ParsedMessage, *, raw_input: str
    ) -> Meal | None:
        """«Поправить словами» (§6.27): позиция заменяется тем, что разобрано из слов человека, в
        том же приёме. Вес не назван — остаётся прежний: «это был булгур» меняет еду, не порцию."""
        user_id = _require_id(user)
        meal = await self._meals.get(user_id, meal_id)
        old = next((i for i in (meal.items if meal else []) if i.id == item_id), None)
        if meal is None or old is None or not parsed.items:
            return None
        if len(parsed.items) == 1 and old.grams > 0:
            it = parsed.items[0]
            named = (it.unit in (Unit.GRAM, Unit.ML) and it.grams) or (
                it.unit is Unit.PIECE and it.quantity
            ) or it.total_kcal
            if not named:
                it.unit, it.grams, it.quantity, it.portion_hint = Unit.GRAM, old.grams, None, None
        items, _ = await self._build_items(parsed, raw_input)
        return await self._meals.replace_item(user_id, meal_id, item_id, items)

    async def restore(self, meal: Meal) -> Meal:
        """«Вернуть» (MA-INV-69): приём как был до удаления — время, снапшоты, имя набора."""
        return await self._meals.add(Meal(
            user_id=meal.user_id, eaten_at=meal.eaten_at, input_type=meal.input_type,
            raw_input=meal.raw_input,
            items=[MealItem(name_snapshot=i.name_snapshot, grams=i.grams, macros=i.macros,
                            product_id=i.product_id, is_estimate=i.is_estimate)
                   for i in meal.items],
            source_confidence=meal.source_confidence, totals=meal.totals,
            estimated=meal.estimated, template_name=meal.template_name,
        ))

    async def recent(self, user: User, limit: int = 10) -> list[Meal]:
        return await self._meals.list_recent(_require_id(user), limit)

    async def order_clarify_options(self, user: User, options: list[str]) -> list[str]:
        """D-19: недавний/частый выбор юзера — первой кнопкой (порядок, не решение за него)."""
        meals = await self._meals.list_recent(_require_id(user), 20)
        names = [it.name_snapshot for m in meals for it in m.items]
        return order_options_by_history(options, names)

    async def frequent_items(
        self, user_id: int, *, days: int, limit: int
    ) -> list[tuple[str, float, int]]:
        """Что человек ест чаще всего — для чипов быстрой записи. Без LLM."""
        return await self._meals.frequent_items(user_id, days=days, limit=limit)

    async def evening_items(
        self, user_id: int, *, days: int, limit: int, tz: str, since_hour: int
    ) -> list[tuple[int | None, str, float, int]]:
        """Типичная еда после указанного часа — чипы карточки добора (§6.20)."""
        return await self._meals.evening_items(
            user_id, days=days, limit=limit, tz=tz, since_hour=since_hour
        )

    async def repeat(
        self, user: User, meal_id: int, *, day_offset: int = 0, times: int = 1
    ) -> Meal | None:
        """Тот же приём ещё раз: тот же вес, тот же снапшот КБЖУ, текущее время. Без LLM.

        Кейс владельца: снял бутылку пива с этикетки, потом выпил ещё. Копируем снапшот позиций
        (INV-3), поэтому цифры совпадают с оригиналом до грамма — не пересчитываем и не ходим в
        ИИ, где повтор дал бы другую оценку.
        """
        user_id = _require_id(user)
        src = await self._meals.get(user_id, meal_id)
        if src is None:
            return None
        # FR-39.2: имя едет с повтором — шаблон жив, пока блюдо едят, даже если исходный приём
        # удалили. Но только с повтором САМОГО набора (самого свежего приёма с этим именем): повтор
        # старой версии («понедельник, 200 г» из истории) иначе стал бы свежим и молча откатил бы
        # набор на старые граммы (FR-42, AC-117). И только одной порцией: «×2» с именем сделал бы
        # набором двойную порцию — дальше «⚡ Частое» и повтор словами писали бы вдвое (AC-119).
        template = src.template_name if times == 1 else None
        if template:
            latest = await self._meals.latest_by_template(user_id, template)
            if latest is None or latest.id != src.id:
                template = None
        return await self._meals.add(
            Meal(
                user_id=user_id,
                eaten_at=self._clock() + timedelta(days=max(-30, min(7, day_offset))),
                input_type=src.input_type,
                raw_input=src.raw_input,
                items=[
                    MealItem(
                        name_snapshot=it.name_snapshot,
                        grams=round(it.grams * times, 1),
                        macros=_times(it.macros, times),
                        product_id=it.product_id,
                        is_estimate=it.is_estimate,
                    )
                    for it in src.items
                ],
                source_confidence=src.source_confidence,
                template_name=template,
                totals=_times(src.totals, times),
                estimated=src.estimated,
            )
        )

    async def repeat_item(
        self, user: User, meal_id: int, item_id: int, *,
        grams: float | None = None, day_offset: int = 0,
    ) -> Meal | None:
        """Одна позиция приёма ещё раз — отдельным приёмом (§6.25, MA-INV-67).

        Кейс владельца: вчера хлеб записан по этикетке, сегодня этикетки нет. Берём снимок
        позиции, а не каталог: повторяется съеденное, а не то, что сейчас лежит в базе.
        Другая граммовка масштабирует снимок; позиция без веса («свои цифры») — как есть.
        """
        user_id = _require_id(user)
        src = await self._meals.get(user_id, meal_id)
        it = next((i for i in (src.items if src else []) if i.id == item_id), None)
        if src is None or it is None:
            return None
        k = grams / it.grams if grams and it.grams > 0 else 1.0
        m = it.macros
        macros = Macros(m.kcal * k, m.protein * k, m.fat * k, m.carb * k).rounded(1)
        return await self._meals.add(
            Meal(
                user_id=user_id,
                eaten_at=self._clock() + timedelta(days=max(-30, min(7, day_offset))),
                input_type=src.input_type,
                raw_input=it.name_snapshot,
                items=[
                    MealItem(
                        name_snapshot=it.name_snapshot,
                        grams=round(it.grams * k, 1),
                        macros=macros,
                        product_id=it.product_id,
                        is_estimate=it.is_estimate,
                    )
                ],
                source_confidence=src.source_confidence,
                totals=macros,
                estimated=it.is_estimate,
            )
        )

    async def contributions(
        self, user_id: int, *, days: int, limit: int, by: str = "kcal"
    ) -> list[tuple[int | None, str, float, int]]:
        """Вклад продуктов за окно: по калориям или по белку."""
        return await self._meals.contributions(user_id, days=days, limit=limit, by=by)

    async def product_usage(
        self, user_id: int, *, by: str, query: str | None, offset: int, limit: int
    ) -> tuple[list[tuple[int, int]], int]:
        """Съеденные человеком продукты по последнему разу или частоте (§6.25)."""
        return await self._meals.product_usage(
            user_id, by=by, query=query, offset=offset, limit=limit
        )

    async def last_use(
        self, user_id: int, product_ids: list[int]
    ) -> dict[int, tuple[datetime, float]]:
        return await self._meals.last_use(user_id, product_ids)

    async def stated_usage(self, user_id: int, *, query: str | None) -> list[StatedUse]:
        return await self._meals.stated_usage(user_id, query=query)

    async def items_of_product(
        self, user_id: int, product_id: int, *, limit: int = 5
    ) -> list[tuple[datetime, float, float]]:
        """Где встречался продукт."""
        return await self._meals.items_of_product(user_id, product_id, limit=limit)

    async def estimate_share(self, user_id: int, *, days: int) -> tuple[float, float]:
        """Сколько калорий пришло из оценок, а сколько из выверенного."""
        return await self._meals.estimate_share(user_id, days=days)

    async def log_known(
        self, user: User, product: Product, grams: float, *, raw_input: str,
        day_offset: int = 0,
    ) -> Meal:
        """Записать известный продукт без обращения к модели (AC-MA-31).

        Плотность берётся из каталога, а не переоценивается: продукт уже
        запиннен (INV-2), спрашивать модель заново значило бы платить деньги
        за ответ, который обязан совпасть с уже записанным.
        """
        macros = macros_for_grams(product.density, grams)
        item = MealItem(
            name_snapshot=product.display_name,
            grams=grams,
            macros=macros,
            product_id=product.id,
            is_estimate=product.is_estimate,
        )
        meal = Meal(
            user_id=_require_id(user),
            eaten_at=self._clock() + timedelta(days=max(-30, min(7, day_offset))),
            input_type="quick",
            raw_input=raw_input,
            items=[item],
            source_confidence=1.0,
            totals=macros,
        )
        return await self._meals.add(meal)

    async def adjust_item(
        self, user: User, meal_id: int, item_id: int, new_grams: float
    ) -> tuple[MealItem, float] | None:
        """Поправить вес конкретной позиции (MA-INV-22: правка достижима после
        ухода карточки «Записал»).

        Отдельно от `adjust_item_by_name`: там цель ищется нечётким совпадением
        по последним приёмам, здесь она названа точно — и промахнуться нельзя.
        """
        user_id = _require_id(user)
        new_grams = round(clamp_grams(new_grams), 1)
        meal = await self._meals.get(user_id, meal_id)
        if meal is None:
            return None
        item = next((i for i in meal.items if i.id == item_id), None)
        # Стейтед-позицию без веса масштабировать не от чего: доли нет.
        if item is None or not item.grams or item.grams <= 0:
            return None
        # Прежний вес забираем ДО записи: репозиторий может обновить тот же
        # объект, и после вызова item.grams уже новый.
        old_grams = item.grams
        new_macros = _scaled(item, new_grams)
        await self._meals.update_item(
            user_id, meal_id, item_id, grams=new_grams, macros=new_macros
        )
        updated = MealItem(
            name_snapshot=item.name_snapshot,
            grams=new_grams,
            macros=new_macros,
            product_id=item.product_id,
            is_estimate=item.is_estimate,
            id=item.id,
        )
        return updated, old_grams

    async def get(self, user: User, meal_id: int) -> Meal | None:
        """Приём по идентификатору, только свой."""
        return await self._meals.get(_require_id(user), meal_id)

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

    async def adjust_item_by_name(
        self, user: User, name: str, new_grams: float
    ) -> tuple[MealItem, float] | None:
        """Коррекция «поправь граммы у X» (FR-14.2): пересчитать самую свежую подходящую позицию
        под новый вес. Снапшот масштабируется пропорционально — для оценок плотности это ТОЧНО
        (снапшот = плотность×граммы). Стейтед-позиции без веса (граммы 0) править нельзя → None.
        """
        user_id = _require_id(user)
        new_grams = round(clamp_grams(new_grams), 1)
        recent = await self._meals.list_recent(user_id, _CORRECTION_LOOKBACK)
        for meal in recent:
            for item in meal.items:
                if _name_matches(name, item.name_snapshot) and item.grams and item.grams > 0:
                    assert meal.id is not None and item.id is not None
                    old_grams = item.grams
                    new_macros = _scaled(item, new_grams)
                    await self._meals.update_item(
                        user_id, meal.id, item.id, grams=new_grams, macros=new_macros
                    )
                    updated = MealItem(
                        name_snapshot=item.name_snapshot,
                        grams=new_grams,
                        macros=new_macros,
                        product_id=item.product_id,
                        is_estimate=item.is_estimate,
                        id=item.id,
                    )
                    return (updated, old_grams)
        return None

    async def resolves_to_existing(self, parsed_item: ParsedItem) -> bool:
        """Возьмёт ли запись эту позицию из каталога — та же логика, что `_resolve_product`,
        без создания продукта. Нужна разбору, чтобы решить, сэмплировать ли (INV-12): ключ
        мог совпасть, а продукт оказаться чужим — тогда будет новый продукт, и его плотности
        нужна медиана, а не одна оценка."""
        key = _canonical_key(parsed_item.canonical_key, parsed_item.name)
        existing = await self._products.get_by_key(key)
        if existing is not None and _names_compatible(parsed_item.name, existing.display_name):
            return True
        if parsed_item.from_label:
            return False
        return best_product_match(parsed_item.name, await self._products.list_all()) is not None

    async def save_template(self, user: User, meal_id: int, name: str) -> Meal | None:
        """FR-39.2: дать записанному приёму имя блюда своей сборки.

        Продукт в каталоге НЕ заводим: сборка — это состав, а не плотность. Умножать набор
        на вес нельзя (больше начинки ≠ больше лаваша), а состав нужен, чтобы его править.
        Сам приём не трогаем (INV-3) — добавляется только ярлык.
        """
        clean = name.strip()[:64]
        if not clean:
            return None
        meal = await self._meals.get(_require_id(user), meal_id)
        if meal is None or len(meal.items) < 2:
            return None  # одна позиция — это уже продукт, блюдо из неё делать нечего
        if any(not item.grams or item.grams <= 0 for item in meal.items):
            return None  # нечего повторять: вес позиции неизвестен
        return await self._meals.set_template(_require_id(user), meal_id, clean)

    async def repeat_template(
        self, user: User, name: str, *, day_offset: int = 0, times: int = 1
    ) -> Meal | None:
        """FR-39.2: «курица в лаваше» → повторить самый свежий приём с этим именем. Без LLM."""
        src = await self._meals.latest_by_template(_require_id(user), name.strip())
        if src is None or src.id is None:
            return None
        return await self.repeat(user, src.id, day_offset=day_offset, times=times)

    async def log_set(
        self, user: User, name: str, picks: list[SetPick], *, update: bool, day_offset: int = 0
    ) -> Meal | None:
        """FR-39.6: набор с правкой перед записью — граммы, убрать, добавить из каталога.

        Цифры считаются ЗДЕСЬ, клиенту не доверяем: позиция набора — снапшот × (новые граммы /
        старые), что точно (снапшот = плотность × граммы, INV-3); добавленный продукт — через его
        запиннутую плотность и тот же gate, что у обычной записи (INV-11).

        Имя набора приём получает, только если состав не тронут ИЛИ человек выбрал «Обновить
        набор»: самый свежий приём с именем и есть набор, и «только сегодня» его подменять не
        должен.
        """
        uid = _require_id(user)
        src = await self._meals.latest_by_template(uid, name.strip())
        if src is None:
            return None
        items: list[MealItem] = []
        for pick in picks:
            grams = round(clamp_grams(pick.grams), 1)
            if pick.src is not None:
                if not 0 <= pick.src < len(src.items) or not src.items[pick.src].grams:
                    return None  # чужой индекс — клиент рассинхронился, писать наугад нельзя
                it = src.items[pick.src]
                if pick.name is not None and pick.name != it.name_snapshot:
                    return None  # набор успели обновить с другого устройства
                items.append(MealItem(
                    name_snapshot=it.name_snapshot, grams=grams,
                    macros=_times(it.macros, grams / it.grams),
                    product_id=it.product_id, is_estimate=it.is_estimate,
                ))
            elif pick.product_id is not None:
                product = await self._products.get_by_id(pick.product_id)
                if product is None:
                    return None
                raw = macros_for_grams(product.density, grams)
                items.append(MealItem(
                    name_snapshot=product.display_name, grams=grams,
                    macros=clamp_macros(raw) if is_alcoholic(product.display_name)
                    else vet_macros(raw),
                    product_id=product.id, is_estimate=product.is_estimate,
                ))
        if not items:
            return None
        untouched = len(picks) == len(src.items) and all(
            p.src == i and abs(p.grams - src.items[i].grams) < 0.05 for i, p in enumerate(picks)
        )
        return await self._meals.add(
            Meal(
                user_id=uid,
                eaten_at=self._clock() + timedelta(days=max(-30, min(7, day_offset))),
                input_type=src.input_type,
                raw_input=src.raw_input,
                items=items,
                source_confidence=src.source_confidence,
                template_name=src.template_name if (untouched or update) else None,
                totals=sum_macros(i.macros for i in items),
                estimated=any(i.is_estimate for i in items),
            )
        )

    async def sets(self, user: User) -> list[Meal]:
        """FR-39.5: по одному на имя — самый свежий состав (он и есть набор), свежие первыми."""
        uid = _require_id(user)
        found = [
            await self._meals.latest_by_template(uid, name)
            for name in await self._meals.template_names(uid)
        ]
        return sorted((m for m in found if m is not None), key=lambda m: m.eaten_at, reverse=True)

    async def template_match(self, user: User, text: str) -> tuple[str, int] | None:
        """Сообщение ЦЕЛИКОМ — своё блюдо? Возвращает (имя, сколько штук) или None.

        СИММЕТРИЧНО, как RAG (INV-5.1): «курица в лаваше и кофе» содержит набор, но им не является.
        Асимметрия засчитала бы совпадение и молча выкинула кофе — съеденное пропало бы из дня.
        Число рядом с именем — штуки: «2 курицы в лаваше», «курица в лаваше 2 шт».
        """
        probe = text.strip()
        if not probe or len(probe) > 64:
            return None
        times = 1
        count = _COUNT_RE.search(probe)
        if count is not None:
            times = int(count.group(1))
            probe = (probe[: count.start()] + " " + probe[count.end() :]).strip()
        if not probe or not 1 <= times <= _MAX_TIMES:
            return None
        for name in await self._meals.template_names(_require_id(user)):
            if _qualifiers(probe) != _qualifiers(name):
                continue
            if min(_coverage(probe, name), _coverage(name, probe)) >= _NAME_THRESHOLD:
                return name, times
        return None

    async def ingredient(self, parsed_item: ParsedItem) -> RecipeItem:
        """Позиция разбора → ингредиент рецепта через каталог (FR-39.1, FR-39.8): у известного
        сырья — запиннутая плотность, не свежая выдумка (INV-2); граммы — по INV-7."""
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
            parsed_portion_sizes=parsed_item.portion_options,
        )
        return RecipeItem(
            name=product.display_name,
            grams=round(clamp_batch_grams(resolution.grams), 1),  # партия, не порция
            density=product.density,
            product_id=product.id,  # None у непиннутого фолбэка — останется снимок
            is_estimate=product.is_estimate,
            alcohol=is_alcoholic(parsed_item.name) or is_alcoholic(product.display_name),
        )

    async def prepare_batch(self, parsed: ParsedMessage) -> Product | None:
        """FR-39.1: ингредиенты → рецепт в каталоге. Приём пищи НЕ пишем. Состав сохраняем
        (FR-39.7) — иначе его потом не поправить. RecipeRejected — вес готового не сходится."""
        items = [await self.ingredient(parsed_item) for parsed_item in parsed.items]
        if not items:
            return None
        total, grams_total, estimate = _cook(items)
        named = parsed.batch_yield_g
        if named is not None and named <= 0:
            named = None  # модель вернула 0 — значит «не назван», как и у API
        if named is not None and named > MAX_YIELD_G:
            raise RecipeRejected("Вес готового больше 50 кг — проверь число.")
        # Вес готового: назвали — берём его (при варке вода ушла, масса не равна сумме сырья);
        # не назвали — сумма весов сырья, что для сборки без термообработки ТОЧНО, а не почти.
        yield_g = named or grams_total
        if yield_g <= 0:
            return None
        portions = parsed.batch_portions if (parsed.batch_portions or 0) > 0 else None
        product = await self._pin_dish(
            (parsed.batch_name or parsed.items[0].name),
            _recipe_density(total, yield_g),
            unit_weight=round(yield_g / portions, 1) if portions else None,
            estimate=estimate,
        )
        assert product.id is not None
        for it in items:
            # Рецепт назван как свой ингредиент, или ингредиент — рецепт, в котором есть этот:
            # ссылку не храним, остаётся снимок плотности — иначе цикл раздувал бы плотность.
            if it.product_id is not None and (
                it.product_id == product.id or await self._contains(it.product_id, product.id)
            ):
                it.product_id = None
        await self._products.set_recipe(product.id, Recipe(items=items, yield_g=named or None))
        return product

    async def _contains(self, recipe_id: int | None, target_id: int, depth: int = 0) -> bool:
        """Есть ли `target_id` в составе `recipe_id` — напрямую или через вложенные рецепты.
        ponytail: обход в глубину без кэша — рецептов единицы; глубина 8 — страховка от
        уже испорченных данных."""
        if recipe_id is None or depth > 8:
            return False
        recipe = await self._products.get_recipe(recipe_id)
        if recipe is None:
            return False
        for it in recipe.items:
            if it.product_id == target_id:
                return True
            if it.product_id is not None and await self._contains(
                it.product_id, target_id, depth + 1
            ):
                return True
        return False

    async def recipe_view(self, product_id: int) -> Recipe | None:
        """FR-39.7: состав для карточки — с ТЕКУЩИМИ плотностями ингредиентов. Ровно ими же
        считает правка, поэтому предпросмотр в аппке совпадает с тем, что запишет сервер."""
        recipe = await self._products.get_recipe(product_id)
        if recipe is None:
            return None
        for it in recipe.items:
            if it.product_id == product_id:
                it.product_id = None  # сам себе не ингредиент: только снимок, иначе раздувание
                continue
            live = await self._products.get_by_id(it.product_id) if it.product_id else None
            if live is not None:
                it.density, it.is_estimate = live.density, live.is_estimate
                it.name = live.display_name
            else:
                it.product_id = None  # ингредиент удалён — остаётся снимок
        return recipe

    async def edit_recipe(
        self, product_id: int, picks: list[SetPick], *, yield_g: float | None
    ) -> Product | None:
        """FR-39.7: новый состав рецепта → пересчёт плотности. Цифры считает сервер: клиент
        шлёт индекс позиции текущего состава (`src`, с именем — сверяем, что это та же позиция)
        или `product_id` из каталога, плюс граммы.

        Меняются будущие записи; съеденное не двигается — в приёмах снапшот (INV-3).
        None — не рецепт, состав успели изменить, пустой состав, чужой или несуществующий
        продукт. RecipeRejected — вес готового вне границ или невозможная плотность.
        """
        recipe_product = await self._products.get_by_id(product_id)
        if recipe_product is None or recipe_product.source != "recipe":
            return None
        current = await self.recipe_view(product_id)
        items: list[RecipeItem] = []
        for pick in picks:
            grams = round(clamp_batch_grams(pick.grams), 1)
            if pick.src is not None:
                if current is None or not 0 <= pick.src < len(current.items):
                    return None  # индекс мимо состава — писать наугад нельзя
                it = current.items[pick.src]
                if pick.name is not None and pick.name != it.name:
                    return None  # состав успели изменить с другого устройства
                items.append(RecipeItem(
                    it.name, grams, it.density, it.product_id, it.is_estimate, it.alcohol
                ))
            elif pick.product_id is not None:
                if pick.product_id == product_id:
                    return None  # рецепт не может входить сам в себя
                live = await self._products.get_by_id(pick.product_id)
                if live is None:
                    return None
                if await self._contains(live.id, product_id):
                    return None  # A в B, а B в A — плотность раздувалась бы на каждом сохранении
                items.append(RecipeItem(
                    live.display_name, grams, live.density, live.id, live.is_estimate,
                    is_alcoholic(live.display_name),
                ))
        if not items:
            return None
        if yield_g is not None and not 0 < yield_g <= MAX_YIELD_G:
            raise RecipeRejected("Вес готового должен быть от 1 г до 50 кг — проверь число.")
        total, grams_total, estimate = _cook(items)
        new_yield = yield_g or grams_total
        unit_weight = None
        if recipe_product.unit_weight_g and current is not None:
            # Партию делили на N порций — после правки порция = новый вес / N, иначе
            # «1 порция» врала бы ровно на долю изменения (ревью 15.10.2026).
            old_yield = current.yield_g or sum(i.grams for i in current.items)
            portions = old_yield / recipe_product.unit_weight_g
            if portions > 0:
                unit_weight = round(new_yield / portions, 1)
        updated = await self._products.update_density(
            product_id, _recipe_density(total, new_yield),
            is_estimate=estimate, unit_weight_g=unit_weight, source="recipe",
        )
        await self._products.set_recipe(
            product_id, Recipe(items=items, yield_g=round(yield_g, 1) if yield_g else None)
        )
        return updated

    async def _pin_dish(
        self, name: str, density: Density, *, unit_weight: float | None, estimate: bool
    ) -> Product:
        """Общий пин обоих входов. Собрал то же блюдо заново — обновляем, а не плодим дубль."""
        clean = name.strip()[:255] or "блюдо"
        key = _slug(clean)
        existing = await self._products.get_by_key(key)
        if existing is not None and _names_compatible(clean, existing.display_name):
            assert existing.id is not None
            updated = await self._products.update_density(
                existing.id, density, is_estimate=estimate, unit_weight_g=unit_weight,
                source="recipe",
            )
            if updated is not None:
                return updated
        return await self._products.create(
            Product(
                canonical_key=await self._free_key(key),
                display_name=clean,
                density=density,
                unit_weight_g=unit_weight,
                # Свой источник, а не "manual": иначе рецепт не отличить от продукта,
                # который просто поправили руками (FR-15), и «Моё» не собрать (FR-39.5).
                source="recipe",
                is_estimate=estimate,
            )
        )

    async def _resolve_product(self, parsed_item: ParsedItem) -> Product:
        key = _canonical_key(parsed_item.canonical_key, parsed_item.name)
        existing = await self._products.get_by_key(key)
        if existing is not None and _names_compatible(parsed_item.name, existing.display_name):
            return existing  # детерминизм + дедуп повторного скана (тот же ключ → тот же пин)
        # имя не совпало с пином → чужой ключ (миндальное → macadamia_milk, баг #3): не берём
        if not parsed_item.from_label:  # RAG-страховка — только для текста/голоса, не для этикетки
            match = best_product_match(parsed_item.name, await self._products.list_all())
            if match is not None:
                return match

        unique_key = await self._free_key(key)
        # gate INV-11: Атвотер-сверка ккал↔БЖУ + санитарные границы плотности.
        # INV-14: алкоголь — ккал от этанола, НЕ сверяем Атвотером (иначе занизим в разы).
        raw_density = Density(
            kcal_per_100g=parsed_item.kcal_per_100g,
            protein_per_100g=parsed_item.protein_per_100g,
            fat_per_100g=parsed_item.fat_per_100g,
            carb_per_100g=parsed_item.carb_per_100g,
        )
        # этикетка (FR-27) и алкоголь — плотность как есть, без Атвотер-реконсиляции
        density = clamp_density(
            raw_density
            if (is_alcoholic(parsed_item.name) or parsed_item.from_label)
            else reconcile_density(raw_density)
        )
        if _is_blank_density(density):
            # Пустая оценка (после ретрая INV-9) — НЕ пиним в каталог (иначе мусор навсегда:
            # баг «пангасиус 0 ккал»). Что писать в саму запись — решает INV-11.3: вода/чёрный
            # кофе → 0 как есть; реальная еда → консервативный фолбэк ≈150/100г, а не молчаливый 0.
            # Флаг от LLM — основной сигнал, список слов остался страховкой: он знал только воду
            # и чёрный кофе, поэтому «кола зеро» получала фолбэк и превращалась в 750 ккал на
            # пол-литра. Курировать список дальше бессмысленно — зеро-напитков бесконечно много.
            zero = parsed_item.zero_calorie or _looks_zero_calorie(parsed_item.name)
            fallback = density if zero else FALLBACK_DENSITY
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
            source="label" if parsed_item.from_label else "llm",  # FR-27: этикетка = точно, без «≈»
            is_estimate=not parsed_item.from_label,
            photo_file_id=parsed_item.label_photo_id if parsed_item.from_label else None,
        )
        # Пиним ЛЮБУЮ LLM-оценку → она попадает в /products, и юзер может её поправить (ручной
        # escape-hatch — это и есть путь правки сомнительного). Сэмплинг (D-15) улучшает первую
        # оценку, «≈» помечает. Пересмотр D-16: НЕ-пиннинг неуверенных ПРЯТАЛ их из /products —
        # баг (нельзя было поправить именно сомнительное; багрепорт #1).
        return await self._products.create(product)

    async def _free_key(self, base: str) -> str:
        key, suffix = base, 2
        while await self._products.get_by_key(key) is not None:
            key = f"{base}_{suffix}"
            suffix += 1
        return key


def best_product_match(
    name: str, products: Sequence[Product], threshold: float = _NAME_THRESHOLD
) -> Product | None:
    """Лучшее имя-совпадение из каталога (RAG: предпочесть реальную строку выдумке LLM).

    INV-5.1: симметрично — тут ключ идентичность не подтверждает, сканируем ВЕСЬ каталог.
    Асимметрия пустила бы «миндаль» (605) в «миндальное молоко» (30): короткое имя покрывается
    длинным целиком. Требуем покрытие в обе стороны.
    """
    best: Product | None = None
    best_score = 0.0
    for product in products:
        pinned = product.display_name
        if _qualifiers(name) != _qualifiers(pinned):
            continue  # INV-4.3: «творог 5%» ≠ «творог 9%»
        score = min(_coverage(name, pinned), _coverage(pinned, name))
        if score > best_score:
            best, best_score = product, score
    return best if best_score >= threshold else None


def _times(m: Macros, times: float) -> Macros:
    """Снапшот × k. Точно, а не приближённо: снапшот и есть плотность × граммы (INV-3)."""
    if times == 1:
        return m
    return Macros(m.kcal * times, m.protein * times, m.fat * times, m.carb * times)


def _cook(items: list[RecipeItem]) -> tuple[Macros, float, bool]:
    """Партия рецепта: КБЖУ, общий вес сырья, есть ли оценочный ингредиент.

    Gate каждого ингредиента — на уровне ПЛОТНОСТИ (`gate_density`): Атвотер и границы на
    100 г. Не на итогах позиции — потолки порции резали большие партии (ревью 15.10.2026).
    """
    pot: list[Macros] = []
    grams_total = 0.0
    for it in items:
        d = gate_density(it.density, alcohol=it.alcohol or is_alcoholic(it.name))
        pot.append(macros_for_grams(d, it.grams))
        grams_total += it.grams
    return sum_macros(pot), grams_total, any(it.is_estimate for it in items)


_DENSITY_SLACK = 1.15  # до +15% над физической границей — округления, дальше — опечатка в весе


class RecipeRejected(ValueError):
    """Рецепт не сходится физически: вес готового вне границ или плотность невозможна.
    Сообщение — человеку."""


def _recipe_density(total: Macros, grams: float) -> Density:
    """Плотность партии. Вне физических границ — отказ, а не зажим: «вышло 100 г» вместо
    1000 дало бы 900 ккал и 100 г жира на 100 г — молча записать невозможное нельзя."""
    raw = Density(
        kcal_per_100g=total.kcal / grams * 100,
        protein_per_100g=total.protein / grams * 100,
        fat_per_100g=total.fat / grams * 100,
        carb_per_100g=total.carb / grams * 100,
    )
    if density_in_bounds(raw):
        return raw
    if density_in_bounds(raw, slack=_DENSITY_SLACK):
        return clamp_density(raw)  # у самой границы (масло, топлёное) — шум округлений, жмём
    raise RecipeRejected(
        "Так не бывает: на 100 г выходит больше, чем физически возможно. Скорее всего, вес "
        "готового указан меньше, чем на самом деле."
    )


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
    "варень", "сладк",  # «вода с вареньем», «сладкая вода» — калорийны, не зануляем (AC-40)
)


def _looks_zero_calorie(name: str) -> bool:
    """Осознанно нулевые: вода, чёрный кофе/чай без добавок (FR-25) — им фолбэк не нужен.

    Узкая граница для INV-11.3, чтобы вода не стала 150 ккал. КАЛОРИЙНАЯ ОСНОВА проверяется
    ПЕРВОЙ и бьёт подстроку «вода» — иначе «вода с сиропом/мёдом/вареньем» ложно занулялась бы
    (AC-40). ponytail: пара ключей; станет шире — выносить в каталог zero-продуктов.
    """
    n = name.strip().lower()
    if any(base in n for base in _CALORIC_BASE):
        return False  # калорийная основа — точно не нулевой (даже «вода с сиропом»)
    if "вода" in n or "water" in n:
        return True
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


def order_options_by_history(options: list[str], recent_names: list[str]) -> list[str]:
    """D-19: подсортировать варианты-кнопки — недавний выбор юзера первой кнопкой.

    recent_names — имена позиций недавних приёмов, СВЕЖИЕ первыми. Вариант, совпавший с недавним
    именем, всплывает (чем свежее — тем выше); прочие сохраняют порядок LLM (по убыванию
    вероятности). Память НЕ решает за юзера (D-6/D-19) — только удобный порядок, выбор всегда его.
    """
    norm_recent = [_norm(r) for r in recent_names]

    def rank(opt: str) -> tuple[int, int]:
        no = _norm(opt)
        for i, r in enumerate(norm_recent):
            if no in r or r in no or difflib.SequenceMatcher(None, no, r).ratio() >= 0.7:
                return (0, i)  # встречался → вперёд, свежее = выше
        return (1, options.index(opt))  # не встречался → после, в LLM-порядке

    return sorted(options, key=rank)


def _scaled(item: MealItem, new_grams: float) -> Macros:
    """Снапшот масштабируется пропорционально, а НЕ пересчитывается из каталога.

    INV-3: съеденное вчера не должно поехать от того, что сегодня поправили
    плотность продукта. Правим вес — двигаем только его долю.
    """
    factor = new_grams / item.grams
    m = item.macros
    return Macros(
        round(m.kcal * factor, 1),
        round(m.protein * factor, 1),
        round(m.fat * factor, 1),
        round(m.carb * factor, 1),
    )


def _require_id(user: User) -> int:
    assert user.id is not None, "user must be persisted before logging a meal"
    return user.id
