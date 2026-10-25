"""Результаты работы Assistant — данные для презентационного слоя (бот форматирует)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from app.application.dto import ParsedMessage, PlateReading, ScanReading
from app.application.entities import Meal, MealItem, Product, WeightLog
from app.domain.goals import Goal, Remaining
from app.domain.sessions import group
from app.domain.value_objects import Macros, sum_macros


@dataclass
class MealLogged:
    meal: Meal
    remaining: Remaining


@dataclass
class DishSaved:
    """FR-39: блюдо своей сборки заведено в каталог. Приёма пищи тут нет — только продукт."""

    product: Product
    portions: int | None = None  # сколько порций вышло из котла (вход «фраза»)


@dataclass
class ClarificationNeeded:
    """Нужен один уточняющий вопрос (M2): размер порции для конкретного продукта."""

    draft: ParsedMessage
    item_index: int
    item_name: str


@dataclass
class VariantClarificationNeeded:
    """D-19: запрос неточен по КБЖУ-важной оси — LLM предложил варианты (строки) для кнопок."""

    draft: ParsedMessage
    item_index: int
    item_name: str
    options: list[str]  # готовые замены имени от LLM («с курицей», «с говядиной»)


@dataclass
class LabelRead:
    """FR-27: распознана этикетка — показываем КБЖУ/100г и ждём вес порции (через continue_meal)."""

    draft: ParsedMessage
    item_index: int
    atwater_warning: bool = False  # FR-27: Б/Ж/У ≠ ккал на этикетке → вероятен misread, показать ⚠️


@dataclass
class PlateRead:
    """FR-34: блюдо разобрано на составляющие — показываем карточку и ждём «Записать»."""

    reading: PlateReading
    grams: list[float]          # по составляющим, уже с весом от человека, если был
    weighed: bool               # вес назвал человек (подпись) — не «на глаз»
    total_g: float | None = None


@dataclass
class BodyScanRead:
    """FR-36: на снимке бланк состава тела — дальше его проверяет BodyScanService."""

    reading: ScanReading


@dataclass
class WeightLogged:
    log: WeightLog
    delta_kg: float | None


@dataclass
class WeightTrend:
    logs: list[WeightLog]  # newest-first
    timezone: str


@dataclass
class DayPace:
    """§6.20: сегодняшний темп против личной нормы к этому часу.

    `show=False` означает «спрашивать не о чем или не о чем спрашивать ещё»:
    нормы не набралось (MA-INV-37), день идёт нормально или человек только
    что записывал. Причину наружу не отдаём — интерфейсу нужно решение.
    """

    show: bool
    hour: int
    norm_kcal: float
    today_kcal: float
    days_used: int
    last_at: str | None = None
    norm_points: list[tuple[int, float]] = field(default_factory=list)
    today_points: list[tuple[int, float]] = field(default_factory=list)


@dataclass
class DailyStats:
    remaining: Remaining
    meals_count: int
    meals: list[Meal] = field(default_factory=list)  # сегодняшние приёмы (список съеденного)
    timezone: str = "UTC"  # для рендера времени приёмов


@dataclass
class MealSession:
    """«Приём пищи» (FR-47): записи одного дня подряд. Не хранится — собирается при чтении."""

    meals: list[Meal]  # по времени; хотя бы одна
    totals: Macros  # сумма записей — те же числа, что в итогах дня (INV-1, INV-3)

    @property
    def start(self) -> datetime:
        return self.meals[0].eaten_at

    @property
    def end(self) -> datetime:
        return self.meals[-1].eaten_at

    @property
    def meal_ids(self) -> list[int]:
        return [m.id for m in self.meals if m.id is not None]

    @property
    def estimated(self) -> bool:
        return any(i.is_estimate for m in self.meals for i in m.items)

    @staticmethod
    def group(meals: list[Meal]) -> list[MealSession]:
        """Записи ОДНОГО дня → приёмы пищи по времени. День режет вызывающий (граница дня
        человека), правило разрыва — домен (`domain.sessions`). Равное время — по id."""
        ordered = sorted(meals, key=lambda m: (m.eaten_at, m.id or 0))
        return [
            MealSession([ordered[i] for i in idx], sum_macros(ordered[i].totals for i in idx))
            for idx in group([m.eaten_at for m in ordered])
        ]


@dataclass
class DayView:
    """Вар. A: приёмы за один день + навигация ← / → по дням."""

    meals: list[Meal]  # хронологически (утро→вечер)
    timezone: str
    days_ago: int  # 0 = сегодня
    day: date  # дата дня в TZ юзера (для заголовка)
    sessions: list[MealSession] = field(default_factory=list)  # те же записи приёмами пищи (FR-47)


@dataclass
class WeeklyStats:
    consumed: Macros
    days_logged: int
    meals_count: int
    goal: Goal


@dataclass
class MonthReport:
    """Отчёт за период (FR-29): средние/день, дни в цели, Δ веса — для картинки /month."""

    total_days: int
    days_logged: int
    days_in_goal: int
    avg_macros: Macros
    weight_delta: float | None


@dataclass
class History:
    meals: list[Meal]
    timezone: str


@dataclass
class MealDeleted:
    meal: Meal | None


@dataclass
class ItemRemoved:
    """Коррекция «удали X»: позиции сняты, показываем обновлённый остаток дня."""

    items: list[MealItem]
    remaining: Remaining


@dataclass
class ItemAdjusted:
    """Коррекция «поправь граммы у X»: позиция пересчитана под новый вес, остаток обновлён."""

    item: MealItem
    old_grams: float
    remaining: Remaining


@dataclass
class CorrectionFailed:
    """Коррекция не выполнена: не нашли цель (not_found) или op не поддержан (unsupported)."""

    target: str
    reason: str  # not_found | unsupported


@dataclass
class MealCorrected:
    """FR-44: правка ответом на карточку — что стало с ЭТИМ приёмом. `old` — какими позиции были
    (заменённая, до правки веса, убранные), `new` — какими стали (у удаления пусто). meal None —
    позиций не осталось, приём удалён."""

    kind: str  # fix | adjust | delete
    meal: Meal | None
    remaining: Remaining
    old: list[MealItem]
    new: list[MealItem]


@dataclass
class ItemPickNeeded:
    """FR-44: фраза-замена на приём из нескольких позиций, и какую менять — не понять. Разбор
    отдаём наружу: после тапа заменим им же, второй раз в модель не ходим."""

    meal: Meal
    draft: ParsedMessage


@dataclass
class Unknown:
    reason: str = ""
    # Готовый текст человеку — показывается как есть. Без него бот угадывал смысл по словам
    # причины, и отказ рецепта «вне разумных пределов» уходил в подсказку про вес тела.
    say: str | None = None
    # FR-45: разбор счёл фразу не едой и не командой, и она похожа на вопрос — бот отдаст её
    # ассистенту. Ставится ТОЛЬКО на intent=unknown: запись еды сюда не попадает никогда.
    question: bool = False


Result = (
    MealLogged
    | ClarificationNeeded
    | VariantClarificationNeeded
    | LabelRead
    | PlateRead
    | BodyScanRead
    | WeightLogged
    | WeightTrend
    | DailyStats
    | WeeklyStats
    | History
    | DayView
    | MealDeleted
    | ItemRemoved
    | ItemAdjusted
    | CorrectionFailed
    | MealCorrected
    | ItemPickNeeded
    | Unknown
)
