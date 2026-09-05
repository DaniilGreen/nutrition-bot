"""Фасад приложения: разобрать сообщение и направить в нужный use-case.

Презентационный слой (бот/веб) вызывает Assistant и получает Result, который
форматирует. Это делает маршрутизацию тестируемой (e2e на фейках).
"""
from __future__ import annotations

from datetime import date, datetime
from statistics import median
from zoneinfo import ZoneInfo

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.entities import Meal, MealItem, Product, User, WeightLog
from app.application.meal_service import MealService
from app.application.ports import BugRepo, NluPort, ProductRepo, SttPort, VisionPort
from app.application.results import (
    ClarificationNeeded,
    CorrectionFailed,
    DailyStats,
    DayPace,
    DayView,
    History,
    ItemAdjusted,
    ItemRemoved,
    LabelRead,
    MealDeleted,
    MealLogged,
    MonthReport,
    Result,
    Unknown,
    VariantClarificationNeeded,
    WeeklyStats,
    WeightLogged,
    WeightTrend,
)
from app.application.stats_service import StatsService, _require_id
from app.application.weight_service import WeightService
from app.domain.consensus import confidence_from_cv, median_cv
from app.domain.duplicates import Candidate, group
from app.domain.goals import Goal, Remaining, in_goal, remaining
from app.domain.nutrition import atwater_suspect
from app.domain.plausibility import clamp_density, is_alcoholic
from app.domain.portion_answer import parse as parse_portion
from app.domain.reconcile import Reconciliation, reconcile
from app.domain.value_objects import Density, Macros, sum_macros


