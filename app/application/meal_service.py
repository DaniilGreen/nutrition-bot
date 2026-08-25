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
from datetime import timedelta

from app.application.dto import ParsedItem, ParsedMessage
from app.application.entities import Meal, MealItem, Product, User
from app.application.ports import MealRepo, ProductRepo
from app.clock import Clock, utcnow
from app.domain.nutrition import macros_for_grams, reconcile_density, split_macros_from_kcal
from app.domain.plausibility import (
    FALLBACK_DENSITY,
    clamp_density,
    clamp_grams,
    clamp_macros,
    is_alcoholic,
    vet_macros,
)
from app.domain.portions import resolve_grams
from app.domain.value_objects import Density, Macros, sum_macros

# Ключи, которые LLM возвращает «от неуверенности» — не идентичность продукта.
_JUNK_KEYS = frozenset(
    {"", "unknown", "none", "null", "na", "n_a", "item", "product", "food", "еда"}
)
_MIN_KEY_LEN = 3
_NAME_THRESHOLD = 0.72  # доля токенов имени, нашедших пару (INV-4.2)
_CORRECTION_LOOKBACK = 10  # сколько недавних записей просматриваем для «удали X»
_CORRECTION_NAME_THRESHOLD = 0.8  # порог нечёткого совпадения имени при удалении
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
                if product.source in ("curated", "manual") and not user_is_specific
                else parsed_item.name
            )
            items.append(
                MealItem(
                    name_snapshot=display_name,
                    grams=round(grams, 1),
                    macros=macros,
                    product_id=product.id,
                    is_estimate=product.is_estimate,  # INV-13: метка «≈» на оценочной позиции
                )
            )

        # FR-33: «вчера»/«завтра» сдвигают ДЕНЬ приёма, время суток берём текущее (владелец: время
        # неважно). Кламп — страховка от галлюцинации LLM (день из будущего/древности).
        offset = max(-30, min(7, parsed.day_offset))
        meal = Meal(
            user_id=user_id,
            eaten_at=self._clock() + timedelta(days=offset),
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

    async def order_clarify_options(self, user: User, options: list[str]) -> list[str]:
        """D-19: недавний/частый выбор юзера — первой кнопкой (порядок, не решение за него)."""
        meals = await self._meals.list_recent(_require_id(user), 20)
        names = [it.name_snapshot for m in meals for it in m.items]
        return order_options_by_history(options, names)

    async def repeat(self, user: User, meal_id: int) -> Meal | None:
        """Тот же приём ещё раз: тот же вес, тот же снапшот КБЖУ, текущее время. Без LLM.

        Кейс владельца: снял бутылку пива с этикетки, потом выпил ещё. Копируем снапшот позиций
        (INV-3), поэтому цифры совпадают с оригиналом до грамма — не пересчитываем и не ходим в
        ИИ, где повтор дал бы другую оценку.
        """
        user_id = _require_id(user)
        src = await self._meals.get(user_id, meal_id)
        if src is None:
            return None
        return await self._meals.add(
            Meal(
                user_id=user_id,
                eaten_at=self._clock(),
                input_type=src.input_type,
                raw_input=src.raw_input,
                items=[
                    MealItem(
                        name_snapshot=it.name_snapshot,
                        grams=it.grams,
                        macros=it.macros,
                        product_id=it.product_id,
                        is_estimate=it.is_estimate,
                    )
                    for it in src.items
                ],
                source_confidence=src.source_confidence,
                totals=src.totals,
                estimated=src.estimated,
            )
        )

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
                    factor = new_grams / item.grams
                    old_grams, m = item.grams, item.macros
                    new_macros = Macros(
                        round(m.kcal * factor, 1),
                        round(m.protein * factor, 1),
                        round(m.fat * factor, 1),
                        round(m.carb * factor, 1),
                    )
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


def _names_compatible(parsed_name: str, pinned_name: str) -> bool:
    """Имя из ввода и имя запиннутого продукта — одна еда? Защита от reuse ЧУЖОГО ключа: LLM
    иногда отдаёт ключ другого продукта (молоко миндальное → macadamia_milk «молоко макадамии»).

    INV-5.1: асимметрично — ключ уже утверждает идентичность, имя ловит грубый промах, поэтому
    уточнение засчитывается («куриная грудка» ⊂ «куриная грудка отварная»). INV-4.3 бьёт первой:
    разошлись числа-квалификаторы («творог 5%»/«творог 9%») — разная еда при любом покрытии.
    """
    if _qualifiers(parsed_name) != _qualifiers(pinned_name):
        return False
    best = max(_coverage(parsed_name, pinned_name), _coverage(pinned_name, parsed_name))
    return best >= _NAME_THRESHOLD


def _norm(text: str) -> str:
    return text.strip().lower()


# Порционные числа — шум имени («йогурт 200 г» и «йогурт» одна еда), в отличие от жирности.
_PORTION_RE = re.compile(r"\b\d+[.,]?\d*\s*(?:г|гр|грамм\w*|мл|л|кг|g|ml|l|kg|шт|скуп\w*)\b")
# Предлоги: еду не различают, но разбавляют счёт («арбузный сок БЕЗ сахара» vs «сок гуавы БЕЗ
# сахара» — 3 общих токена из 4). «сахара» НЕ выбрасываем: «кола без сахара» ≠ «кола» по ккал.
_STOP_WORDS = frozenset({"с", "со", "из", "и", "в", "на", "для", "без", "по"})


def _name_tokens(name: str) -> list[str]:
    """Слова имени: ё→е, без предлогов и без цифр. Цифры целиком на совести `_qualifiers` —
    иначе общая «3.2» тянет совпадение вверх («кофе с молоком 3.2%» ≈ «молоко 3.2%»)."""
    words = re.findall(r"[^\W\d_]+", _PORTION_RE.sub(" ", _norm(name).replace("ё", "е")))
    return [w for w in words if w not in _STOP_WORDS] or words


def _same_word(a: str, b: str) -> bool:
    """Словоформы одного слова: общий префикс ≥3 символов И ≥60% ДЛИННОГО токена (INV-4.2).

    По длинному, а не короткому: иначе «сыр» ⊂ «сырники», «суши» ⊂ «сушёный», «бур» ⊂ «бургер» —
    разная еда с общим началом склеивается. ponytail: префикс вместо стеммера; русский флексирует
    хвостом, этого хватает. Понадобится больше — pymorphy/pg_trgm.
    """
    if a == b:
        return True
    common = 0
    for x, y in zip(a, b, strict=False):  # слова разной длины — обрезка по короткому тут и нужна
        if x != y:
            break
        common += 1
    return common >= 3 and common >= 0.6 * max(len(a), len(b))


def _coverage(name: str, other: str) -> float:
    """Доля токенов `name`, нашедших пару в `other`. НАПРАВЛЕННАЯ: _coverage(a,b) != _coverage(b,a).

    Заменила difflib по склеенной строке (D-32): та зависела от порядка слов («пиво нефильтрованное
    светлое» vs «светлое пиво нефильтрованное» = 0.714 при пороге 0.72), а в русском порядок
    свободный; и наоборот, роднила разные слова с общими буквами («жареные»/«варёные» = 0.86).
    """
    tokens, pool = _name_tokens(name), _name_tokens(other)
    if not tokens or not pool:
        return 0.0
    hit = 0
    for token in tokens:
        match = next((w for w in pool if _same_word(token, w)), None)
        if match is not None:
            pool.remove(match)
            hit += 1
    return hit / len(tokens)


def _qualifiers(name: str) -> list[str]:
    """Числа-квалификаторы имени (жирность «3.2%», «творог 5%») — часть идентичности (INV-4.3).
    Порционные числа вырезаны: «200 г» продукт не различает."""
    stripped = _PORTION_RE.sub(" ", _norm(name).replace(",", "."))
    return sorted(re.findall(r"\d+[.,]?\d*", stripped))


def _require_id(user: User) -> int:
    assert user.id is not None, "user must be persisted before logging a meal"
    return user.id
