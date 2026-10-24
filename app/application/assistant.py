"""Фасад приложения: разобрать сообщение и направить в нужный use-case.

Презентационный слой (бот/веб) вызывает Assistant и получает Result, который
форматирует. Это делает маршрутизацию тестируемой (e2e на фейках).
"""
from __future__ import annotations

import asyncio
import hashlib
import weakref
from datetime import date, datetime
from statistics import median
from zoneinfo import ZoneInfo

from app.application.body_scans import from_reading
from app.application.coach import LimitReached
from app.application.dto import (
    Intent,
    ParsedItem,
    ParsedMessage,
    PlateReading,
    ScanReading,
    SetPick,
    Unit,
)
from app.application.entities import (
    Meal,
    MealItem,
    Product,
    Recipe,
    RecipeItem,
    StatedUse,
    User,
    WeightLog,
)
from app.application.meal_service import MealService, RecipeRejected
from app.application.ports import (
    BugRepo,
    NluPort,
    ProductRepo,
    SpendRepo,
    SttPort,
    VisionPort,
)
from app.application.results import (
    BodyScanRead,
    ClarificationNeeded,
    CorrectionFailed,
    DailyStats,
    DayPace,
    DayView,
    DishSaved,
    History,
    ItemAdjusted,
    ItemRemoved,
    LabelRead,
    MealDeleted,
    MealLogged,
    MonthReport,
    PlateRead,
    Result,
    Unknown,
    VariantClarificationNeeded,
    WeeklyStats,
    WeightLogged,
    WeightTrend,
)
from app.application.stats_service import StatsService, _require_id
from app.application.weight_service import WeightService
from app.domain.body_scan import mismatches
from app.domain.clarify import worth_asking_portion
from app.domain.consensus import confidence_from_cv, median_cv
from app.domain.duplicates import Candidate, group
from app.domain.goals import Goal, Remaining, in_goal, remaining
from app.domain.menu import MenuLine
from app.domain.menu import misread as menu_misread
from app.domain.menu import picked as menu_picked
from app.domain.menu import title as menu_title
from app.domain.menu import totals as menu_totals
from app.domain.naming import looks_like_key, phrase_names
from app.domain.nutrition import atwater_suspect
from app.domain.plate import Component, caption_numbers, clean_name, said
from app.domain.plate import compose as plate_compose
from app.domain.plate import extras as plate_extras
from app.domain.plate import split as plate_split
from app.domain.plate import valid_grams as plate_valid
from app.domain.plausibility import clamp_density, is_alcoholic
from app.domain.portion_answer import parse as parse_portion
from app.domain.reconcile import Reconciliation, reconcile
from app.domain.spend import month_limit_reached
from app.domain.value_objects import Density, Macros, sum_macros
from app.domain.week import WeekBalance

