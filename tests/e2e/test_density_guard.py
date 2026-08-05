"""E2E: пустую LLM-оценку (все макросы 0) не пиним + ретрай парса (баг «пангасиус 0 ккал»)."""
from __future__ import annotations

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.entities import Product
from app.application.results import MealLogged
from app.domain.value_objects import Density
from tests.e2e.test_meal_flow import _build, _new_user
from tests.fakes import SamplingStubNlu, StubNlu


def _fish(kcal: float, p: float, f: float, c: float) -> ParsedMessage:
    return ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="пангасиус",
                canonical_key="pangasius",
                unit=Unit.GRAM,
                grams=400,
                kcal_per_100g=kcal,
                protein_per_100g=p,
                fat_per_100g=f,
                carb_per_100g=c,
                confidence=0.9,
            )
        ],
    )


def test_names_compatible_blocks_nut_collision() -> None:
    from app.application.meal_service import _names_compatible

    # багрепорт #11: «молоко миндальное» vs «молоко макадамии» = 0.606 — РАЗНАЯ еда, не reuse
    assert _names_compatible("молоко миндальное", "молоко макадамии") is False
    assert _names_compatible("куриная грудка", "куриная грудка отварная") is True  # уточнение
    assert _names_compatible("молоко миндальное", "молоко миндальное") is True  # точное


async def test_key_collision_keeps_user_food() -> None:
    # Багрепорт #3: LLM отдал ЧУЖОЙ ключ macadamia_milk для миндального молока → не берём пин,
    # пишем как сказал юзер с его плотностью (а не «молоко макадамии» @ 40).
    almond = ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="миндальное молоко",
                canonical_key="macadamia_milk",  # LLM переиспользовал чужой ключ
                unit=Unit.GRAM,
                grams=200,
                kcal_per_100g=35,
                protein_per_100g=0,
                fat_per_100g=0,
                carb_per_100g=8.75,  # Атвотер-консистентно (4×8.75≈35)
                confidence=0.9,
            )
        ],
    )
    assistant, users, products = _build(StubNlu(almond))
    await products.create(
        Product(
            canonical_key="macadamia_milk",
            display_name="молоко макадамии",
            density=Density(40, 1, 3.5, 1),
            source="manual",
            is_estimate=False,
        )
    )
    user = await _new_user(users)

    result = await assistant.process(user, "миндальное молоко 200")

    assert isinstance(result, MealLogged)
    item = result.meal.items[0]
    assert item.name_snapshot == "миндальное молоко"  # НЕ «молоко макадамии»
    assert item.macros.kcal == 70.0  # 200г × 35/100 (миндальное), не 80 (макадамия @40)


async def test_blank_density_not_pinned() -> None:
    # LLM отдаёт пустую оценку (и на ретрае тоже) → продукт НЕ закрепляется в каталоге
    assistant, users, products = _build(StubNlu(_fish(0, 0, 0, 0)))
    user = await _new_user(users)

    await assistant.process(user, "пангасиус 400")

    assert await products.get_by_key("pangasius") is None  # каталог не отравлен
    assert "pangasius" not in await products.all_keys()


async def test_stated_totals_logged_as_is() -> None:
    # FR-22: пользователь сам назвал готовые КБЖУ (меню/этикетка) → пишем как есть, без каталога
    meal = ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="рис с гречей из кафе",
                canonical_key="cafe_dish",
                unit=Unit.PORTION,
                total_kcal=400,
                total_protein=30,
                total_fat=10,
                total_carb=50,
                confidence=0.9,
            )
        ],
    )
    assistant, users, products = _build(StubNlu(meal))
    user = await _new_user(users)

    result = await assistant.process(user, "рис с гречей из кафе 400 ккал 30/10/50")

    assert isinstance(result, MealLogged)
    it = result.meal.items[0]
    assert it.name_snapshot == "рис с гречей из кафе"
    assert (it.macros.kcal, it.macros.protein, it.macros.fat, it.macros.carb) == (400, 30, 10, 50)
    assert it.product_id is None  # в каталог не пиним
    assert await products.get_by_key("cafe_dish") is None


