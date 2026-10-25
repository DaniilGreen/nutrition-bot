"""Дневной контекст: один сбор данных для всех «сегодняшних» экранов + производные метрики.

Один `gather()` кормит «Сегодня», карточку приёма и вечерний итог: у них общий костяк (остаток
дня, недельный якорь, погрешность, добор). Производные метрики (доля оценок, средняя за
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

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.application.assistant import Assistant
from app.application.entities import MealItem, Product, User
from app.application.results import DailyStats, MonthReport, WeightTrend
from app.bot.ui.kit import sigma_kcal
from app.domain.goals import Goal, in_goal
from app.domain.prefs import day_start
from app.domain.timeframe import local_date
from app.domain.value_objects import Macros
from app.domain.week import WeekBalance


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
    local_hour: int  # час у пользователя: «недобор» и советы имеют смысл только к концу дня
    est_kcal_share: int  # % калорий за сегодня, посчитанных ОЦЕНКОЙ, а не по точному весу
    week_bal: WeekBalance | None = None  # недельный баланс (§6.23); None — не считали
    # КБЖУ по дням за те же 7 дней, что `days` (старые → новые): средние «Недели» (FR-48) берут
    # только вошедшие дни, а `week7` делит на все дни с записями, включая неполный сегодня.
    macros7: list[tuple[date, Macros]] = field(default_factory=list)
    first_day: date | None = None  # день регистрации у человека: «из M прошедших» — с него

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
        """Сколько дней назад последнее взвешивание: «85,2 кг» без даты читать нельзя."""
        if not self.weight.logs:
            return None
        last = self.weight.logs[0].measured_at
        return max(0, (datetime.now(UTC) - last).days)

    @property
    def weight_points(self) -> list[tuple[datetime, float]]:
        """Замеры (время, кг), старые → новые — один вход для текста «⚖️ Вес» и PNG (FR-8, D-40)."""
        return [(x.measured_at, x.weight_kg) for x in reversed(self.weight.logs)]

    @property
    def protein_per_kg(self) -> float | None:
        """Белок на кг массы. При дефиците цель 1.6–2.2 г/кг: абсолютные «164 г» ничего не
        значат, пока не знаешь — это 60-килограммовый человек или 100-килограммовый."""
        if not self.weight_kg:
            return None
        return round(self.today.remaining.consumed.protein / self.weight_kg, 2)


def _est_share(today: DailyStats) -> int:
    """Какая доля сегодняшних калорий посчитана ОЦЕНКОЙ (is_estimate), а не по точному весу.

    Продукт продаёт точность — значит обязан честно говорить, где он её не гарантирует.
    """
    total = sum(i.macros.kcal for m in today.meals for i in m.items)
    if total <= 0:
        return 0
    est = sum(i.macros.kcal for m in today.meals for i in m.items if i.is_estimate)
    return round(100 * est / total)


async def gather(assistant: Assistant, user: User, scans: object | None = None) -> DayContext:
    """Один поход в БД на все разделы демо. Пустые данные (нет веса/продуктов) — не падаем.

    `scans` (BodyScanService) — только ради пола недели по обмену (§6.23); без него неделя
    считается с полом без замера."""
    today = await assistant.today(user)
    days, goal = await assistant.day_calories(user, 7)
    week7 = await assistant.month_report(user, days=7)
    month = await assistant.month_report(user, days=30)
    # 60 замеров: «за 4 недели» (FR-8) должно доставать месяц назад и при ежедневных весах.
    weight = await assistant.weight_trend(user, limit=60)
    products = await assistant.fixable_products(limit=6)
    rows, _ = await assistant.daily_series(user, 7)
    first_day = (
        local_date(user.created_at, user.timezone, day_start(user.ui_prefs))
        if user.created_at else None
    )

    return DayContext(
        today=today,
        days=days,
        goal=goal,
        week7=week7,
        month=month,
        weight=weight,
        products=products,
        weight_kg=weight.logs[0].weight_kg if weight.logs else None,  # logs — новые первыми
        est_kcal_share=_est_share(today),
        local_hour=datetime.now(ZoneInfo(today.timezone)).hour,
        week_bal=await assistant.week_balance(
            user, 0, bmr_kcal=await scans.bmr(user) if scans is not None else None
        ),
        macros7=[(d, m) for d, m, _ in rows],
        first_day=first_day,
    )


def day_status(kcal: float, goal_kcal: float) -> str:
    """Статус дня для таблиц недели: пусто / в цели / мало / перебор. Коридор — общий (in_goal)."""
    if kcal <= 0:
        return "none"
    if in_goal(kcal, goal_kcal):
        return "ok"
    return "over" if kcal > goal_kcal else "low"