# ponytail: подпись каталога → группы дублей (id). Одна запись на процесс: бот и API считают
# каждый своё. Потолок — каталог, меняющийся на каждом запросе; тогда кэш бесполезен, но не вреден.
_GROUPS_MEMO: dict[str, list[list[int]]] = {}
# Замок на цикл событий, а не один на модуль: asyncio.Lock привязывается к циклу при первой
# конкуренции, а у тестов цикл свой на каждый тест.
_GROUPS_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def _groups_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _GROUPS_LOCKS.get(loop)
    if lock is None:
        lock = _GROUPS_LOCKS[loop] = asyncio.Lock()
    return lock

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
        spend: SpendRepo | None = None,
        month_limit_usd: float = 0.0,
    ) -> None:
        self._nlu = nlu
        self._products = products
        self._meals = meal_service
        self._weights = weight_service
        self._stats = stats_service
        self._stt = stt
        self._bug = bug_repo
        self._vision = vision
        # FR-34: фото блюда — под месячным лимитом (MA-INV-45). Текстовая запись —
        # нет, поэтому лимит проверяется только в read_photo.
        self._spend = spend
        self._limit = month_limit_usd

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
        self, user: User, text: str, *, input_type: str = "text", day_offset: int = 0,
        photo: bool = False, weight_guessed: bool = False,
    ) -> Result:
        """`day_offset` — день, на который смотрит человек, когда пишет (0 = сегодня,
        −1 = вчера). Это ДЕФОЛТ: слово «вчера» в самой фразе его перебивает."""
        # FR-39.2: свой набор («курица в лаваше») — повторяем сохранённый состав, в LLM не идём.
        # Проверка ДО разбора: знакомая еда не должна стоить ни денег, ни задержки.
        if input_type == "text" and not photo:
            match = await self._meals.template_match(user, text)
            if match is not None:
                template, times = match
                repeated = await self._meals.repeat_template(
                    user, template, day_offset=day_offset, times=times
                )
                if repeated is not None:
                    stats = await self._stats.daily(user, days_ago=-day_offset)
                    return MealLogged(meal=repeated, remaining=stats.remaining)

        known_keys = await self._products.all_keys()
        # Self-consistency (D-15/INV-12): k сэмплов оценки → медиана + калиброванная уверенность.
        # Заменяет фейковый ретрай при temperature=0 (был почти no-op). Адаптер сам решает k:
        # для не-meal/стейтед — один сэмпл. Фейки возвращают один сэмпл → поведение тестов цело.
        samples = await self._nlu.parse_samples(text, known_keys=known_keys)
        if len(samples) == 1 and await self._becomes_new_product(samples[0], known_keys):
            # Ключ знакомый, а продукт чужой — запись создаст новый; одна оценка его плотности
            # пиннится навсегда (INV-2), значит нужна медиана (INV-12). Редкий случай: лишний
            # один вызов дешевле плотности наугад.
            samples = await self._nlu.parse_samples(text, known_keys=known_keys, force=True)
        parsed = _consolidate(samples)
        parsed.photo = photo
        parsed.weight_guessed = weight_guessed
        if photo:
            _names_from_phrase(parsed, text)
            # Карточка фото уже была подтверждением: второй круг «с кожей?», «с маслом?»
            # переспрашивал то, что человек только что отметил, а ответ «с маслом» при
            # отдельной позиции масла считал его дважды (фото 17.09.2026).
            for item in parsed.items:
                item.clarify_options = None

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

        if parsed.intent is Intent.BATCH and parsed.items:
            # FR-39: наготовил впрок. Пиним блюдо, приём пищи НЕ пишем — он это не ел.
            try:
                product = await self._meals.prepare_batch(parsed)
            except RecipeRejected as exc:
                return Unknown(reason="рецепт не сходится", say=str(exc))
            if product is None:
                return Unknown(reason="не понял состав заготовки")
            return DishSaved(product=product, portions=parsed.batch_portions)

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

    async def read_photo(
        self, user: User, image: bytes, *, caption: str = "", photo_file_id: str | None = None,
        day_offset: int = 0,
    ) -> Result:
        """FR-34: снимок → этикетка (FR-27) или карточка блюда.

        Раскладку по составляющим делает модель, вес — человек. Число из подписи
        принимается, только если человек его действительно написал.
        """
        if self._vision is None:
            return Unknown(reason="распознавание фото недоступно")
        await self._ensure_budget()
        reading = await self._vision.read_photo(image, caption)
        if reading.kind == "label":
            return await self.read_label(user, image, photo_file_id=photo_file_id)
        if reading.kind == "menu":
            return await self._log_menu(user, reading.menu, day_offset=day_offset)
        if reading.kind == "body_scan":
            scan = await self._vision.read_scan(image)
            if scan is None:
                return Unknown(reason="не разобрал бланк — сними его ровно и целиком, при свете")
            # Числа бланка связаны между собой (MA-INV-50): не сошлось — значит
            # что-то прочитано неверно. Перечитываем один раз и берём чтение с
            # меньшим числом расхождений — дешевле, чем сверять руками.
            # ponytail: одна попытка и «лучшее из двух»; сводить поля по согласию
            # двух чтений — если и этого будет мало.
            gaps = _scan_gaps(scan, user.timezone)
            if gaps:
                again = await self._vision.read_scan(image)
                if again is not None and len(_scan_gaps(again, user.timezone)) < len(gaps):
                    scan = again
            return BodyScanRead(reading=scan)
        # Позиция, чьё имя после очистки пусто (одни цифры и знаки), не попала бы
        # в фразу — и карточка показала бы то, что не запишется. Отсекаем здесь.
        reading.components = [c for c in reading.components if clean_name(c.name)]
        if reading.kind != "dish" or not reading.components:
            return Unknown(reason="не вижу на снимке еды — сними тарелку сверху, при свете")
        numbers = caption_numbers(caption)
        total = reading.caption_total_g if said(reading.caption_total_g, numbers) else None
        for c in reading.components:
            c.fixed = bool(c.fixed and said(c.grams, numbers))
        grams = plate_split(reading.components, total)
        weighed = total is not None or any(c.fixed for c in reading.components)
        # D-MA-20: масло при готовке и невидимая заправка — строками карточки, без вопроса.
        # Поверх веса тарелки, как было с чипами: делёж еды они не трогают; лишнее — «✖».
        for name, g in plate_extras(reading.cooking_fat, reading.fat_kind, reading.dressing):
            reading.components.append(Component(name, 0.0, g, True, "на готовку"))
            grams.append(g)
        return PlateRead(reading=reading, grams=grams, weighed=weighed, total_g=total)

    async def _log_menu(
        self, user: User, lines: list[MenuLine], *, day_offset: int = 0
    ) -> Result:
        """FR-38: скриншот меню — цифры как свои (FR-22), пишем сразу. Перепутанная цифра
        (Атуотер не сходится) — не пишем: молча записать неверное хуже, чем переспросить."""
        lines = menu_picked(lines)
        if not lines:
            return Unknown(reason="не нашёл на скриншоте калорий — пришли цифры текстом")
        bad = [ln.name for ln in lines if menu_misread(ln)]
        if bad:
            return Unknown(
                reason="цифры на скриншоте не сходятся (" + ", ".join(bad) + ") — "
                "пришли ккал и БЖУ текстом, так надёжнее"
            )
        items = []
        for ln in lines:
            kcal, p, f, c = menu_totals(ln)
            items.append(ParsedItem(
                name=menu_title(ln), canonical_key="", unit=Unit.PORTION,
                total_kcal=kcal, total_protein=p, total_fat=f, total_carb=c,
            ))
        draft = ParsedMessage(intent=Intent.MEAL, items=items, confidence=1.0)
        return await self.continue_meal(
            user, draft, raw_input="меню: " + ", ".join(i.name for i in items),
            input_type="menu", day_offset=day_offset,
        )

    @staticmethod
    def photo_text(reading: PlateReading, grams: list[float | None]) -> str:
        """FR-34: карточка → фраза «имя N г, …». `grams[i]` — граммы i-й
        составляющей, None — человек её убрал. Имена берём из черновика, не от
        клиента (MA-INV-43). ValueError — граммы вне границ (MA-INV-44)."""
        parts = [
            (c.name, float(g))
            for c, g in zip(reading.components, grams, strict=True)
            if g is not None
        ]
        if not parts or not plate_valid([g for _, g in parts]):
            raise ValueError("bad_plate")
        text = plate_compose(parts)
        if not text:
            raise ValueError("bad_plate")
        return text

    async def log_photo(
        self, user: User, text: str, *, weighed: bool = False, day_offset: int = 0
    ) -> Result:
        """Фраза из карточки → обычный разбор: каталог, INV-2, INV-7, сэмплы."""
        return await self.process(
            user, text, input_type="photo", day_offset=day_offset,
            photo=True, weight_guessed=not weighed,
        )

    async def _becomes_new_product(self, parsed: ParsedMessage, known_keys: list[str]) -> bool:
        """Есть ли позиция, которую адаптер счёл знакомой по ключу, а запись создаст заново.

        Только ключи из каталога: новый ключ адаптер видел сам, и если не сэмплировал — значит
        сэмплирование выключено (k = 1), и форсировать его — лишний вызов впустую."""
        if parsed.intent is not Intent.MEAL:
            return False
        known = set(known_keys)
        for item in parsed.items:
            if item.total_kcal or item.clarify_options or item.canonical_key not in known:
                continue
            if not await self._meals.resolves_to_existing(item):
                return True
        return False

    async def _ensure_budget(self) -> None:
        """MA-INV-45: проверка ДО vision-вызова — иначе лимит ничего не ограничивает.

        Потолок читается из базы при каждой проверке (MA-INV-65): владелец поднял его в
        настройках — действует со следующего вызова, без перезапуска."""
        if self._spend is None:
            return
        limit = await self._spend.month_limit(self._limit)
        if limit <= 0:
            return
        if month_limit_reached(await self._spend.spent_this_month(), limit):
            raise LimitReached

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

    async def repeat_item(
        self, user: User, meal_id: int, item_id: int, *,
        grams: float | None = None, day_offset: int = 0,
    ) -> MealLogged | None:
        """Одна позиция ещё раз — обычно с прошлого дня в сегодня (§6.25)."""
        meal = await self._meals.repeat_item(
            user, meal_id, item_id, grams=grams, day_offset=day_offset
        )
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

    async def week_balance(
        self, user: User, weeks_ago: int = 0, *, bmr_kcal: float | None = None
    ) -> WeekBalance:
        return await self._stats.week_balance(user, weeks_ago, bmr_kcal=bmr_kcal)

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
        self, user: User, *, offset: int = 0, limit: int = 20, query: str | None = None,
        sort: str = "name",
    ) -> tuple[
        list[Product | StatedUse], int, dict[int, int], dict[int, float],
        dict[int, tuple[datetime, float]],
    ]:
        """Страница каталога: продукты, всего, сколько раз ел, привычный вес, последний раз.

        `recent`/`frequent` — только то, что человек ел, по последнему разу или по числу раз
        (§6.25); `mine` — свои рецепты (FR-39.5), их видно сразу, даже несъеденные;
        `name` — весь каталог по алфавиту."""
        uid = _require_id(user)
        items: list[Product | StatedUse]
        if sort in ("recent", "frequent"):
            # Продукты и «свои цифры» — одним списком (§6.25): из каждого источника берём первые
            # offset+limit, сливаем по общему ключу и режем страницу — так страница верна.
            rows, ptotal = await self._meals.product_usage(
                uid, by=sort, query=query, offset=0, limit=offset + limit
            )
            uses = dict(rows)
            when = await self._meals.last_use(uid, list(uses))
            found = [await self._products.get_by_id(pid) for pid, _ in rows]
            stated = await self._meals.stated_usage(uid, query=query)
            merged: list[Product | StatedUse] = [p for p in found if p is not None] + stated

            def at(e: Product | StatedUse) -> datetime:
                return e.last_at if isinstance(e, StatedUse) else when[e.id or 0][0]

            def times(e: Product | StatedUse) -> int:
                return e.uses if isinstance(e, StatedUse) else uses.get(e.id or 0, 0)

            if sort == "recent":
                merged.sort(key=at, reverse=True)
            else:
                merged.sort(key=lambda e: (times(e), at(e)), reverse=True)
            items = merged[offset:offset + limit]
            total = ptotal + len(stated)
        else:
            items, total = await self._products.list_page(
                offset=offset, limit=limit, query=query,
                source="recipe" if sort == "mine" else None,
            )
            uses = await self._products.usage_counts([p.id for p in items if p.id], user_id=uid)
        # Привычная граммовка — из истории самого человека: без неё тап по
        # строке каталога не знает, сколько записывать (MA-INV-24).
        freq = await self._meals.frequent_items(uid, days=180, limit=200)
        grams = {pid: g for pid, _, g, _ in freq if pid is not None}
        last = await self._meals.last_use(
            uid, [p.id for p in items if isinstance(p, Product) and p.id]
        )
        return items, total, uses, grams, last

    async def sets(self, user: User) -> list[Meal]:
        """FR-39.5: свои наборы для «Моё»."""
        return await self._meals.sets(user)

    async def fix_item(
        self, user: User, meal_id: int, item_id: int, text: str
    ) -> MealLogged | None:
        """«Поправить словами» (§6.27): «это был булгур», «индейка 150 г» — тот же разбор и
        каталог, что у записи; позиция заменяется в том же приёме. None — не понял или нет такой."""
        known_keys = await self._products.all_keys()
        parsed = _consolidate(await self._nlu.parse_samples(text, known_keys=known_keys))
        if parsed.intent is not Intent.MEAL or not parsed.items:
            return None
        for it in parsed.items:
            it.clarify_options = None  # человек сам сказал, что это, — не переспрашиваем
        meal = await self._meals.fix_item(user, meal_id, item_id, parsed, raw_input=text)
        if meal is None:
            return None
        stats = await self._stats.daily(user)
        return MealLogged(meal=meal, remaining=stats.remaining)

    async def restore_meal(self, user: User, snapshot: Meal) -> MealLogged | None:
        """«Вернуть» (MA-INV-69). Приём ещё есть (убрали позицию) — сперва убираем его нынешний
        вид, потом кладём снимок: восстанавливается ровно то, что было до удаления."""
        if snapshot.user_id != user.id:
            return None
        if snapshot.id is not None and await self._meals.get(user, snapshot.id) is not None:
            await self._meals.delete(user, snapshot.id)
        meal = await self._meals.restore(snapshot)
        stats = await self._stats.daily(user)
        return MealLogged(meal=meal, remaining=stats.remaining)

    async def ingredients_from_text(self, text: str) -> tuple[list[RecipeItem], list[str]]:
        """FR-39.8: свой ингредиент текстом — тот же разбор, что у записи и «рецепт …», и через
        каталог. Возвращает закреплённые в каталоге позиции и имена тех, что закрепить не вышло
        (пустая оценка, INV-11.3): молча подставить фолбэк — неверная плотность навсегда."""
        known_keys = await self._products.all_keys()
        parsed = _consolidate(await self._nlu.parse_samples(text, known_keys=known_keys))
        ok: list[RecipeItem] = []
        skipped: list[str] = []
        for item in parsed.items:
            if item.total_kcal:  # «соус 120 ккал»: плотность из итога без веса не вывести
                skipped.append(item.name)
                continue
            ing = await self._meals.ingredient(item)
            (ok.append(ing) if ing.product_id is not None else skipped.append(item.name))
        return ok, skipped

    async def ingredient_from_label(
        self, image: bytes, *, photo_file_id: str | None = None
    ) -> RecipeItem | None:
        """FR-39.8: фото этикетки → продукт с этикеточной плотностью (FR-27). Вес — порция с
        этикетки, иначе пачка, иначе 100 г; правится цифрами в листе."""
        if self._vision is None:
            return None
        item = await self._vision.read_label(image)
        if item is None:
            return None
        item.from_label = True
        item.label_photo_id = photo_file_id
        item.unit = Unit.GRAM
        item.grams = item.serving_g or item.unit_weight_g or 100.0
        ing = await self._meals.ingredient(item)
        return ing if ing.product_id is not None else None

    async def recipe_of(self, product_id: int) -> Recipe | None:
        """FR-39.7: состав рецепта для карточки — с текущими плотностями ингредиентов."""
        return await self._meals.recipe_view(product_id)

    async def edit_recipe(
        self, product_id: int, picks: list[SetPick], *, yield_g: float | None
    ) -> Product | None:
        """FR-39.7: правка состава рецепта → пересчёт плотности."""
        return await self._meals.edit_recipe(product_id, picks, yield_g=yield_g)

    async def log_set(
        self, user: User, name: str, picks: list[SetPick], *, update: bool, day_offset: int = 0
    ) -> MealLogged | None:
        """FR-39.6: набор с правкой состава перед записью."""
        meal = await self._meals.log_set(
            user, name, picks, update=update, day_offset=day_offset
        )
        if meal is None:
            return None
        stats = await self._stats.daily(user, days_ago=-day_offset)
        return MealLogged(meal=meal, remaining=stats.remaining)

    async def last_use_of(self, user: User, product_id: int) -> tuple[datetime, float] | None:
        """Когда и сколько человек ел продукт в последний раз — для карточки (§6.25)."""
        return (await self._meals.last_use(_require_id(user), [product_id])).get(product_id)

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
        by_id = {p.id: p for p in items}
        # Группы зависят только от id, ключа и имени (domain.duplicates.group). Каталог меняется
        # редко, а «Итоги» спрашивают группы на каждом открытии и дважды (ккал и белок) —
        # пересчитываем, только когда подпись каталога сменилась (16.10.2026: было 4.5 с).
        sig = hashlib.md5(repr(sorted(
            (p.id, p.canonical_key, p.display_name) for p in items if p.id is not None
        )).encode()).hexdigest()
        # Замок: «Итоги» спрашивают группы двумя запросами разом — без него оба считали бы заново.
        async with _groups_lock():
            ids = await self._groups_for(sig, items, uses)
        return [[by_id[i] for i in g if i in by_id] for g in ids], uses

    @staticmethod
    async def _groups_for(
        sig: str, items: list[Product], uses: dict[int, int]
    ) -> list[list[int]]:
        ids = _GROUPS_MEMO.get(sig)
        if ids is None:
            rows = [
                Candidate(
                    id=p.id, canonical_key=p.canonical_key, display_name=p.display_name,
                    source=p.source, kcal_per_100g=p.density.kcal_per_100g,
                    uses=uses.get(p.id, 0),
                )
                for p in items
                if p.id is not None
            ]
            # Попарное сравнение — работа процессора: в потоке, чтобы не держать остальные запросы.
            found = await asyncio.to_thread(group, rows)
            ids = [[c.id for c in g] for g in found]
            _GROUPS_MEMO.clear()
            _GROUPS_MEMO[sig] = ids
        return ids

    async def merge_products(self, winner_id: int, loser_ids: list[int]) -> int:
        """Слить дубли. История не движется: перепривязывается только связь (INV-3)."""
        return await self._products.merge(winner_id, loser_ids)

    async def remember_dish(self, user: User, meal_id: int, name: str) -> Meal | None:
        """FR-39.2: дать приёму имя блюда. Продукт не заводим — сборка это состав (D-38)."""
        return await self._meals.save_template(user, meal_id, name)

    async def fix_product_density(self, product_id: int, density: Density) -> Product | None:
        # INV-11.2: ручную правку каталога тоже отсекаем по границам
        # (защита от опечатки «310 г белка»)
        current = await self._products.get_by_id(product_id)
        # Рецепт остаётся рецептом: иначе ручная правка (даже просто «Сохранить» в форме с
        # переименованием) прятала его состав и закрывала правку состава (ревью 15.10.2026).
        # Следующая правка состава пересчитает плотность из ингредиентов — это ожидаемо.
        source = "recipe" if current is not None and current.source == "recipe" else "manual"
        return await self._products.update_density(
            product_id, clamp_density(density), source=source
        )

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