async def test_blank_real_food_fallback_not_zero() -> None:
    # INV-11.3: реальная еда с пустой оценкой (и на ретрае) → фолбэк ≈150/100г, не 0 в дневнике
    assistant, users, products = _build(StubNlu(_fish(0, 0, 0, 0)))
    user = await _new_user(users)

    result = await assistant.process(user, "пангасиус 400")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal > 0  # не молчаливый 0
    assert await products.get_by_key("pangasius") is None  # фолбэк в каталог не пиннится


async def test_water_blank_stays_zero() -> None:
    # вода — осознанно нулевая (FR-25): фолбэк НЕ применяется, остаётся 0
    meal = ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="вода", canonical_key="water", unit=Unit.ML, grams=500,
                kcal_per_100g=0, protein_per_100g=0, fat_per_100g=0, carb_per_100g=0,
                confidence=0.9,
            )
        ],
    )
    assistant, users, _ = _build(StubNlu(meal))
    user = await _new_user(users)

    result = await assistant.process(user, "вода 500")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal == 0


async def test_stated_totals_trusted_not_reconciled() -> None:
    # INV-11: явные тоталы пользователя доверяем как есть, НЕ перетираем Атвотером (только границы)
    meal = ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="поке", canonical_key="poke", unit=Unit.PORTION,
                total_kcal=350, total_protein=35, total_fat=17.5, total_carb=70,
                confidence=0.9,
            )
        ],
    )
    assistant, users, _ = _build(StubNlu(meal))
    user = await _new_user(users)

    result = await assistant.process(user, "поке 350 ккал 35/17.5/70")

    assert isinstance(result, MealLogged)
    assert result.meal.items[0].macros.kcal == 350  # сохранён ввод юзера, не пересчитан в 577


async def test_partial_stated_totals_preserved() -> None:
    # HIGH-1: «блюдо 400 ккал 30 белка» (частичный набор) → ккал юзера сохраняем, не зануляем
    meal = ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="блюдо из кафе", canonical_key="cafe_dish", unit=Unit.PORTION,
                total_kcal=400, total_protein=30, confidence=0.9,  # fat/carb = None
            )
        ],
    )
    assistant, users, _ = _build(StubNlu(meal))
    user = await _new_user(users)

    result = await assistant.process(user, "блюдо из кафе 400 ккал 30 белка")

    assert isinstance(result, MealLogged)
    it = result.meal.items[0]
    assert it.macros.kcal == 400 and it.macros.protein == 30  # не перетёрто Атвотером (120)


async def test_blank_coffee_with_milk_gets_fallback() -> None:
    # HIGH-3: «кофе с молоком без сахара» с пустой оценкой → фолбэк (не 0): есть «молок»
    meal = ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="кофе с молоком без сахара", canonical_key="coffee_milk", unit=Unit.ML,
                grams=200, kcal_per_100g=0, protein_per_100g=0, fat_per_100g=0, carb_per_100g=0,
                confidence=0.9,
            )
        ],
    )
    assistant, users, _ = _build(StubNlu(meal))
    user = await _new_user(users)

    result = await assistant.process(user, "кофе с молоком без сахара 200")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal > 0  # «молок» в имени → не зануляем


async def test_sampling_consensus_recovers_and_pins() -> None:
    # D-15: один сэмпл пустой, два согласны (120) → медиана не-пустых, консенсус → пин; 400×1.2=480
    samples = [_fish(0, 0, 0, 0), _fish(120, 20, 5, 0), _fish(120, 20, 5, 0)]
    assistant, users, products = _build(SamplingStubNlu(samples))
    user = await _new_user(users)

    result = await assistant.process(user, "пангасиус 400")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal == 480.0
    pinned = await products.get_by_key("pangasius")
    assert pinned is not None and pinned.density.kcal_per_100g == 120  # уверенно → пин
    assert result.meal.items[0].is_estimate is True  # llm-оценка → метка «≈»


