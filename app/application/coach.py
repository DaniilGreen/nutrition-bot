"""ИИ-ассистент: вопросы по своим данным (§8.7, MA-M6).

Отдельный сервис, а не метод в `Assistant`: у него своя ответственность —
собрать факты, спросить модель, записать расход. `Assistant` про деньги не знает
и знать не должен.

Что здесь НЕ делается: расчёт КБЖУ. Модель получает уже посчитанные цифры и
только объясняет их (INV-1). Спросить у неё «сколько калорий в борще» через
этот путь нельзя — для этого есть запись приёма.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.application.entities import User
from app.application.ports import SpendRepo
from app.application.stats_service import StatsService
from app.domain.spend import Usage, month_limit_reached


class CoachPort(Protocol):
    """Порт к языковой модели для свободного вопроса. Отдельный от `NluPort`:
    тот извлекает сущности по схеме, этот отвечает текстом."""

    async def ask(self, question: str, facts: str) -> tuple[str, Usage]: ...


@dataclass
class CoachAnswer:
    text: str
    spent_usd: float
    limit_usd: float


class LimitReached(Exception):
    """Бюджет месяца исчерпан. Отдельное исключение, а не пустой ответ: молчание
    выглядело бы как поломка, а человек должен понимать, почему отказ."""


class Coach:
    def __init__(
        self,
        port: CoachPort,
        spend: SpendRepo,
        stats: StatsService,
        limit_usd: float,
    ) -> None:
        self._port = port
        self._spend = spend
        self._stats = stats
        self._limit = limit_usd

    async def ask(self, user: User, question: str) -> CoachAnswer:
        # Проверка ДО запроса. После — это «превысил и узнал», то есть лимит,
        # который ничего не ограничивает.
        spent = await self._spend.spent_this_month(purpose="coach")
        if month_limit_reached(spent, self._limit):
            raise LimitReached

        facts = await self._facts(user)
        text, usage = await self._port.ask(question, facts)

        await self._spend.add(
            user_id=user.id,
            model=usage.model,
            purpose="coach",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
        )
        return CoachAnswer(text=text, spent_usd=spent + usage.cost_usd, limit_usd=self._limit)

    async def _facts(self, user: User) -> str:
        """Факты для модели — уже посчитанные приложением.

        Отдаём сводку, а не сырые приёмы: month-длина промпта это деньги, а
        для вопросов вида «почему не худею» нужны агрегаты, не список блюд.
        """
        rows = await self._stats.last_days_macros(user, 30)
        goal = await self._stats.current_goal(user)
        logged = [(d, m, k) for d, m, k in rows if k > 0]
        if not logged:
            return "Записей за 30 дней нет."
        avg_kcal = sum(m.kcal for _, m, _ in logged) / len(logged)
        avg_prot = sum(m.protein for _, m, _ in logged) / len(logged)
        lines = [
            f"Цель: {goal.daily_kcal:.0f} ккал, белок {goal.protein_g:.0f} г, "
            f"жир {goal.fat_g:.0f} г, углеводы {goal.carb_g:.0f} г.",
            f"Дней с записями за 30: {len(logged)} из {len(rows)}.",
            f"Среднее за день по записанным: {avg_kcal:.0f} ккал, белок {avg_prot:.0f} г.",
            "По дням (дата, ккал, белок, приёмов):",
        ]
        lines += [
            f"{d.isoformat()} {m.kcal:.0f} {m.protein:.0f} {k}" for d, m, k in rows
        ]
        return "\n".join(lines)
