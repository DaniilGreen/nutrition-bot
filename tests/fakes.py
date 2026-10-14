"""In-memory реализации портов + StubNlu/StubStt для тестов без БД и без LLM."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

from app.application.dto import ParsedItem, ParsedMessage, PlateReading
from app.application.entities import Meal, Product, Recipe, RecipeItem, User, WeightLog
from app.domain.goals import Goal
from app.domain.value_objects import Density, Macros, sum_macros


class InMemoryUserRepo:
    def __init__(self) -> None:
        self._users: dict[int, User] = {}
        self._goals: dict[int, Goal] = {}
        self.history: dict[int, list] = {}
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

    async def set_goal(self, user_id: int, goal: Goal, source: str = "manual") -> None:
        self._goals[user_id] = goal
        self.history.setdefault(user_id, []).append(
            (datetime.now(UTC), goal.daily_kcal, source)
        )

    async def goal_history(self, user_id: int) -> list[tuple[datetime, float]]:
        return [(t, k) for t, k, _ in self.history.get(user_id, [])]

    async def set_timezone(self, user_id: int, tzname: str) -> None:
        if user_id in self._users:
            self._users[user_id].timezone = tzname

    async def set_onboarded(self, user_id: int) -> None:
        if user_id in self._users and self._users[user_id].onboarded_at is None:
            self._users[user_id].onboarded_at = datetime(2026, 1, 1)  # сентинел «онбординнут»

    async def set_prefs(self, user_id: int, prefs: dict) -> None:
        if user_id in self._users:
            self._users[user_id].ui_prefs = prefs

    async def mark_scan_nudged(self, user_id: int, at: datetime) -> None:
        if user_id in self._users:
            self._users[user_id].scan_nudged_at = at

    async def list_active(self) -> list[User]:
        return list(self._users.values())


class InMemoryProductRepo:
    def __init__(self) -> None:
        self._recipes: dict[int, Recipe] = {}  # FR-39.7
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
        self, *, offset: int, limit: int, query: str | None = None, source: str | None = None
    ) -> tuple[list[Product], int]:
        xs = list(self._by_key.values())
        if source:
            xs = [p for p in xs if p.source == source]
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
        for rid, r in self._recipes.items():  # FR-39.7: состав ссылается на выжившего,
            for it in r.items:                   # кроме состава самого выжившего (SET NULL)
                if it.product_id in losers:
                    it.product_id = None if rid == winner_id else winner_id
        for i in losers:
            self._recipes.pop(i, None)  # ON DELETE CASCADE
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

    async def update_density(
        self, product_id: int, density: Density, *,
        is_estimate: bool = False, unit_weight_g: float | None = None, source: str = "manual",
    ) -> Product | None:
        for p in self._by_key.values():
            if p.id == product_id:
                p.density = density
                p.is_estimate = is_estimate
                p.source = source
                if unit_weight_g is not None:
                    p.unit_weight_g = unit_weight_g
                return p
        return None

    async def get_recipe(self, product_id: int) -> Recipe | None:
        r = self._recipes.get(product_id)
        if r is None:
            return None
        return Recipe(items=[RecipeItem(**vars(it)) for it in r.items], yield_g=r.yield_g)

    async def set_recipe(self, product_id: int, recipe: Recipe) -> None:
        self._recipes[product_id] = Recipe(
            items=[RecipeItem(**vars(it)) for it in recipe.items], yield_g=recipe.yield_g
        )

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

    async def contributions(
        self, user_id: int, *, days: int, limit: int, by: str = "kcal"
    ) -> list[tuple[int | None, str, float, int]]:
        from collections import defaultdict
        from datetime import timedelta

        since = datetime.now(UTC) - timedelta(days=days)
        kcal: dict[object, float] = defaultdict(float)
        times: dict[object, int] = defaultdict(int)
        names: dict[object, str] = {}
        for m in self._meals:
            if m.user_id != user_id or m.eaten_at < since:
                continue
            for it in m.items:
                key = it.product_id if it.product_id is not None else it.name_snapshot
                kcal[key] += it.macros.protein if by == "protein" else it.macros.kcal
                times[key] += 1
                names[key] = it.name_snapshot
        rows = [
            (k if isinstance(k, int) else None, names[k], kcal[k], times[k]) for k in kcal
        ]
        rows.sort(key=lambda r: -r[2])
        return rows[:limit]

    async def product_usage(
        self, user_id: int, *, by: str, query: str | None, offset: int, limit: int
    ) -> tuple[list[tuple[int, int]], int]:
        last: dict[int, datetime] = {}
        uses: dict[int, int] = {}
        for m in self._meals:
            if m.user_id != user_id:
                continue
            for it in m.items:
                if it.product_id is None:
                    continue
                if query and query.lower() not in it.name_snapshot.lower():
                    continue
                uses[it.product_id] = uses.get(it.product_id, 0) + 1
                last[it.product_id] = max(last.get(it.product_id, m.eaten_at), m.eaten_at)
        if by == "recent":
            ids = sorted(uses, key=lambda p: last[p], reverse=True)
        else:
            ids = sorted(uses, key=lambda p: (-uses[p], -last[p].timestamp()))
        page = ids[offset:offset + limit]
        return [(p, uses[p]) for p in page], len(ids)

    async def stated_usage(self, user_id: int, *, query: str | None):
        from app.application.entities import StatedUse

        seen: dict[str, StatedUse] = {}
        for m in sorted(self._meals, key=lambda m: m.eaten_at, reverse=True):
            if m.user_id != user_id:
                continue
            for it in m.items:
                if it.product_id is not None:
                    continue
                if query and query.lower() not in it.name_snapshot.lower():
                    continue
                key = it.name_snapshot.strip().lower()
                if key in seen:
                    seen[key].uses += 1
                else:
                    seen[key] = StatedUse(it.name_snapshot, m.eaten_at, 1, m.id, it.id,
                                          it.grams, it.macros)
        return list(seen.values())

    async def last_use(
        self, user_id: int, product_ids: list[int]
    ) -> dict[int, tuple[datetime, float]]:
        out: dict[int, tuple[datetime, float]] = {}
        for m in sorted(self._meals, key=lambda m: m.eaten_at, reverse=True):
            if m.user_id != user_id:
                continue
            for it in m.items:
                if it.product_id in product_ids:
                    out.setdefault(it.product_id, (m.eaten_at, it.grams))
        return out

    async def items_of_product(
        self, user_id: int, product_id: int, *, limit: int
    ) -> list[tuple[datetime, float, float]]:
        rows = [
            (m.eaten_at, it.grams, it.macros.kcal)
            for m in self._meals
            if m.user_id == user_id
            for it in m.items
            if it.product_id == product_id
        ]
        rows.sort(key=lambda r: r[0], reverse=True)
        return rows[:limit]

    async def estimate_share(self, user_id: int, *, days: int) -> tuple[float, float]:
        from datetime import timedelta

        since = datetime.now(UTC) - timedelta(days=days)
        est = total = 0.0
        for m in self._meals:
            if m.user_id != user_id or m.eaten_at < since:
                continue
            for it in m.items:
                total += it.macros.kcal
                if it.is_estimate:
                    est += it.macros.kcal
        return est, total

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

    async def hourly_kcal(
        self, user_id: int, *, days: int, tz: str, day_start: int = 0
    ) -> list[tuple[str, int, float]]:
        from collections import defaultdict
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        since = datetime.now(UTC) - timedelta(days=days)
        zone = ZoneInfo(tz)
        buckets: dict[tuple[str, int], float] = defaultdict(float)
        for m in self._meals:
            if m.user_id != user_id or m.eaten_at < since:
                continue
            local = m.eaten_at.astimezone(zone) - timedelta(hours=day_start)
            buckets[(local.date().isoformat(), local.hour)] += m.totals.kcal
        return sorted((d, h, k) for (d, h), k in buckets.items())

    async def evening_items(
        self, user_id: int, *, days: int, limit: int, tz: str, since_hour: int
    ) -> list[tuple[int | None, str, float, int]]:
        from collections import defaultdict
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        since = datetime.now(UTC) - timedelta(days=days)
        zone = ZoneInfo(tz)
        buckets: dict[int, list[float]] = defaultdict(list)
        names: dict[int, str] = {}
        for m in self._meals:
            if m.user_id != user_id or m.eaten_at < since:
                continue
            if m.eaten_at.astimezone(zone).hour < since_hour:
                continue
            for it in m.items:
                if it.product_id is None or it.grams <= 0:
                    continue
                buckets[it.product_id].append(it.grams)
                names[it.product_id] = it.name_snapshot
        rows = []
        for pid, grams in buckets.items():
            grams.sort()
            median = grams[len(grams) // 2] if len(grams) % 2 else (
                (grams[len(grams) // 2 - 1] + grams[len(grams) // 2]) / 2
            )
            rows.append((pid, names[pid], float(median), len(grams)))
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

    async def set_template(self, user_id: int, meal_id: int, name: str) -> Meal | None:
        meal = await self.get(user_id, meal_id)
        if meal is not None:
            meal.template_name = name
        return meal

    async def latest_by_template(self, user_id: int, name: str) -> Meal | None:
        hits = [
            m for m in self._meals
            if m.user_id == user_id and (m.template_name or "").lower() == name.lower()
        ]
        return max(hits, key=lambda m: m.eaten_at) if hits else None

    async def template_names(self, user_id: int) -> list[str]:
        return sorted({
            m.template_name for m in self._meals
            if m.user_id == user_id and m.template_name
        })

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


class InMemoryBodyScanRepo:
    def __init__(self) -> None:
        self.scans: list = []
        self._seq = 0

    async def get_at(self, user_id, measured_at):
        return next((x for x in self.scans
                     if x.user_id == user_id and x.measured_at == measured_at), None)

    async def delete(self, user_id, scan_id):
        before = len(self.scans)
        self.scans = [x for x in self.scans if not (x.id == scan_id and x.user_id == user_id)]
        return len(self.scans) < before

    async def upsert(self, scan, corrected=None):
        from dataclasses import replace as _replace

        self.corrected = list(corrected or [])

        for i, x in enumerate(self.scans):
            if x.user_id == scan.user_id and x.measured_at == scan.measured_at:
                self.scans[i] = _replace(scan, id=x.id)
                return self.scans[i], True
        self._seq += 1
        saved = _replace(scan, id=self._seq)
        self.scans.append(saved)
        return saved, False

    async def list_for(self, user_id):
        return sorted((x for x in self.scans if x.user_id == user_id),
                      key=lambda x: x.measured_at)


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

    async def delete(self, user_id: int, log_id: int) -> WeightLog | None:
        for i, w in enumerate(self._logs):
            if w.id == log_id and w.user_id == user_id:
                return self._logs.pop(i)
        return None

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

    async def parse_samples(self, text: str, known_keys: object, *, force: bool = False
                            ) -> list[ParsedMessage]:
        return [await self.parse(text, known_keys)]  # один сэмпл → поведение как до D-15


class SamplingStubNlu:
    """parse_samples отдаёт заданный список сэмплов (тест self-consistency, D-15)."""

    def __init__(self, samples: list[ParsedMessage]) -> None:
        self._samples = list(samples)
        self.forced = False            # просил ли сервис сэмплы принудительно (INV-12)

    async def parse(self, text: str, known_keys: object) -> ParsedMessage:
        return self._samples[0]

    async def parse_samples(self, text: str, known_keys: object, *, force: bool = False
                            ) -> list[ParsedMessage]:
        self.forced = self.forced or force
        return list(self._samples)


class StubVision:
    """read_label отдаёт заданный ParsedItem (FR-27, без vision-вызова). None = не распознано.

    read_photo (FR-34): без `plate` снимок считается этикеткой — так старые тесты
    этикетки проходят и через общий вход фото."""

    def __init__(self, item: ParsedItem | None, plate: PlateReading | None = None,
                 scan: object | None = None, scans: list | None = None) -> None:
        self._item = item
        self._plate = plate
        # `scans` — очередь чтений бланка: второе отдаётся при перечитывании
        # несошедшегося (§6.24). Последнее повторяется, если запросов больше.
        self._scans = list(scans) if scans else None
        self._scan = scan if scans is None else scans[0]
        self.photo_calls = 0
        self.scan_calls = 0
        self.seen_caption: str | None = None

    async def read_photo(self, image: bytes, caption: str = "") -> PlateReading:
        self.photo_calls += 1
        self.seen_caption = caption
        if self._scan is not None:
            return PlateReading(kind="body_scan")
        if self._plate is None:
            return PlateReading(kind="label")
        return deepcopy(self._plate)

    async def read_scan(self, image: bytes):
        self.scan_calls += 1
        if self._scans:
            return deepcopy(self._scans.pop(0) if len(self._scans) > 1 else self._scans[0])
        return deepcopy(self._scan)

    async def read_label(self, image: bytes) -> ParsedItem | None:
        # копия на каждый скан — независимые «снимки» (для теста дедупа повторного скана)
        return replace(self._item) if self._item is not None else None


class StubStt:
    """Возвращает фиксированный транскрипт для голоса."""

    def __init__(self, transcript: str) -> None:
        self._transcript = transcript
        # Имя файла запоминаем: Whisper выбирает парсер по расширению, и
        # неверное имя ломает распознавание при успешной записи.
        self.seen_filename: str | None = None

    async def transcribe(self, audio: bytes, *, filename: str = "voice.ogg") -> str:
        self.seen_filename = filename
        return self._transcript


class InMemorySpendRepo:
    """Учёт расходов на LLM в памяти. Считает по месяцу так же, как SQL-версия."""

    def __init__(self, preloaded_usd: float = 0.0) -> None:
        self._rows: list[tuple[str, float]] = []
        self.limit: float | None = None
        if preloaded_usd:
            self._rows.append(("coach", preloaded_usd))

    async def month_limit(self, default: float) -> float:
        return default if self.limit is None else self.limit

    async def set_month_limit(self, usd: float) -> None:
        self.limit = usd

    async def add(
        self, *, user_id, model, purpose, input_tokens, output_tokens, cost_usd
    ) -> None:
        self._rows.append((purpose, cost_usd))

    async def spent_this_month(self, *, purpose: str | None = None) -> float:
        return sum(c for p, c in self._rows if purpose is None or p == purpose)
