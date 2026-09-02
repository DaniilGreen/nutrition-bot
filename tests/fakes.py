"""In-memory реализации портов + StubNlu/StubStt для тестов без БД и без LLM."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from app.application.dto import ParsedItem, ParsedMessage
from app.application.entities import Meal, Product, User, WeightLog
from app.domain.goals import Goal
from app.domain.value_objects import Density, Macros, sum_macros


class InMemoryUserRepo:
    def __init__(self) -> None:
        self._users: dict[int, User] = {}
        self._goals: dict[int, Goal] = {}
        self._seq = 0

    async def get_or_create(
        self, *, telegram_id: int, name: str | None, locale: str, timezone: str
    ) -> User:
        for u in self._users.values():
            if u.telegram_id == telegram_id:
                return u
        self._seq += 1
        user = User(
            id=self._seq, telegram_id=telegram_id, name=name, locale=locale, timezone=timezone
        )
        self._users[self._seq] = user
        return user

    async def get_goal(self, user_id: int) -> Goal | None:
        return self._goals.get(user_id)

    async def set_goal(self, user_id: int, goal: Goal) -> None:
        self._goals[user_id] = goal

    async def set_timezone(self, user_id: int, tzname: str) -> None:
        if user_id in self._users:
            self._users[user_id].timezone = tzname

    async def set_onboarded(self, user_id: int) -> None:
        if user_id in self._users and self._users[user_id].onboarded_at is None:
            self._users[user_id].onboarded_at = datetime(2026, 1, 1)  # сентинел «онбординнут»

    async def set_prefs(self, user_id: int, prefs: dict) -> None:
        if user_id in self._users:
            self._users[user_id].ui_prefs = prefs

    async def list_active(self) -> list[User]:
        return list(self._users.values())


class InMemoryProductRepo:
    def __init__(self) -> None:
        self._by_key: dict[str, Product] = {}
        self._seq = 0
        self._uses: dict[int, int] = {}
        self._uses_by_user: dict[int, dict[int, int]] = {}

    async def get_by_key(self, canonical_key: str) -> Product | None:
        return self._by_key.get(canonical_key)

    async def get_by_id(self, product_id: int) -> Product | None:
        for p in self._by_key.values():
            if p.id == product_id:
                return p
        return None

    async def create(self, product: Product) -> Product:
        self._seq += 1
        product.id = self._seq
        self._by_key[product.canonical_key] = product
        return product

    async def all_keys(self) -> list[str]:
        return list(self._by_key.keys())

    async def list_all(self) -> list[Product]:
        return list(self._by_key.values())

    async def list_by_category(self, category: str) -> list[Product]:
        return [p for p in self._by_key.values() if p.category == category]

    async def list_page(
        self, *, offset: int, limit: int, query: str | None = None
    ) -> tuple[list[Product], int]:
        xs = list(self._by_key.values())
        if query:
            q = query.lower()
            xs = [p for p in xs if q in p.display_name.lower() or q in p.canonical_key.lower()]
        xs.sort(key=lambda p: p.display_name)
        return xs[offset : offset + limit], len(xs)

    async def usage_counts(
        self, product_ids: list[int], *, user_id: int | None = None
    ) -> dict[int, int]:
        # Фейк каталога не знает про приёмы — счётчик подставляется тестом
        # через `set_usage`. Фильтр по пользователю моделируется вторым словарём:
        # без этого утечка между юзерами прошла бы мимо тестов.
        src = self._uses_by_user.get(user_id, {}) if user_id is not None else self._uses
        return {i: src.get(i, 0) for i in product_ids}

    def set_usage(self, product_id: int, count: int, *, user_id: int | None = None) -> None:
        if user_id is None:
            self._uses[product_id] = count
        else:
            self._uses_by_user.setdefault(user_id, {})[product_id] = count

    async def merge(self, winner_id: int, loser_ids: list[int]) -> int:
        losers = [i for i in loser_ids if i != winner_id]
        moved = sum(self._uses.pop(i, 0) for i in losers)
        self._uses[winner_id] = self._uses.get(winner_id, 0) + moved
        for key, prod in list(self._by_key.items()):
            if prod.id in losers:
                del self._by_key[key]
        return moved

    async def list_estimated(self, limit: int) -> list[Product]:
        # Новейшие первыми — как в SQL-репозитории (order_by created_at desc). Порядок вставки
        # в dict = порядок создания, поэтому просто разворачиваем: иначе фейк врал бы про
        # «10 последних» и тест на лимит проходил бы вхолостую.
        estimates = [p for p in self._by_key.values() if p.is_estimate]
        return estimates[::-1][:limit]

    async def search(self, query: str, limit: int) -> list[Product]:
        q = query.casefold()
        hits = [
            p
            for p in self._by_key.values()
            if q in p.display_name.casefold() or q in p.canonical_key.casefold()
        ]
        hits.sort(key=lambda p: not p.is_estimate)  # оценки первыми — их и правят чаще
        return hits[:limit]

    async def update_density(self, product_id: int, density: Density) -> Product | None:
        for p in self._by_key.values():
            if p.id == product_id:
                p.density = density
                p.is_estimate = False
                p.source = "manual"
                return p
        return None

    async def rename(self, product_id: int, name: str) -> Product | None:
        for p in self._by_key.values():
            if p.id == product_id:
                p.display_name = name
                return p
        return None


class InMemoryMealRepo:
    def __init__(self) -> None:
        self._meals: list[Meal] = []
        self._seq = 0
        self._item_seq = 0

    async def add(self, meal: Meal) -> Meal:
        self._seq += 1
        meal.id = self._seq
        for item in meal.items:
            self._item_seq += 1
            item.id = self._item_seq
        self._meals.append(meal)
        return meal

    async def list_between(self, user_id: int, start: datetime, end: datetime) -> list[Meal]:
        return [m for m in self._meals if m.user_id == user_id and start <= m.eaten_at < end]

    async def frequent_items(
        self, user_id: int, *, days: int, limit: int
    ) -> list[tuple[int | None, str, float, int]]:
        from collections import defaultdict
        from datetime import timedelta

        since = datetime.now(UTC) - timedelta(days=days)
        # Ключ тот же, что в SQL: product_id, а для беспродуктовых — имя.
        buckets: dict[object, list[float]] = defaultdict(list)
        names: dict[object, str] = {}
        for m in self._meals:
            if m.user_id != user_id or m.eaten_at < since:
                continue
            for it in m.items:
                key = it.product_id if it.product_id is not None else it.name_snapshot
                buckets[key].append(it.grams)
                names[key] = it.name_snapshot
        rows = []
        for key, grams in buckets.items():
            grams.sort()
            median = grams[len(grams) // 2] if len(grams) % 2 else (
                (grams[len(grams) // 2 - 1] + grams[len(grams) // 2]) / 2
            )
            pid = key if isinstance(key, int) else None
            rows.append((pid, names[key], float(median), len(grams)))
        rows.sort(key=lambda r: (-r[3], r[1]))
        return rows[:limit]

    async def list_recent(self, user_id: int, limit: int) -> list[Meal]:
        xs = [m for m in self._meals if m.user_id == user_id]
        xs.sort(key=lambda m: m.eaten_at, reverse=True)
        return xs[:limit]

    async def delete_last(self, user_id: int) -> Meal | None:
        # Последний ПО ВСТАВКЕ = последний записанный, как created_at desc в SQL-репозитории.
        # По eaten_at нельзя: с бэкдейтом (FR-33) вчерашняя запись «моложе» сегодняшнего обеда.
        xs = [m for m in self._meals if m.user_id == user_id]
        if not xs:
            return None
        last = xs[-1]
        self._meals.remove(last)
        return last

    async def get(self, user_id: int, meal_id: int) -> Meal | None:
        return next((m for m in self._meals if m.id == meal_id and m.user_id == user_id), None)

    async def delete(self, user_id: int, meal_id: int) -> Meal | None:
        for m in self._meals:
            if m.id == meal_id and m.user_id == user_id:
                self._meals.remove(m)
                return m
        return None

    async def remove_item(self, user_id: int, meal_id: int, item_id: int) -> Meal | None:
        meal = next((m for m in self._meals if m.id == meal_id and m.user_id == user_id), None)
        if meal is None:
            return None
        item = next((i for i in meal.items if i.id == item_id), None)
        if item is None:
            return meal
        meal.items.remove(item)
        if not meal.items:
            self._meals.remove(meal)
            return None
        meal.totals = sum_macros(i.macros for i in meal.items)
        return meal

    async def update_item(
        self, user_id: int, meal_id: int, item_id: int, *, grams: float, macros: Macros
    ) -> Meal | None:
        meal = next((m for m in self._meals if m.id == meal_id and m.user_id == user_id), None)
        if meal is None:
            return None
        item = next((i for i in meal.items if i.id == item_id), None)
        if item is None:
            return meal
        item.grams = grams
        item.macros = macros
        meal.totals = sum_macros(i.macros for i in meal.items)
        return meal


class InMemoryWeightRepo:
    def __init__(self) -> None:
        self._logs: list[WeightLog] = []
        self._seq = 0

    async def add(self, log: WeightLog) -> WeightLog:
        self._seq += 1
        log.id = self._seq
        self._logs.append(log)
        return log

    async def latest(self, user_id: int, before: datetime | None = None) -> WeightLog | None:
        xs = [
            log
            for log in self._logs
            if log.user_id == user_id and (before is None or log.measured_at < before)
        ]
        return max(xs, key=lambda log: log.measured_at) if xs else None

    async def history(self, user_id: int, limit: int) -> list[WeightLog]:
        xs = [log for log in self._logs if log.user_id == user_id]
        xs.sort(key=lambda log: log.measured_at, reverse=True)
        return xs[:limit]


class InMemoryBugRepo:
    def __init__(self) -> None:
        self.reports: list[dict] = []

    async def add(
        self, *, user_id: int, note: str, context: list[dict[str, object]] | None
    ) -> None:
        self.reports.append({"user_id": user_id, "note": note, "context": context})


class StubNlu:
    """Возвращает заранее заданные ответы по порядку (последний повторяется)."""

    def __init__(self, *responses: ParsedMessage) -> None:
        self._responses = list(responses)
        self._i = 0

    async def parse(self, text: str, known_keys: object) -> ParsedMessage:
        idx = min(self._i, len(self._responses) - 1)
        self._i += 1
        return self._responses[idx]

    async def parse_samples(self, text: str, known_keys: object) -> list[ParsedMessage]:
        return [await self.parse(text, known_keys)]  # один сэмпл → поведение как до D-15


class SamplingStubNlu:
    """parse_samples отдаёт заданный список сэмплов (тест self-consistency, D-15)."""

    def __init__(self, samples: list[ParsedMessage]) -> None:
        self._samples = list(samples)

    async def parse(self, text: str, known_keys: object) -> ParsedMessage:
        return self._samples[0]

    async def parse_samples(self, text: str, known_keys: object) -> list[ParsedMessage]:
        return list(self._samples)


class StubVision:
    """read_label отдаёт заданный ParsedItem (FR-27, без vision-вызова). None = не распознано."""

    def __init__(self, item: ParsedItem | None) -> None:
        self._item = item

    async def read_label(self, image: bytes) -> ParsedItem | None:
        # копия на каждый скан — независимые «снимки» (для теста дедупа повторного скана)
        return replace(self._item) if self._item is not None else None


class StubStt:
    """Возвращает фиксированный транскрипт для голоса."""

    def __init__(self, transcript: str) -> None:
        self._transcript = transcript

    async def transcribe(self, audio: bytes, *, filename: str = "voice.ogg") -> str:
        return self._transcript


class InMemorySpendRepo:
    """Учёт расходов на LLM в памяти. Считает по месяцу так же, как SQL-версия."""

    def __init__(self, preloaded_usd: float = 0.0) -> None:
        self._rows: list[tuple[str, float]] = []
        if preloaded_usd:
            self._rows.append(("coach", preloaded_usd))

    async def add(
        self, *, user_id, model, purpose, input_tokens, output_tokens, cost_usd
    ) -> None:
        self._rows.append((purpose, cost_usd))

    async def spent_this_month(self, *, purpose: str | None = None) -> float:
        return sum(c for p, c in self._rows if purpose is None or p == purpose)