def _names_from_phrase(parsed: ParsedMessage, text: str) -> None:
    """Фраза фото собрана нами, и имя каждой позиции в ней известно. Ключ вместо
    имени («grapes_red_fresh», замер 16.09.2026: 7 позиций из ~300) возвращаем к
    словам фразы.

    Один к одному сходится не всегда: разбор может разделить «курица с овощами»
    надвое. Тогда берём куски фразы, которые не заняты именами других позиций:
    единственный свободный — он и есть; несколько — по порядку позиции.
    """
    names = phrase_names(text)
    if not names:
        return
    if len(names) == len(parsed.items):
        for item, name in zip(parsed.items, names, strict=True):
            if looks_like_key(item.name):
                item.name = name
        return
    taken = {i.name.lower() for i in parsed.items if not looks_like_key(i.name)}
    free = [n for n in names if n.lower() not in taken]
    for idx, item in enumerate(parsed.items):
        if not looks_like_key(item.name) or not free:
            continue
        item.name = free.pop(0) if len(free) == 1 else free.pop(min(idx, len(free) - 1))


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
    """Первый продукт с порцией без размера — для него и зададим вопрос.

    Где размер ничего не меняет (вода, чёрный кофе, разница порций < 40 ккал), вопроса нет:
    пишем среднюю порцию (FR-21)."""
    for i, item in enumerate(parsed.items):
        if item.total_kcal:
            continue  # FR-22: явные КБЖУ названы — размер/грамовка не нужны
        if item.unit is Unit.PORTION and item.portion_hint is None:
            if not worth_asking_portion(
                item.zero_calorie, item.kcal_per_100g, item.portion_options
            ):
                item.portion_hint = "medium"
                continue
            return i
    return None


def _scan_gaps(reading: ScanReading, tz: str) -> set[str]:
    """Что на бланке не сходится с остальными числами. Пустое — чтение связное."""
    scan, _, reason = from_reading(reading, tz)
    return {reason or "no_scan"} if scan is None else mismatches(scan)