async def test_sampling_high_variance_pinned_for_fixing() -> None:
    # Пересмотр D-16: даже неуверенная оценка пиннится → попадает в /products (юзер поправит).
    samples = [_fish(100, 5, 2, 18), _fish(220, 12, 6, 30), _fish(340, 20, 10, 45)]
    assistant, users, products = _build(SamplingStubNlu(samples))
    user = await _new_user(users)

    result = await assistant.process(user, "пангасиус 400")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal > 0  # запись есть (медиана), не молчаливый 0
    pinned = await products.get_by_key("pangasius")
    # запиннено + «≈» → правится в /products (раньше неуверенное не пиннилось → баг #1)
    assert pinned is not None and pinned.is_estimate is True
    assert result.meal.items[0].is_estimate is True


async def test_single_sample_pins_as_before() -> None:
    # один сэмпл (StubNlu/фейк) → уверенность None → пиннится как до D-15 (детерминизм цел)
    assistant, users, products = _build(StubNlu(_fish(120, 20, 5, 0)))
    user = await _new_user(users)

    result = await assistant.process(user, "пангасиус 400")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal == 480.0
    pinned = await products.get_by_key("pangasius")
    assert pinned is not None and pinned.density.kcal_per_100g == 120


async def test_alcohol_kcal_not_reconciled_down() -> None:
    # INV-14: вино — ккал от этанола; Атвотер-гейт НЕ занижает (85→127.5, а не до ~16)
    meal = ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="вино красное", canonical_key="red_wine", unit=Unit.GRAM, grams=150,
                kcal_per_100g=85, protein_per_100g=0.1, fat_per_100g=0, carb_per_100g=2.6,
                confidence=0.9,
            )
        ],
    )
    assistant, users, _ = _build(StubNlu(meal))
    user = await _new_user(users)

    result = await assistant.process(user, "вино красное 150 мл")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal == 127.5  # 85 × 1.5, НЕ пересчитано Атвотером в ~16


def _rice_chicken(rice_kcal: float, chicken_kcal: float, *, swapped: bool = False) -> ParsedMessage:
    rice = ParsedItem(
        name="рис", canonical_key="rice", unit=Unit.GRAM, grams=100,
        kcal_per_100g=rice_kcal, protein_per_100g=3, fat_per_100g=0.5, carb_per_100g=28,
        confidence=0.9,
    )
    chicken = ParsedItem(
        name="курица", canonical_key="chicken", unit=Unit.GRAM, grams=100,
        kcal_per_100g=chicken_kcal, protein_per_100g=20, fat_per_100g=10, carb_per_100g=0,
        confidence=0.9,
    )
    items = [chicken, rice] if swapped else [rice, chicken]
    return ParsedMessage(intent=Intent.MEAL, confidence=0.9, items=items)


async def test_multi_item_sampling_guard_no_corruption() -> None:
    # баг голос+мультиблюдо: сэмплы разъехались по порядку → медиана НЕ должна мешать продукты.
    # base [рис130, курица200]; сэмпл2 переставлен [курица210, рис128] → guard держит base.
    samples = [_rice_chicken(130, 200), _rice_chicken(128, 210, swapped=True)]
    assistant, users, _ = _build(SamplingStubNlu(samples))
    user = await _new_user(users)

    result = await assistant.process(user, "рис и курица")

    assert isinstance(result, MealLogged)
    by = {it.name_snapshot: it.macros.kcal for it in result.meal.items}
    assert by["рис"] == 130.0  # не смешано с курицей (без guard было бы ~170)
    assert by["курица"] == 200.0


async def test_stated_kcal_only_splits_macros() -> None:
    # INV-11.4 / AC-39: «хумус 150 ккал» — LLM не дал БЖУ → раскладка, НЕ Б0Ж0У0, помечено оценкой
    meal = ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="хумус", canonical_key="hummus", unit=Unit.PORTION,
                total_kcal=150, confidence=0.9,  # total_protein/fat/carb = None
            )
        ],
    )
    assistant, users, products = _build(StubNlu(meal))
    user = await _new_user(users)

    result = await assistant.process(user, "хумус 150 ккал")

    assert isinstance(result, MealLogged)
    it = result.meal.items[0]
    assert it.macros.kcal == 150  # ккал юзера — главная
    assert it.macros.protein > 0 and it.macros.fat > 0 and it.macros.carb > 0  # НЕ 0/0/0
    assert it.is_estimate is True  # БЖУ оценены → метка «≈»
    assert it.product_id is None
    assert await products.get_by_key("hummus") is None  # стейтед не пиннится


