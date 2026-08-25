"""E2E FR-27: фото этикетки → КБЖУ/100г → вес порции → лог с ТОЧНОЙ плотностью (source=label)."""
from __future__ import annotations

from datetime import UTC, datetime

from app.application.assistant import Assistant
from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.meal_service import MealService
from app.application.results import LabelRead, MealLogged, Unknown
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.domain.goals import Goal
from tests.fakes import (
    InMemoryMealRepo,
    InMemoryProductRepo,
    InMemoryUserRepo,
    InMemoryWeightRepo,
    StubNlu,
    StubVision,
)

FIXED_NOW = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)


def _build(item: ParsedItem | None) -> tuple[Assistant, InMemoryUserRepo, InMemoryProductRepo]:
    users, products = InMemoryUserRepo(), InMemoryProductRepo()
    meals, weights = InMemoryMealRepo(), InMemoryWeightRepo()

    def clock() -> datetime:
        return FIXED_NOW

    ms = MealService(products, meals, clock)
    # StubNlu в label-флоу не вызывается, но конструктору Assistant он нужен
    nlu = StubNlu(ParsedMessage(intent=Intent.MEAL, items=[], confidence=1.0))
    assistant = Assistant(
        nlu, products, ms, WeightService(weights, clock),
        StatsService(meals, users, Goal(2000, 150, 65, 250), clock), vision=StubVision(item),
    )
    return assistant, users, products


def _label() -> ParsedItem:
    # Атвотер-консистентно: 4*3 + 9*2 + 4*12 = 78 ≈ 80
    return ParsedItem(
        name="Йогурт греческий", canonical_key="", unit=Unit.GRAM, grams=None,
        kcal_per_100g=80, protein_per_100g=3, fat_per_100g=2, carb_per_100g=12,
        unit_weight_g=250, serving_g=125, confidence=1.0,
    )


async def _user(users: InMemoryUserRepo):
    return await users.get_or_create(telegram_id=1, name="t", locale="ru", timezone="UTC")


async def test_label_read_then_grams_logs_exact() -> None:
    assistant, users, products = _build(_label())
    user = await _user(users)

    read = await assistant.read_label(user, b"fake-image", photo_file_id="tg_file_123")
    assert isinstance(read, LabelRead)
    item = read.draft.items[read.item_index]
    assert item.from_label is True and item.unit_weight_g == 250 and item.serving_g == 125

    item.grams = 100  # юзер ввёл вес порции
    item.unit = Unit.GRAM
    final = await assistant.continue_meal(user, read.draft, raw_input="этикетка")

    assert isinstance(final, MealLogged)
    mi = final.meal.items[0]
    assert round(mi.macros.kcal) == 80  # 80/100 * 100, точные цифры с этикетки
    assert mi.is_estimate is False  # этикетка = точно, НЕ «≈»
    pinned = [p for p in await products.list_all() if p.source == "label"]
    assert len(pinned) == 1 and pinned[0].display_name == "Йогурт греческий"
    assert pinned[0].photo_file_id == "tg_file_123"  # фото этикетки сохранено у продукта (#5)


async def test_label_rescan_no_duplicate() -> None:
    # FR-27: повторный скан того же продукта НЕ плодит дубль — тот же пин
    assistant, users, products = _build(_label())
    user = await _user(users)
    for _ in range(2):
        read = await assistant.read_label(user, b"img")
        assert isinstance(read, LabelRead)
        read.draft.items[0].grams = 100
        read.draft.items[0].unit = Unit.GRAM
        await assistant.continue_meal(user, read.draft, raw_input="этикетка")
    labels = [p for p in await products.list_all() if p.source == "label"]
    assert len(labels) == 1  # дедуп: один продукт, не два


async def test_rename_product() -> None:
    # FR-27 #6: обобщённое имя с этикетки → юзер переименовывает в конкретное
    assistant, users, products = _build(_label())
    user = await _user(users)
    read = await assistant.read_label(user, b"img")
    assert isinstance(read, LabelRead)
    read.draft.items[0].grams = 100
    read.draft.items[0].unit = Unit.GRAM
    await assistant.continue_meal(user, read.draft, raw_input="этикетка")

    pid = (await products.list_all())[0].id
    renamed = await assistant.rename_product(pid, "  Йогурт Dalat 3.8%  ")
    assert renamed is not None and renamed.display_name == "Йогурт Dalat 3.8%"  # обрезали пробелы
    assert await assistant.rename_product(pid, "   ") is None  # пустое имя — отказ


async def test_label_atwater_warning_on_misread() -> None:
    # Б30 вместо 3.0 (misread): атвотер 4*30+9*2+4*12=186 ≫ 80 ккал → ⚠️ предупреждение
    misread = ParsedItem(
        name="йогурт", canonical_key="", unit=Unit.GRAM, grams=None,
        kcal_per_100g=80, protein_per_100g=30, fat_per_100g=2, carb_per_100g=12, confidence=1.0,
    )
    assistant, users, _ = _build(misread)
    read = await assistant.read_label(await _user(users), b"img")
    assert isinstance(read, LabelRead) and read.atwater_warning is True


async def test_label_no_warning_when_consistent() -> None:
    assistant, users, _ = _build(_label())  # 80 ≈ атвотер(3,2,12)=78
    read = await assistant.read_label(await _user(users), b"img")
    assert isinstance(read, LabelRead) and read.atwater_warning is False


async def test_label_not_recognized_returns_unknown() -> None:
    assistant, users, _ = _build(None)  # vision не нашёл таблицу
    user = await _user(users)
    assert isinstance(await assistant.read_label(user, b"x"), Unknown)


# --- Баг #31: из вопроса про вес порции не было выхода ---


def test_label_grams_keyboard_always_offers_cancel() -> None:
    """Вопрос «сколько грамм?» обязан давать кнопку выхода.

    Это был единственный визард без отмены, и при пустых пресетах клавиатура вообще не
    приходила. Вопрос висел, а новое фото не принималось: `_busy_with_flow` отбивает фото и
    голос, пока есть незавершённое состояние. Человек оказывался заперт.
    """
    from app.bot.keyboards import WIZ_CANCEL, label_grams_keyboard

    for package_g, serving_g in ((250.0, 100.0), (None, None), (None, 30.0)):
        kb = label_grams_keyboard(package_g, serving_g)
        assert kb is not None, (package_g, serving_g)  # клавиатуры без кнопок больше нет
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert WIZ_CANCEL in data, (package_g, serving_g)