class Assistant:
    def __init__(
        self,
        nlu: NluPort,
        products: ProductRepo,
        meal_service: MealService,
        weight_service: WeightService,
        stats_service: StatsService,
        stt: SttPort | None = None,
        bug_repo: BugRepo | None = None,
        vision: VisionPort | None = None,
    ) -> None:
        self._nlu = nlu
        self._products = products
        self._meals = meal_service
        self._weights = weight_service
        self._stats = stats_service
        self._stt = stt
        self._bug = bug_repo
        self._vision = vision

    async def report_bug(self, user: User, note: str) -> None:
        """FR-26: сохранить багрепорт + контекст последних приёмов юзера (для дебага)."""
        if self._bug is None or user.id is None:
            return
        recent = await self._meals.recent(user, 10)
        context = [
            {
                "raw_input": m.raw_input,
                "input_type": m.input_type,
                "eaten_at": m.eaten_at.isoformat(),
                "kcal": m.totals.kcal,
                "items": [it.name_snapshot for it in m.items],
            }
            for m in recent
        ]
        await self._bug.add(user_id=user.id, note=note.strip()[:2000], context=context)

    async def process(
        self, user: User, text: str, *, input_type: str = "text", day_offset: int = 0
    ) -> Result:
        """`day_offset` — день, на который смотрит человек, когда пишет (0 = сегодня,
        −1 = вчера). Это ДЕФОЛТ: слово «вчера» в самой фразе его перебивает."""
        known_keys = await self._products.all_keys()
        # Self-consistency (D-15/INV-12): k сэмплов оценки → медиана + калиброванная уверенность.
        # Заменяет фейковый ретрай при temperature=0 (был почти no-op). Адаптер сам решает k:
        # для не-meal/стейтед — один сэмпл. Фейки возвращают один сэмпл → поведение тестов цело.
        samples = await self._nlu.parse_samples(text, known_keys=known_keys)
        parsed = _consolidate(samples)

        if parsed.intent is Intent.MEAL and parsed.items:
            return await self._handle_meal(
                user, parsed, raw_input=text, input_type=input_type, day_offset=day_offset
            )

        if parsed.intent is Intent.WEIGHT and parsed.weight_kg is not None:
            # Проверка диапазона живёт в WeightService (одна на бота и аппку).
            if not self._weights.plausible(parsed.weight_kg):
                return Unknown(reason="вес вне разумных пределов (20–400 кг)")
            log, delta = await self._weights.log(user, parsed.weight_kg)
            return WeightLogged(log=log, delta_kg=delta)

        if parsed.intent is Intent.QUERY:
            return await self._stats.daily(user)

        if parsed.intent is Intent.CORRECTION:
            return await self._handle_correction(user, parsed)

        return Unknown(reason="не распознал приём пищи или вес")

    async def transcribe(self, audio: bytes, *, filename: str = "voice.ogg") -> str | None:
        """Голос → текст (M3). None, если STT не сконфигурирован (нет OpenAI-ключа)."""
        if self._stt is None:
            return None
        return await self._stt.transcribe(audio, filename=filename)

    async def read_label(
        self, user: User, image: bytes, *, photo_file_id: str | None = None
    ) -> Result:
        """FR-27: фото этикетки → распознать КБЖУ/100г → показать и ждать вес порции (текстом)."""
        if self._vision is None:
            return Unknown(reason="распознавание этикеток недоступно (нет vision)")
        item = await self._vision.read_label(image)
        if item is None:
            return Unknown(reason="не разобрал этикетку — сфоткай таблицу КБЖУ ближе и чётче")
        item.from_label = True
        item.label_photo_id = photo_file_id
        warn = not is_alcoholic(item.name) and atwater_suspect(
            item.kcal_per_100g, item.protein_per_100g, item.fat_per_100g, item.carb_per_100g
        )
        draft = ParsedMessage(intent=Intent.MEAL, items=[item], confidence=1.0)
        return LabelRead(draft=draft, item_index=0, atwater_warning=warn)

    async def answer_portion(
        self, user: User, draft: ParsedMessage, item_index: int, answer: str, *,
        raw_input: str, day_offset: int = 0,
    ) -> Result:
        """Ответ на «какая порция?»: размер словом или вес числом.

        Отдельно от `resolve_clarification` (D-19): там ответ ЗАМЕНЯЕТ имя и
        плотность переоценивается, здесь ответ задаёт только вес. Перепутать
        значит записать «300 г» как название продукта и потерять приём.
        """
        parsed = parse_portion(answer)
        if not parsed.understood:
            return Unknown(
                reason="Ответь размером (маленькая / средняя / большая) или граммами, напр. «250»."
            )
        item = draft.items[item_index]
        if parsed.size is not None:
            item.unit = Unit.PORTION
            item.portion_hint = parsed.size
        else:
            item.unit = Unit.GRAM
            item.grams = parsed.grams
            item.portion_hint = None
        return await self._handle_meal(
            user, draft, raw_input=raw_input, input_type="text", day_offset=day_offset
        )

    async def continue_meal(
        self, user: User, draft: ParsedMessage, *, raw_input: str, input_type: str = "text",
        day_offset: int = 0,
    ) -> Result:
        """Продолжить запись приёма пищи после ответа на уточняющий вопрос."""
        return await self._handle_meal(
            user, draft, raw_input=raw_input, input_type=input_type, day_offset=day_offset
        )

    async def resolve_clarification(
        self, user: User, draft: ParsedMessage, item_index: int, new_name: str, *,
        raw_input: str, day_offset: int = 0,
    ) -> Result:
        """D-19: ответ на уточнение → ставим уточнённое имя, ПЕРЕоцениваем плотность и продолжаем.

        new_name — полное имя: кнопка передаёт вариант целиком, текст — имя+дописку (склеивает
        хендлер). Одно clarify-уточнение на позицию (+ размер порции = ≤2 вопроса)."""
        item = draft.items[item_index]
        item.name = new_name.strip() or item.name
        item.clarify_options = None  # consumed — эту позицию больше не переспрашиваем
        known_keys = await self._products.all_keys()
        reparsed = await self._nlu.parse(item.name, known_keys=known_keys)
        if reparsed.items:  # свежая плотность под уточнённое имя; вес/штуки оставляем как были
            ni = reparsed.items[0]
            item.canonical_key = ni.canonical_key
            item.kcal_per_100g = ni.kcal_per_100g
            item.protein_per_100g = ni.protein_per_100g
            item.fat_per_100g = ni.fat_per_100g
            item.carb_per_100g = ni.carb_per_100g
        return await self._handle_meal(
            user, draft, raw_input=raw_input, input_type="text", day_offset=day_offset
        )

    async def today(self, user: User) -> DailyStats:
        return await self._stats.daily(user)

    async def weekly(self, user: User) -> WeeklyStats:
        return await self._stats.weekly(user)

    async def current_goal(self, user: User) -> Goal:
        return await self._stats.current_goal(user)

    async def history(self, user: User, limit: int = 10) -> History:
        meals = await self._meals.recent(user, limit)
        return History(meals=meals, timezone=user.timezone)

    async def today_meals(self, user: User) -> History:
        """Все приёмы за сегодня (пикер отмены показывает весь день, не последние 5; баг #12)."""
        return History(meals=await self._stats.day_meals(user), timezone=user.timezone)

    async def day_view(self, user: User, days_ago: int = 0) -> DayView:
        """Просмотр одного дня (вар. A): приёмы хронологически + дата, для навигации ← / →."""
        meals, day = await self._stats.day_view(user, days_ago)
        return DayView(meals=meals, timezone=user.timezone, days_ago=days_ago, day=day)

    async def day_calories(self, user: User, n: int) -> tuple[list[tuple[date, float]], Goal]:
        """(дни с ккал, цель) за n дней — для графика недели и хитмапа (картинки)."""
        return await self._stats.last_days(user, n), await self._stats.current_goal(user)

    async def day_card(self, user: User, days_ago: int = 0) -> tuple[Remaining, DayView]:
        """День для карточки-картинки (История): остаток дня + приёмы (для подписи)."""
        dv = await self.day_view(user, days_ago)
        consumed = sum_macros(m.totals for m in dv.meals)
        goal = await self._stats.current_goal(user)
        return remaining(consumed, goal), dv

    async def month_report(self, user: User, days: int = 30) -> MonthReport:
        """Отчёт за период для картинки /month (FR-29): средние/день, дни в цели, Δ веса."""
        # ponytail: окно читается из БД дважды (last_days по дням + period целиком) — при 2 юзерах
        # и редком /month дешевле, чем тащить per-day макросы; объединить, если станет горячо.
        perday, goal = await self.day_calories(user, days)
        consumed = await self._stats.period(user, days)
        logged = [k for _, k in perday if k > 0]
        n = max(1, len(logged))
        avg = Macros(consumed.kcal / n, consumed.protein / n, consumed.fat / n, consumed.carb / n)
        goal_days = sum(1 for k in logged if in_goal(k, goal.daily_kcal))  # коридор как в /streak
        # Δ веса — по замерам ВНУТРИ окна периода (а не по числу записей): при редком
        # взвешивании limit=days охватил бы больше дней, чем заявлено в подписи «вес за период».
        tz = ZoneInfo(user.timezone)
        start = perday[0][0] if perday else None
        logs = (await self.weight_trend(user, limit=days * 4)).logs  # ponytail: до ~4 замеров/день
        win = [lg for lg in logs if start and lg.measured_at.astimezone(tz).date() >= start]
        wd = round(win[0].weight_kg - win[-1].weight_kg, 1) if len(win) >= 2 else None
        return MonthReport(days, len(logged), goal_days, avg, wd)

    async def weight_trend(self, user: User, limit: int = 14) -> WeightTrend:
        logs = await self._weights.history(user, limit)
        logs = [log for log in logs if 20 <= log.weight_kg <= 400]  # отсечь мусорные значения
        return WeightTrend(logs=logs, timezone=user.timezone)

    async def log_weight(self, user: User, weight_kg: float) -> tuple[WeightLog, float | None]:
        return await self._weights.log(user, weight_kg)

    async def undo(self, user: User) -> MealDeleted:
        return MealDeleted(meal=await self._meals.delete_last(user))

    async def delete_meal(self, user: User, meal_id: int) -> MealDeleted:
        return MealDeleted(meal=await self._meals.delete(user, meal_id))

    async def repeat_meal(
        self, user: User, meal_id: int, *, day_offset: int = 0
    ) -> MealLogged | None:
        """Повторить приём кнопкой. None — если запись уже удалили (кнопка живёт в чате вечно)."""
        meal = await self._meals.repeat(user, meal_id, day_offset=day_offset)
        if meal is None:
            return None
        stats = await self._stats.daily(user, days_ago=-day_offset)
        return MealLogged(meal=meal, remaining=stats.remaining)

    async def remove_item(self, user: User, meal_id: int, item_id: int) -> MealLogged | None:
        """Удалить одну позицию записи. None — если позиций не осталось (запись удалена)."""
        meal = await self._meals.remove_item(user, meal_id, item_id)
        if meal is None:
            return None
        stats = await self._stats.daily(user)
        return MealLogged(meal=meal, remaining=stats.remaining)

    # --- каталог продуктов (правка кривых AI-оценок) ---

    async def fixable_products(self, limit: int = 10, query: str | None = None) -> list[Product]:
        # Без запроса — 10 последних оценок («поправить только что записанное»). С запросом —
        # поиск по всему каталогу: иначе всё старше десятки недостижимо для правки (FR-15.1).
        if query:
            return await self._products.search(query, limit)
        return await self._products.list_estimated(limit)

    async def get_product(self, product_id: int) -> Product | None:
        return await self._products.get_by_id(product_id)

    async def rename_product(self, product_id: int, name: str) -> Product | None:
        """FR-27 #6: переименовать продукт (обобщённое имя с этикетки → конкретное)."""
        clean = name.strip()[:255]
        return await self._products.rename(product_id, clean) if clean else None

    async def daily_series(self, user: User, days: int = 30):
        """Ряд по дням для графиков: (дата, КБЖУ, число приёмов) + цель."""
        return await self._stats.last_days_macros(user, days), await self._stats.current_goal(user)

    async def frequent(
        self, user: User, *, days: int = 30, limit: int = 10
    ) -> list[tuple[int | None, str, float, int]]:
        """Чаще всего съеденное — для чипов быстрой записи (§6.3)."""
        return await self._meals.frequent_items(_require_id(user), days=days, limit=limit)

    async def day_pace(self, user: User) -> DayPace:
        """§6.20: темп сегодняшнего дня против личной нормы к этому часу."""
        return await self._stats.day_pace(user)

    async def evening_frequent(
        self, user: User, *, since_hour: int, days: int = 30, limit: int = 4
    ) -> list[tuple[int | None, str, float, int]]:
        """Типичная еда после указанного часа — чипы карточки добора (§6.20)."""
        return await self._meals.evening_items(
            _require_id(user), days=days, limit=limit,
            tz=user.timezone, since_hour=since_hour,
        )

    async def catalog_page(
        self, user: User, *, offset: int = 0, limit: int = 20, query: str | None = None
    ) -> tuple[list[Product], int, dict[int, int], dict[int, float]]:
        """Страница каталога: продукты, всего, сколько раз ел, привычный вес."""
        items, total = await self._products.list_page(offset=offset, limit=limit, query=query)
        uses = await self._products.usage_counts(
            [p.id for p in items if p.id], user_id=_require_id(user)
        )
        # Привычная граммовка — из истории самого человека: без неё тап по
        # строке каталога не знает, сколько записывать (MA-INV-24).
        rows = await self._meals.frequent_items(_require_id(user), days=180, limit=200)
        grams = {pid: g for pid, _, g, _ in rows if pid is not None}
        return items, total, uses, grams

    async def usage_of(self, user: User, product_id: int) -> int:
        """Сколько раз ЭТОТ человек ел продукт. Карточке нужно одно число."""
        counts = await self._products.usage_counts([product_id], user_id=_require_id(user))
        return counts.get(product_id, 0)

    async def adjust_item(
        self, user: User, meal_id: int, item_id: int, new_grams: float
    ) -> tuple[MealItem, float] | None:
        """Правка веса позиции из ленты."""
        return await self._meals.adjust_item(user, meal_id, item_id, new_grams)

    async def delete_weight(self, user: User, log_id: int) -> WeightLog | None:
        return await self._weights.delete(user, log_id)

    async def contributions(
        self, user: User, *, days: int = 30, limit: int = 8, by: str = "kcal"
    ) -> list[tuple[int | None, str, float, int]]:
        """Вклад продуктов (§3.3, §6.10). Абсолютная сумма, не доля.

        `by="protein"` отвечает на «откуда приходит белок» — вопрос важнее
        калорийного, потому что белок и есть главный разрыв дневника (§4.0).
        """
        return await self._meals.contributions(
            _require_id(user), days=days, limit=limit, by=by
        )

    async def product_history(
        self, user: User, product_id: int, *, limit: int = 5
    ) -> list[tuple[datetime, float, float]]:
        """Последние появления продукта в приёмах ЭТОГО человека (§6.8)."""
        return await self._meals.items_of_product(
            _require_id(user), product_id, limit=limit
        )

    async def duplicate_index(self) -> dict[int, int]:
        """product_id → номер группы дублей. Нужен графику вкладов: один
        продукт двумя строками там виден сразу, и это лучший момент предложить
        слияние — человек как раз смотрит, куда уходят калории (§3.3)."""
        groups, _ = await self.duplicate_groups()
        return {
            p.id: gi for gi, g in enumerate(groups) for p in g if p.id is not None
        }

    async def accuracy(self, user: User, *, days: int = 30) -> tuple[float, float]:
        """Доверие к цифре: сколько калорий из оценок, сколько всего (§3.5)."""
        return await self._meals.estimate_share(_require_id(user), days=days)

    async def reconcile_weight(self, user: User, *, days: int = 30) -> Reconciliation:
        """Сходится ли дневник с весами (§4.0). Флагманская панель.

        Ничего не обвиняет: кладёт рядом два факта и говорит, сходятся ли они.
        """
        rows = await self._stats.last_days_macros(user, days)
        goal = await self._stats.current_goal(user)
        trend = await self._weights.history(user, 100)
        window = [
            log
            for log in trend
            if log.measured_at.date() >= rows[0][0] and 20 <= log.weight_kg <= 400
        ] if rows else []
        window.sort(key=lambda log: log.measured_at)
        return reconcile(
            days=days,
            logged_kcal_total=sum(m.kcal for _, m, _ in rows),
            days_with_records=sum(1 for _, _, k in rows if k > 0),
            weight_start_kg=window[0].weight_kg if len(window) >= 2 else None,
            weight_end_kg=window[-1].weight_kg if len(window) >= 2 else None,
            maintenance_kcal=goal.daily_kcal,
        )

    async def log_quick(
        self, user: User, product_id: int, grams: float, *, day_offset: int = 0
    ) -> Result:
        """Запись тапом по чипу: продукт известен, вес известен, модель не нужна."""
        product = await self._products.get_by_id(product_id)
        if product is None:
            return Unknown(reason="Такого продукта больше нет в каталоге.")
        meal = await self._meals.log_known(
            user, product, grams, raw_input=f"{product.display_name} {grams:g} г",
            day_offset=day_offset,
        )
        stats = await self._stats.daily(user, days_ago=-day_offset)
        return MealLogged(meal=meal, remaining=stats.remaining)

    async def get_meal(self, user: User, meal_id: int) -> Meal | None:
        """Приём по идентификатору. Нужен, чтобы отличить «позиции нет» от
        «позиция была последней и приём удалён» — это разные ответы."""
        return await self._meals.get(user, meal_id)

    async def duplicate_groups(self) -> tuple[list[list[Product]], dict[int, int]]:
        """Группы дублей каталога. Не сливает — только показывает: разброс плотности
        внутри группы может быть таким, что слияние заметно двинет цифры (D-32)."""
        items = await self._products.list_all()
        # Здесь без user_id намеренно: выжившего выбирает общий вес записи
        # в каталоге, а не частота у одного из двоих.
        uses = await self._products.usage_counts([p.id for p in items if p.id])
        rows = [
            Candidate(
                id=p.id, canonical_key=p.canonical_key, display_name=p.display_name,
                source=p.source, kcal_per_100g=p.density.kcal_per_100g,
                uses=uses.get(p.id, 0),
            )
            for p in items
            if p.id is not None
        ]
        by_id = {p.id: p for p in items}
        return [[by_id[c.id] for c in g] for g in group(rows)], uses

    async def merge_products(self, winner_id: int, loser_ids: list[int]) -> int:
        """Слить дубли. История не движется: перепривязывается только связь (INV-3)."""
        return await self._products.merge(winner_id, loser_ids)

    async def fix_product_density(self, product_id: int, density: Density) -> Product | None:
        # INV-11.2: ручную правку каталога тоже отсекаем по границам
        # (защита от опечатки «310 г белка»)
        return await self._products.update_density(product_id, clamp_density(density))

    async def _handle_meal(
        self, user: User, parsed: ParsedMessage, *, raw_input: str, input_type: str,
        day_offset: int = 0,
    ) -> Result:
        # D-19: сперва уточняем СОСТАВ (начинка/тип/жирность) — влияет и на плотность, и на размер.
        for i, item in enumerate(parsed.items):
            if item.clarify_options and not item.total_kcal:
                options = await self._meals.order_clarify_options(user, item.clarify_options)
                return VariantClarificationNeeded(
                    draft=parsed, item_index=i, item_name=item.name, options=options
                )

        idx = _first_unclarified_index(parsed)  # затем размер порции (FR-21)
        if idx is not None:
            return ClarificationNeeded(
                draft=parsed, item_index=idx, item_name=parsed.items[idx].name
            )

        meal = await self._meals.log(
            user, parsed, raw_input=raw_input, input_type=input_type, day_offset=day_offset
        )
        # Остаток ТОГО дня, куда записали (при «вчера/завтра» — не сегодняшнего). offset −1 (вчера)
        # → days_ago +1; +1 (завтра) → days_ago −1. Правило приоритета то же, что в MealService:
        # слово в тексте старше открытого экрана — иначе остаток показали бы от чужого дня.
        stats = await self._stats.daily(user, days_ago=-(parsed.day_offset or day_offset))
        return MealLogged(meal=meal, remaining=stats.remaining)

    async def _handle_correction(self, user: User, parsed: ParsedMessage) -> Result:
        """FR-14: «удали X» (delete) и «поправь граммы у X» (adjust). Замена (replace) — отказ."""
        op = parsed.correction_op or "delete"
        if op == "adjust":
            return await self._handle_adjust(user, parsed)
        if op != "delete":
            return CorrectionFailed(target="", reason="unsupported")
        targets = [it.name for it in parsed.items if it.name.strip()]
        removed: list[MealItem] = []
        for name in targets:
            item = await self._meals.remove_item_by_name(user, name)
            if item is not None:
                removed.append(item)
        if not removed:
            return CorrectionFailed(target=", ".join(targets), reason="not_found")
        stats = await self._stats.daily(user)
        return ItemRemoved(items=removed, remaining=stats.remaining)

    async def _handle_adjust(self, user: User, parsed: ParsedMessage) -> Result:
        """FR-14.2 «поправь рис на 180»: items[] несут новый вес (grams) для целевых имён."""
        adjusted: list[tuple[MealItem, float]] = []
        for it in parsed.items:
            if it.name.strip() and it.grams is not None:
                res = await self._meals.adjust_item_by_name(user, it.name, it.grams)
                if res is not None:
                    adjusted.append(res)
        if not adjusted:
            targets = ", ".join(it.name for it in parsed.items if it.name.strip())
            return CorrectionFailed(target=targets, reason="not_found")
        item, old_grams = adjusted[-1]
        stats = await self._stats.daily(user)
        return ItemAdjusted(item=item, old_grams=old_grams, remaining=stats.remaining)


