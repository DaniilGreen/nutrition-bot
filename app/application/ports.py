"""Порты (Protocol-интерфейсы) — точка инверсии зависимостей.

Сервисы зависят от этих абстракций, а не от SQLAlchemy/OpenAI. В проде их
реализует infrastructure/, в тестах — in-memory фейки. Так тесты гоняются
без БД и без вызовов LLM.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from app.application.dto import ParsedItem, ParsedMessage
from app.application.entities import Meal, Product, User, WeightLog
from app.domain.goals import Goal
from app.domain.value_objects import Density, Macros


class NluPort(Protocol):
    async def parse(self, text: str, known_keys: Sequence[str]) -> ParsedMessage: ...

    async def parse_samples(
        self, text: str, known_keys: Sequence[str]
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


class UserRepo(Protocol):
    async def get_or_create(
        self, *, telegram_id: int, name: str | None, locale: str, timezone: str
    ) -> User: ...

    async def get_goal(self, user_id: int) -> Goal | None: ...

    async def set_goal(self, user_id: int, goal: Goal) -> None: ...

    async def set_timezone(self, user_id: int, tzname: str) -> None: ...

    async def set_onboarded(self, user_id: int) -> None: ...

    async def set_prefs(self, user_id: int, prefs: dict) -> None:
        """Сохранить настройки оформления целиком (§6.5). Частичность решается
        выше — домен собирает итоговый набор, репозиторий его просто кладёт."""
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

    async def update_density(self, product_id: int, density: Density) -> Product | None: ...

    async def rename(self, product_id: int, name: str) -> Product | None:
        """Переименовать продукт (FR-27 #6: vision назвал обобщённо → юзер уточняет имя)."""
        ...

    async def list_page(
        self, *, offset: int, limit: int, query: str | None = None
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

    async def list_between(
        self, user_id: int, start: datetime, end: datetime
    ) -> list[Meal]: ...

    async def list_recent(self, user_id: int, limit: int) -> list[Meal]: ...

    async def contributions(
        self, user_id: int, *, days: int, limit: int
    ) -> list[tuple[int | None, str, float, int]]:
        """Куда уходят калории: (product_id, имя, сумма ккал, сколько раз) за окно.

        Именно сумма ккал, а не доля макросов: доли нормируются на съеденное,
        поэтому чем меньше ешь, тем лучше выглядит картина — метрика маскирует
        главную проблему (§4.1). Абсолютный вклад так не обманывает.
        """
        ...

    async def estimate_share(self, user_id: int, *, days: int) -> tuple[float, float]:
        """(ккал из оценок, ккал всего) за окно — для «точности учёта» (§3.5).

        Считаем по калориям, а не по числу позиций: одна оценённая тарелка борща
        весит больше десяти точных специй.
        """
        ...

    async def get(self, user_id: int, meal_id: int) -> Meal | None: ...

    async def delete_last(self, user_id: int) -> Meal | None: ...

    async def delete(self, user_id: int, meal_id: int) -> Meal | None: ...

    async def remove_item(self, user_id: int, meal_id: int, item_id: int) -> Meal | None: ...

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

    async def spent_this_month(self, *, purpose: str | None = None) -> float:
        """Потрачено с первого числа текущего месяца, в долларах.

        Лимит месячный и общий на инсталляцию, а не на пользователя: платит
        владелец, и его интересует счёт целиком.
        """
        ...
