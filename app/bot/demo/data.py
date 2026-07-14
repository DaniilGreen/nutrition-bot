"""Данные для демо: один сбор — три дизайна. Плюс производные метрики (считаем ЗДЕСЬ).

Почему производные живут тут, а не в дизайнах: если каждый дизайн посчитает «среднее за неделю»
по-своему, владелец увидит три разных числа и справедливо решит, что бот врёт. Формула одна.

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
from datetime import date

from app.application.assistant import Assistant
from app.application.entities import Meal, MealItem, Product, User
from app.application.results import DailyStats, MonthReport, WeightTrend
from app.bot.dashboard_image import _streak  # единственное определение серии — не плодим второе
from app.bot.demo.kit import sigma_kcal
from app.domain.goals import Goal, in_goal


@dataclass
class DemoData:
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


def _ema(values: list[float], alpha: float = 0.25) -> float | None:
    """Экспоненциальное сглаживание веса (α=0.25 ≈ окно ~7 взвешиваний).

    Дневной вес — шум: вода, гликоген и содержимое кишечника дают ±1–1.5 кг за сутки. Тренд —
    сигнал. По нему решают «дефицит работает или нет», а не по «вчера 80.2, сегодня 81.1».
    """
    if not values:
        return None
    acc = values[0]
    for v in values[1:]:
        acc = alpha * v + (1 - alpha) * acc
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


async def collect(assistant: Assistant, user: User) -> DemoData:
    """Один поход в БД на все разделы демо. Пустые данные (нет веса/продуктов) — не падаем."""
    today = await assistant.today(user)
    days, goal = await assistant.day_calories(user, 7)
    week7 = await assistant.month_report(user, days=7)
    month = await assistant.month_report(user, days=30)
    weight = await assistant.weight_trend(user, limit=14)
    products = await assistant.fixable_products(limit=6)

    # logs идут newest-first → для EMA нужен хронологический порядок
    kgs = [log.weight_kg for log in reversed(weight.logs)]

    return DemoData(
        today=today,
        days=days,
        goal=goal,
        week7=week7,
        month=month,
        weight=weight,
        products=products,
        weight_kg=kgs[-1] if kgs else None,
        trend_kg=_ema(kgs),
        streak=_streak(days, goal.daily_kcal),
        est_kcal_share=_est_share(today),
    )


def day_status(kcal: float, goal_kcal: float) -> str:
    """Статус дня для таблиц недели: пусто / в цели / мало / перебор. Коридор — общий (in_goal)."""
    if kcal <= 0:
        return "none"
    if in_goal(kcal, goal_kcal):
        return "ok"
    return "over" if kcal > goal_kcal else "low"