def _is_blank_item(i: ParsedItem) -> bool:
    """Пустая оценка позиции (0/0/0/0) — промах сэмпла, исключаем из медианы."""
    return (
        i.kcal_per_100g == 0
        and i.protein_per_100g == 0
        and i.fat_per_100g == 0
        and i.carb_per_100g == 0
    )


def _consolidate(samples: list[ParsedMessage]) -> ParsedMessage:
    """Свести k сэмплов оценки в один разбор (D-15/INV-12): медиана плотности и веса штуки +
    калиброванная уверенность из разброса (CV). Один сэмпл (фейки/не-meal) → как есть."""
    base = samples[0]
    if len(samples) < 2 or base.intent is not Intent.MEAL:
        return base
    for i, item in enumerate(base.items):
        if item.total_kcal:  # стейтед (FR-22) — не сэмплируем
            continue
        # Выравниваем по canonical_key: i-я позиция сэмпла в медиану — ТОЛЬКО если тот же продукт.
        # При temp>0 модель по-разному дробит мультипозицию (рис+курица vs «рис с курицей») — индекс
        # не гарантирует совпадение, медиана по разным продуктам = мусор (баг голос+мультиблюдо).
        key = item.canonical_key
        col = [
            s.items[i]
            for s in samples
            if i < len(s.items) and s.items[i].canonical_key == key
        ]
        if len(col) < 2:
            continue  # рассинхрон сэмплов → доверяем base (temp=0), не миксуем
        src = [c for c in col if not _is_blank_item(c)] or col  # пустые промахи отбрасываем
        kmed, dcv = median_cv([c.kcal_per_100g for c in src])
        item.kcal_per_100g = round(kmed, 1)
        item.protein_per_100g = round(median([c.protein_per_100g for c in src]), 1)
        item.fat_per_100g = round(median([c.fat_per_100g for c in src]), 1)
        item.carb_per_100g = round(median([c.carb_per_100g for c in src]), 1)
        gcv = 0.0
        weights = [c.unit_weight_g for c in src if c.unit_weight_g]
        if weights:
            wmed, gcv = median_cv(weights)
            item.unit_weight_g = round(wmed, 1)
        # уверенность — по худшему разбросу (плотность ИЛИ вес штуки)
        item.estimate_confidence = confidence_from_cv(max(dcv, gcv))
    return base


def _first_unclarified_index(parsed: ParsedMessage) -> int | None:
    """Первый продукт с порцией без размера — для него и зададим вопрос."""
    for i, item in enumerate(parsed.items):
        if item.total_kcal:
            continue  # FR-22: явные КБЖУ названы — размер/грамовка не нужны
        if item.unit is Unit.PORTION and item.portion_hint is None:
            return i
    return None