async def test_partial_stated_not_split() -> None:
    # частичный набор («400 ккал 30 белка») НЕ раскладываем — доверяем цифрам юзера как есть
    meal = ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="блюдо", canonical_key="dish", unit=Unit.PORTION,
                total_kcal=400, total_protein=30, confidence=0.9,  # fat/carb None
            )
        ],
    )
    assistant, users, _ = _build(StubNlu(meal))
    user = await _new_user(users)

    result = await assistant.process(user, "блюдо 400 ккал 30 белка")

    it = result.meal.items[0]
    assert (it.macros.kcal, it.macros.protein, it.macros.fat, it.macros.carb) == (400, 30, 0, 0)
    assert it.is_estimate is False  # явные цифры юзера, не оценка → метка «📋», не «≈»


async def test_water_with_syrup_not_zeroed() -> None:
    # AC-40: «вода с сиропом» с пустой оценкой — калорийная основа бьёт «вода» → фолбэк, не 0
    meal = ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="вода с сиропом", canonical_key="water_syrup", unit=Unit.ML, grams=250,
                kcal_per_100g=0, protein_per_100g=0, fat_per_100g=0, carb_per_100g=0,
                confidence=0.9,
            )
        ],
    )
    assistant, users, _ = _build(StubNlu(meal))
    user = await _new_user(users)

    result = await assistant.process(user, "вода с сиропом 250")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal > 0  # «сироп» в калорийной основе → не зануляем


# --- Находки eval-прогона 05.08.2026 ---


def _cola_zero(*, flagged: bool) -> ParsedMessage:
    """Напиток, у которого LLM вернула все нули. flagged — сказала ли она «это правда ноль»."""
    return ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name="кола зеро", canonical_key="cola_zero", unit=Unit.ML, grams=500,
                kcal_per_100g=0, protein_per_100g=0, fat_per_100g=0, carb_per_100g=0,
                zero_calorie=flagged, confidence=0.9,
            )
        ],
    )


async def test_flagged_zero_drink_stays_zero() -> None:
    """Пол-литра колы зеро — это 0 ккал, а не 750.

    Ноль трактовался как «LLM не смогла оценить» и подменялся фолбэком ~150/100 г, потому что
    список нулевых знал только воду и чёрный кофе. Курировать его дальше бессмысленно —
    зеро-напитков бесконечно много, поэтому сигнал теперь даёт сама модель.
    """
    assistant, users, _ = _build(StubNlu(_cola_zero(flagged=True)))
    user = await _new_user(users)

    result = await assistant.process(user, "кола зеро 500 мл")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal == 0.0
    assert result.meal.items[0].grams == 500  # объём записан, обнулены только калории


async def test_unflagged_zero_still_gets_fallback() -> None:
    """Обратная сторона: НЕпомеченный ноль по-прежнему промах оценки, а не еда без калорий.

    Без этого вернулся бы баг «пангасиус 0 ккал» — рыба молча записалась бы нулём.
    """
    assistant, users, _ = _build(StubNlu(_cola_zero(flagged=False)))
    user = await _new_user(users)

    result = await assistant.process(user, "кола зеро 500 мл")

    assert isinstance(result, MealLogged)
    assert result.meal.totals.kcal > 0  # консервативный фолбэк, а не тихий ноль


async def test_non_food_is_not_logged() -> None:
    """«Пизденка 230г» записывалась как 414 ккал — еды нет, писать нечего.

    Опаснее, чем выглядит: по D-16 выдуманная оценка пиннится в каталог НАВСЕГДА и дальше
    молча портит учёт. Вес рядом со словом не делает слово продуктом.
    """
    assistant, users, _ = _build(StubNlu(ParsedMessage(intent=Intent.UNKNOWN, confidence=0.2)))
    user = await _new_user(users)

    result = await assistant.process(user, "Пизденка 230г")

    assert not isinstance(result, MealLogged)
    assert (await assistant.today(user)).remaining.consumed.kcal == 0  # день не испачкан
