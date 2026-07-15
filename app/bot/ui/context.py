"""Дневной контекст: один сбор данных для всех «сегодняшних» экранов + производные метрики.

Один `gather()` кормит «Сегодня», карточку приёма и вечерний итог: у них общий костяк (остаток
дня, недельный якорь, погрешность, добор). Производные метрики (EMA веса, доля оценок, средняя за
7 дней) считаются ЗДЕСЬ и только здесь — чтобы одно и то же число не разъезжалось между экранами.

ДВЕ ЛОВУШКИ КОДОВОЙ БАЗЫ, которые тут обойдены:
1. «Неделя» существует в двух смыслах: картинка 📈 рисует СКОЛЬЗЯЩИЕ 7 дней (day_calories),
   а assistant.weekly() считает КАЛЕНДАРНУЮ Пн–Вс. Берём только скользящие: «за 7 дней» —
   это то, что человек и имеет в виду. weekly() не трогаем вовсе.
2. Профиль (пол/рост/возраст/активность) НЕ персистится — визард держит его в FSM и стирает.
   В БД только Goal и вес (в weight_logs). Поэтому «твой режим: похудение» показать нельзя,
   а вес для «белка на кг» берём из последнего взвешивания.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.application.assistant import Assistant
from app.application.entities import Meal, MealItem, Product, User, WeightLog
from app.application.results import DailyStats, MonthReport, WeightTrend
from app.bot.dashboard_image import _streak  # единственное определение серии — не плодим второе
from app.bot.ui.kit import sigma_kcal
from app.domain.goals import Goal, in_goal


@dataclass
class DayContext:
    today: DailyStats
    days: list[tuple[date, float]]  # последние 7 дней: (дата, ккал); 0.0 = день НЕ логировался
    goal: Goal
    week7: MonthReport  # month_report(days=7): avg_macros уже поделено на дни С ЗАПИСЯМИ
    month: MonthReport  # month_report(days=30)
    weight: WeightTrend
    products: list[Product]

    # --- производные ---
    weight_kg: float | None  # последнее взвешивание — для белка на кг
    trend_kg: float | None  # EMA веса: дневной вес скачет на воде, тренд — сигнал
    streak: int  # дней подряд в цели
    local_hour: int  # час у пользователя: «недобор» и советы имеют смысл только к концу дня
    est_kcal_share: int  # % калорий за сегодня, посчитанных ОЦЕНКОЙ, а не по точному весу

    @property
    def last_meal(self) -> Meal | None:
        return self.today.meals[-1] if self.today.meals else None

    @property
    def items(self) -> list[MealItem]:
        return [i for m in self.today.meals for i in m.items]

    @property
    def sigma(self) -> float:
        """Погрешность дня в ккал (см. kit.sigma_kcal). «1820 ± 230» честнее, чем «1820»."""
        return sigma_kcal([(i.macros.kcal, i.is_estimate) for i in self.items])

    @property
    def est_items(self) -> int:
        return sum(1 for i in self.items if i.is_estimate)

    @property
    def top_item(self) -> MealItem | None:
        """Где были деньги: позиция с максимальным вкладом в калории дня.

        Это ЕДИНСТВЕННАЯ информация о прошлом, которая меняет будущий выбор. «Чай, 2 ккал»
        не меняет ничего; «пицца 400 г — 1050 ккал, 38% дня» меняет следующий ужин.
        """
        return max(self.items, key=lambda i: i.macros.kcal, default=None)

    @property
    def to_clarify(self) -> MealItem | None:
        """Что уточнить, чтобы точность дня выросла сильнее всего: argmax(kcal × 0.25) у оценок.

        Уточнять 20 г зелени бессмысленно — это налог на внимание без отдачи. Уточнить 400 г
        пасты — режет погрешность дня в разы.
        """
        est = [i for i in self.items if i.is_estimate]
        return max(est, key=lambda i: i.macros.kcal, default=None)

    @property
    def trust_ok(self) -> bool:
        """Можно ли давать точечные советы. При >50% оценок база плывёт сильнее совета."""
        return self.est_kcal_share <= 50

    @property
    def logged7(self) -> int:
        return self.week7.days_logged

    @property
    def avg7_kcal(self) -> float:
        return self.week7.avg_macros.kcal

    @property
    def day_over(self) -> bool:
        """День фактически закрыт → можно говорить «недобор» и подводить итоги.

        До вечера недобор — не факт, а нетерпение: человек просто ещё не поужинал.
        """
        return self.local_hour >= 21

    @property
    def advice_due(self) -> bool:
        """Пора ли советовать добор.

        На завтраке совет «съешь 400 г куриной грудки» абсурден: впереди ещё два приёма, разрыв
        закроется сам. Совет уместен, когда день почти прожит — по часам или по съеденному.
        """
        eaten = self.today.remaining.consumed.kcal
        goal = self.goal.daily_kcal
        return self.local_hour >= 17 or (goal > 0 and eaten >= 0.6 * goal)

    @property
    def days_since_weigh_in(self) -> int | None:
        """Сколько дней назад последнее взвешивание. Тренд без этого числа читать нельзя."""
        if not self.weight.logs:
            return None
        last = self.weight.logs[0].measured_at
        return max(0, (datetime.now(UTC) - last).days)

    @property
    def trend_ok(self) -> bool:
        """Можно ли ПОКАЗЫВАТЬ тренд.

        Нельзя, если замеров мало или последний протух: линия, построенная по четырём точкам
        месячной давности, — не сглаживание, а выдумка. Честнее сказать «встань на весы».
        """
        stale = self.days_since_weigh_in
        return (
            len(self.weight.logs) >= MIN_LOGS
            and stale is not None
            and stale <= STALE_DAYS
            and self.trend_kg is not None
        )

    @property
    def protein_per_kg(self) -> float | None:
        """Белок на кг массы. При дефиците цель 1.6–2.2 г/кг: абсолютные «164 г» ничего не
        значат, пока не знаешь — это 60-килограммовый человек или 100-килограммовый."""
        if not self.weight_kg:
            return None
        return round(self.today.remaining.consumed.protein / self.weight_kg, 2)

    @property
    def avg7_protein_per_kg(self) -> float | None:
        if not self.weight_kg or not self.week7.days_logged:
            return None
        return round(self.week7.avg_macros.protein / self.weight_kg, 2)


ALPHA_PER_DAY = 0.12  # эффективное окно ≈ 16 дней
STALE_DAYS = 10  # последний замер старше — тренд протух; показывать его нельзя
MIN_LOGS = 4  # замеров меньше — линии просто нет, и врать об этом не надо


def _ema(logs: list[WeightLog]) -> float | None:
    """Сглаживание веса, зависящее ОТ ВРЕМЕНИ, а не от числа замеров.

    Наивная EMA (α на каждый замер) на нерегулярных взвешиваниях ВРЁТ. У владельца 4 замера:
    86.8, 86.6, 86.2 подряд в июне и 83.6 через неделю — наивная формула выдавала «тренд 85.9 кг»
    при последнем реальном взвешивании 83.6. Расхождение 2.3 кг: три старые точки весили столько
    же, сколько одна свежая. Это не сглаживание, это ложь.

    Правильно: α_eff = 1 − (1 − α)^Δd — не вставал на весы неделю, значит новое измерение весит
    больше. Δd < 1 дня (два взвешивания за утро) считаем как день, иначе второе просто исчезает.

    Зачем сглаживать вообще: дневной вес — шум (вода, гликоген, содержимое ЖКТ: ±1–1.5 кг за
    сутки). По линии решают «дефицит работает или нет», а не по «вчера 80.2, сегодня 81.1».
    """
    if not logs:
        return None
    acc = logs[0].weight_kg
    prev = logs[0].measured_at
    for log in logs[1:]:
        days = max(1.0, (log.measured_at - prev).total_seconds() / 86400)
        alpha = 1 - (1 - ALPHA_PER_DAY) ** days
        acc = alpha * log.weight_kg + (1 - alpha) * acc
        prev = log.measured_at
    return round(acc, 1)


def _est_share(today: DailyStats) -> int:
    """Какая доля сегодняшних калорий посчитана ОЦЕНКОЙ (is_estimate), а не по точному весу.

    Продукт продаёт точность — значит обязан честно говорить, где он её не гарантирует.
    """
    total = sum(i.macros.kcal for m in today.meals for i in m.items)
    if total <= 0:
        return 0
    est = sum(i.macros.kcal for m in today.meals for i in m.items if i.is_estimate)
    return round(100 * est / total)


async def gather(assistant: Assistant, user: User) -> DayContext:
    """Один поход в БД на все разделы демо. Пустые данные (нет веса/продуктов) — не падаем."""
    today = await assistant.today(user)
    days, goal = await assistant.day_calories(user, 7)
    week7 = await assistant.month_report(user, days=7)
    month = await assistant.month_report(user, days=30)
    weight = await assistant.weight_trend(user, limit=14)
    products = await assistant.fixable_products(limit=6)

    # logs идут newest-first → для EMA нужен хронологический порядок
    chron = list(reversed(weight.logs))

    return DayContext(
        today=today,
        days=days,
        goal=goal,
        week7=week7,
        month=month,
        weight=weight,
        products=products,
        weight_kg=chron[-1].weight_kg if chron else None,
        trend_kg=_ema(chron),
        streak=_streak(days, goal.daily_kcal),
        est_kcal_share=_est_share(today),
        local_hour=datetime.now(ZoneInfo(today.timezone)).hour,
    )


def day_status(kcal: float, goal_kcal: float) -> str:
    """Статус дня для таблиц недели: пусто / в цели / мало / перебор. Коридор — общий (in_goal)."""
    if kcal <= 0:
        return "none"
    if in_goal(kcal, goal_kcal):
        return "ok"
    return "over" if kcal > goal_kcal else "low"
