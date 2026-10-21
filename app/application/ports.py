"""Порты (Protocol-интерфейсы) — точка инверсии зависимостей.

Сервисы зависят от этих абстракций, а не от SQLAlchemy/OpenAI. В проде их
реализует infrastructure/, в тестах — in-memory фейки. Так тесты гоняются
без БД и без вызовов LLM.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from app.application.dto import ParsedItem, ParsedMessage, PlateReading, ScanReading
from app.application.entities import (
    Meal,
    MealItem,
    Product,
    Recipe,
    StatedUse,
    User,
    WeightLog,
)
from app.domain.body_scan import BodyScan
from app.domain.goals import Goal
from app.domain.value_objects import Density, Macros


class NluPort(Protocol):
    async def parse(self, text: str, known_keys: Sequence[str]) -> ParsedMessage: ...

    async def parse_samples(
        self, text: str, known_keys: Sequence[str], *, force: bool = False
    ) -> list[ParsedMessage]:
        """k независимых сэмплов оценки (self-consistency, D-15). Адаптер решает k:
        для не-meal/стейтед — один сэмпл; иначе k вызовов при temperature>0."""
        ...


class SttPort(Protocol):
    async def transcribe(self, audio: bytes, *, filename: str = "voice.ogg") -> str: ...


class VisionPort(Protocol):
    async def read_label(self, image: bytes) -> ParsedItem | None:
        """FR-27: прочитать таблицу КБЖУ с фото этикетки → ParsedItem (name + плотность/100г
        + вес пачки в unit_weight_g, если есть). None — этикетка не распознана."""
        ...

    async def read_scan(self, image: bytes) -> ScanReading | None:
        """FR-36: числа с бланка состава тела как напечатаны; None — не прочёл."""
        ...

    async def read_photo(self, image: bytes, caption: str = "") -> PlateReading:
        """FR-34: что на снимке — этикетка, блюдо или ни то ни другое; для блюда —
        составляющие с долями и оценкой граммов. Подпись — слова человека к снимку."""
        ...


class UserRepo(Protocol):
    async def get_or_create(
        self, *, telegram_id: int, name: str | None, locale: str, timezone: str
    ) -> User: ...

    async def get_goal(self, user_id: int) -> Goal | None: ...

    async def set_goal(self, user_id: int, goal: Goal, source: str = "manual") -> None: ...

    async def goal_history(self, user_id: int) -> list[tuple[datetime, float]]:
        """(с какого момента, ккал) — цели во времени, старые первыми (§6.23)."""
        ...

    async def set_timezone(self, user_id: int, tzname: str) -> None: ...

    async def set_onboarded(self, user_id: int) -> None: ...

    async def set_prefs(self, user_id: int, prefs: dict) -> None:
        """Сохранить настройки оформления целиком (§6.5). Частичность решается
        выше — домен собирает итоговый набор, репозиторий его просто кладёт."""
        ...

    async def mark_scan_nudged(self, user_id: int, at: datetime) -> None:
        """Отметить, что о замере напомнили (§6.24, MA-INV-63)."""
        ...

    async def list_active(self) -> list[User]: ...


class ProductRepo(Protocol):
    async def get_by_key(self, canonical_key: str) -> Product | None: ...

    async def get_by_id(self, product_id: int) -> Product | None: ...

    async def create(self, product: Product) -> Product: ...

    async def all_keys(self) -> list[str]: ...

    async def list_all(self) -> list[Product]: ...

    async def list_by_category(self, category: str) -> list[Product]: ...

    async def list_estimated(self, limit: int) -> list[Product]: ...

    async def search(self, query: str, limit: int) -> list[Product]:
        """Поиск по подстроке в имени/ключе, регистронезависимо (FR-15.1)."""
        ...

    async def update_density(
        self, product_id: int, density: Density, *,
        is_estimate: bool = False, unit_weight_g: float | None = None, source: str = "manual",
    ) -> Product | None:
        """Ручная правка каталога (FR-15) и перерасчёт заготовки (FR-39).
        `is_estimate` по умолчанию False — правка руками снимает «≈»; заготовка
        наследует флаг от сырья. `unit_weight_g=None` — не трогать."""
        ...

    async def get_recipe(self, product_id: int) -> Recipe | None:
        """FR-39.7: состав рецепта; None — состава нет (рецепт до 15.10.2026 или не рецепт)."""
        ...

    async def set_recipe(self, product_id: int, recipe: Recipe) -> None:
        """FR-39.7: записать состав целиком (старый заменяется)."""
        ...

    async def rename(self, product_id: int, name: str) -> Product | None:
        """Переименовать продукт (FR-27 #6: vision назвал обобщённо → юзер уточняет имя)."""
        ...

    async def list_page(
        self, *, offset: int, limit: int, query: str | None = None, source: str | None = None
    ) -> tuple[list[Product], int]:
        """Страница каталога и общее число подходящих (§7.2).

        Полный список не годится: каталог перевалил за две сотни и растёт, а экран
        показывает два десятка. Возвращаем и total, иначе нечем нарисовать «из N».
        """
        ...

    async def usage_counts(
        self, product_ids: list[int], *, user_id: int | None = None
    ) -> dict[int, int]:
        """Сколько раз продукт встречается в meal_items.

        `user_id` обязателен везде, где число показывается человеку: «ел 9 раз» —
        это персональные данные, и без фильтра один пользователь видел бы
        частоту другого. Без `user_id` — только для служебных задач вроде выбора
        выжившего при слиянии, где важен общий вес записи в каталоге.
        """
        ...

    async def merge(self, winner_id: int, loser_ids: list[int]) -> int:
        """Слить дубли в одного: перепривязать meal_items и удалить проигравших.

        INV-3: снапшот КБЖУ и граммов в meal_items НЕ трогается — история не движется,
        меняется только связь с каталогом. Возвращает число перепривязанных позиций.
        """
        ...


class MealRepo(Protocol):
    async def add(self, meal: Meal) -> Meal: ...

    async def frequent_items(
        self, user_id: int, *, days: int, limit: int
    ) -> list[tuple[int | None, str, float, int]]:
        """Что человек ест чаще всего: (product_id, имя, граммовка, сколько раз).

        Группировка по `product_id`, а не по строке имени: иначе слияние дублей
        в каталоге не склеивает подсказки, и «Творог 5%» с «творог 5%» остаются
        двумя чипами (AC-MA-33). Записи без продукта группируются по имени —
        для них другого ключа нет.

        Граммовка — медиана по последним записям, а не среднее: одна ошибка
        «2000 г» сдвинула бы среднее так, что чип перестал бы попадать в реальность.
        Окно в днях, а не «за всё время»: рацион меняется, прошлогоднее не подсказка.
        """
        ...

    async def hourly_kcal(
        self, user_id: int, *, days: int, tz: str, day_start: int = 0
    ) -> list[tuple[str, int, float]]:
        """Калории по дням и часам за окно: (местная дата, местный час, ккал).

        Часовой пояс считает БАЗА, а не питон: «день» и «час» здесь местные, и
        собирать их сдвигом на клиенте значит промахнуться на переходе суток —
        ровно там, где живёт вся эта функция (§6.20).
        """
        ...

    async def evening_items(
        self, user_id: int, *, days: int, limit: int, tz: str, since_hour: int
    ) -> list[tuple[int | None, str, float, int]]:
        """Что человек ест ПОСЛЕ указанного часа: (product_id, имя, граммовка, раз).

        Отдельно от `frequent_items`: общий список частого уже висит под полем
        ввода, и повторять его в карточке добора незачем — там нужна именно
        вечерняя еда, которой в дневнике и не хватает.
        """
        ...

    async def list_between(
        self, user_id: int, start: datetime, end: datetime
    ) -> list[Meal]: ...

    async def list_recent(self, user_id: int, limit: int) -> list[Meal]: ...

    async def contributions(
        self, user_id: int, *, days: int, limit: int, by: str = "kcal"
    ) -> list[tuple[int | None, str, float, int]]:
        """Куда уходят калории: (product_id, имя, сумма ккал, сколько раз) за окно.

        Именно сумма ккал, а не доля макросов: доли нормируются на съеденное,
        поэтому чем меньше ешь, тем лучше выглядит картина — метрика маскирует
        главную проблему (§4.1). Абсолютный вклад так не обманывает.
        """
        ...

    async def product_usage(
        self, user_id: int, *, by: str, query: str | None, offset: int, limit: int
    ) -> tuple[list[tuple[int, int]], int]:
        """Продукты, которые человек ел: страница (product_id, сколько раз) и всего (§6.25).

        `by="recent"` — по последнему разу, `by="frequent"` — по числу раз. Только свои
        приёмы: «Недавние» соседа — это чужой дневник.
        """
        ...

    async def stated_usage(self, user_id: int, *, query: str | None) -> list[StatedUse]:
        """Позиции «своими цифрами» (FR-22, FR-38) по имени: последний раз и сколько раз,
        свежие первыми (§6.25). Только свои приёмы."""
        ...

    async def last_use(
        self, user_id: int, product_ids: list[int]
    ) -> dict[int, tuple[datetime, float]]:
        """Последний раз, когда человек ел продукт: (когда, граммы). Подпись в каталоге и
        граммовка шторки по умолчанию (§6.25)."""
        ...

    async def items_of_product(
        self, user_id: int, product_id: int, *, limit: int
    ) -> list[tuple[datetime, float, float]]:
        """Последние появления продукта в приёмах: (когда, граммы, ккал).

        Нужен карточке продукта (§6.8): «где я это ел» — единственный способ
        понять, откуда взялась плотность, и стоит ли её править.
        """
        ...

    async def estimate_share(self, user_id: int, *, days: int) -> tuple[float, float]:
        """(ккал из оценок, ккал всего) за окно — для «точности учёта» (§3.5).

        Считаем по калориям, а не по числу позиций: одна оценённая тарелка борща
        весит больше десяти точных специй.
        """
        ...

    async def get(self, user_id: int, meal_id: int) -> Meal | None: ...

    async def set_template(self, user_id: int, meal_id: int, name: str) -> Meal | None:
        """FR-39.2: дать приёму имя блюда своей сборки."""
        ...

    async def latest_by_template(self, user_id: int, name: str) -> Meal | None:
        """Самый свежий приём с этим именем — источник для повтора."""
        ...

    async def template_names(self, user_id: int) -> list[str]: ...

    async def delete_last(self, user_id: int) -> Meal | None: ...

    async def delete(self, user_id: int, meal_id: int) -> Meal | None: ...

    async def remove_item(self, user_id: int, meal_id: int, item_id: int) -> Meal | None: ...

    async def replace_item(
        self, user_id: int, meal_id: int, item_id: int, items: list[MealItem]
    ) -> Meal | None:
        """Позиция → новые позиции в том же приёме, итоги пересчитаны (§6.27). None — приёма
        или позиции нет (или они чужие)."""
        ...

    async def update_item(
        self, user_id: int, meal_id: int, item_id: int, *, grams: float, macros: Macros
    ) -> Meal | None:
        """Обновить вес+КБЖУ одной позиции (коррекция граммовки) и пересчитать тоталы приёма."""
        ...


class BugRepo(Protocol):
    async def add(
        self, *, user_id: int, note: str, context: list[dict[str, object]] | None
    ) -> None:
        """Сохранить репорт о баге (FR-26): заметка юзера + контекст последних приёмов."""
        ...


class WeightRepo(Protocol):
    async def add(self, log: WeightLog) -> WeightLog: ...

    async def latest(self, user_id: int, before: datetime | None = None) -> WeightLog | None: ...

    async def history(self, user_id: int, limit: int) -> list[WeightLog]: ...

    async def delete(self, user_id: int, log_id: int) -> WeightLog | None:
        """Удалить свой замер. Без этого опечатку «83,6» вместо «93,6» починить
        нечем, а именно про такой выброс и рассказывает экран веса."""
        ...


class BodyScanRepo(Protocol):
    """Замеры состава тела (FR-36). Порядок — по времени замера."""

    async def upsert(
        self, scan: BodyScan, corrected: list[str] | None = None
    ) -> tuple[BodyScan, bool]:
        """Сохранить; тот же (user, measured_at) обновляет. Второе — «обновил».
        `corrected` — поля, которые человек поправил после распознавания (метрика)."""
        ...

    async def delete(self, user_id: int, scan_id: int) -> bool: ...

    async def get_at(self, user_id: int, measured_at: datetime) -> BodyScan | None: ...

    async def list_for(self, user_id: int) -> list[BodyScan]:
        """Все замеры человека, старые первыми."""
        ...


class SpendRepo(Protocol):
    """Учёт расходов на LLM (OQ-M3). Отдельный порт, а не поле в UserRepo:
    траты бывают и не привязанными к пользователю."""

    async def add(
        self,
        *,
        user_id: int | None,
        model: str,
        purpose: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None: ...

    async def month_limit(self, default: float) -> float:
        """Потолок трат на месяц: заданный владельцем, иначе `default` из окружения."""
        ...

    async def set_month_limit(self, usd: float) -> None:
        """Сохранить потолок. Права проверяет вызывающий (MA-INV-65)."""
        ...

    async def spent_this_month(self, *, purpose: str | None = None) -> float:
        """Потрачено с первого числа текущего месяца, в долларах.

        Лимит месячный и общий на инсталляцию, а не на пользователя: платит
        владелец, и его интересует счёт целиком.
        """
        ...
