"""ИИ-ассистент: вопросы по своим данным (§8.7, MA-M6).

Отдельный сервис, а не метод в `Assistant`: у него своя ответственность —
собрать факты, спросить модель, записать расход. `Assistant` про деньги не знает
и знать не должен.

Что здесь НЕ делается: расчёт КБЖУ. Модель получает уже посчитанные цифры и
только объясняет их (INV-1). Спросить у неё «сколько калорий в борще» через
этот путь нельзя — для этого есть запись приёма.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.application.coach_data import CoachData
from app.application.entities import User
from app.application.ports import (
    BodyScanRepo,
    MealRepo,
    ProductRepo,
    SpendRepo,
    WeightRepo,
)
from app.application.stats_service import StatsService
from app.domain.mentions import Candidate, mentioned
from app.domain.spend import Usage, month_limit_reached


@dataclass
class CoachChunk:
    """Событие потока (§6.21.1). Один тип на все виды: разбирать во фронте
    пять форм дороже, чем нести пару пустых полей."""

    kind: str                       # delta | tool | reset | end | error
    text: str = ""                  # для delta — кусок ответа, для tool — имя
    usage: Usage | None = None
    spent_usd: float = 0.0
    limit_usd: float = 0.0
    actions: list[Candidate] = field(default_factory=list)


class CoachPort(Protocol):
    """Порт к языковой модели для свободного вопроса. Отдельный от `NluPort`:
    тот извлекает сущности по схеме, этот отвечает текстом.

    `data` — то, что модель вправе посмотреть сама (§6.21). Адаптер решает,
    сколько раз к ней сходить; расход возвращается суммой по всем раундам.
    """

    async def ask(
        self, question: str, facts: str, data: Any | None = None
    ) -> tuple[str, Usage]: ...

    def stream(
        self, question: str, facts: str, data: Any | None = None
    ) -> AsyncIterator[CoachChunk]:
        """То же самое, но кусками. Адаптер отдаёт delta/tool/reset и в конце
        один chunk с `usage` — расход по ВСЕМ раундам (MA-INV-40)."""
        ...


@dataclass
class CoachAnswer:
    text: str
    spent_usd: float
    limit_usd: float
    # Продукты, названные в ответе, которые человек уже ел: у них есть и
    # плотность, и своя граммовка — значит запись в один тап без модели.
    # §4.3: совет, который нельзя выполнить в один тап, остаётся советом.
    actions: list[Candidate] = field(default_factory=list)


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
        meals: MealRepo | None = None,
        weights: WeightRepo | None = None,
        products: ProductRepo | None = None,
        scans: BodyScanRepo | None = None,
    ) -> None:
        self._port = port
        self._spend = spend
        self._stats = stats
        self._limit = limit_usd
        self._weights = weights
        self._products = products
        self._scans = scans
        # Нужен только ради «что из этого записать»: берём частое человека,
        # там уже есть product_id и медианная граммовка.
        self._meals = meals

    async def ask(self, user: User, question: str) -> CoachAnswer:
        # Проверка ДО запроса. После — это «превысил и узнал», то есть лимит,
        # который ничего не ограничивает.
        spent = await self.ensure_budget()

        facts = await self._facts(user)
        # Инструменты выдаём только когда есть чем их обслужить: без репозиториев
        # модель получит вызовы, на которые нечем ответить (§6.21).
        data = (
            CoachData(user, self._stats, self._meals, self._weights, self._products,
                      self._scans)
            if self._meals is not None and self._weights is not None
            else None
        )
        # Расход уже записан адаптером по раундам (AI-PRINCIPLES §2); здесь только итог
        # для показа. Писать ещё раз — двойной учёт.
        text, usage = await self._port.ask(question, facts, data)
        return CoachAnswer(
            text=text,
            spent_usd=spent + usage.cost_usd,
            limit_usd=self._limit,
            actions=await self._actions(user, text),
        )

    async def ensure_budget(self) -> float:
        """Проверка лимита ДО первого байта потока (MA-INV-41).

        В потоке отказ уже не отдать нормальным кодом: заголовки ушли. Поэтому
        проверяем здесь, из обработчика, пока ответ ещё можно не начинать.
        """
        # Весь расход, а не только ассистента: лимит, который видит часть трат,
        # не ограничивает ничего (AI-PRINCIPLES §2).
        spent = await self._spend.spent_this_month()
        if month_limit_reached(spent, self._limit):
            raise LimitReached
        return spent

    async def ask_stream(self, user: User, question: str) -> AsyncIterator[CoachChunk]:
        """Тот же ответ, но кусками (§6.21.1)."""
        spent = await self.ensure_budget()
        facts = await self._facts(user)
        data = (
            CoachData(user, self._stats, self._meals, self._weights, self._products,
                      self._scans)
            if self._meals is not None and self._weights is not None
            else None
        )
        parts: list[str] = []
        usage: Usage | None = None
        async for chunk in self._port.stream(question, facts, data):
            if chunk.kind == "delta":
                parts.append(chunk.text)
            elif chunk.kind == "reset":
                # Модель написала преамбулу и передумала — напечатанное отменяем,
                # иначе оно зависнет над настоящим ответом.
                parts.clear()
            elif chunk.kind == "end":
                usage = chunk.usage
                continue          # свой end соберём ниже, с расходом и кнопками
            yield chunk

        text = "".join(parts).strip()
        if usage is not None:
            spent += usage.cost_usd   # записан адаптером по раундам, здесь — только показ
        yield CoachChunk(
            kind="end", text=text, spent_usd=spent, limit_usd=self._limit,
            actions=await self._actions(user, text),
        )

    async def _actions(self, user: User, text: str) -> list[Candidate]:
        """Что из названного можно записать одним тапом.

        Кандидаты — только то, что человек УЖЕ ел: у нового продукта нет ни
        запиннутой плотности, ни своей граммовки, и кнопка «Записать» повела бы
        в тот же разбор моделью, от которого мы тут и уходим.
        """
        if self._meals is None or user.id is None:
            return []
        rows = await self._meals.frequent_items(user.id, days=90, limit=40)
        return mentioned(
            text,
            [Candidate(pid, name, grams) for pid, name, grams, _ in rows if pid is not None],
        )

    async def _facts(self, user: User) -> str:
        """Короткая ориентировка, а не данные.

        Раньше здесь лежала таблица по дням за 30 суток — и после выдачи
        инструментов (§6.21) она стала вредной: модель читала таблицу, решала,
        что данные у неё уже есть, и отвечала «доступа к твоим продуктам у меня
        нет». Проверено живым вопросом 08.09.2026 — ни одного вызова инструмента.
        Теперь тут только фон: цель, период, полнота. Детали — инструментами.
        """
        rows = await self._stats.last_days_macros(user, 30)
        goal = await self._stats.current_goal(user)
        logged = [(d, m, k) for d, m, k in rows if k > 0]
        head = (
            f"Цель на день: {goal.daily_kcal:.0f} ккал, белок {goal.protein_g:.0f} г, "
            f"жир {goal.fat_g:.0f} г, углеводы {goal.carb_g:.0f} г."
        )
        if not logged:
            return head + "\nЗаписей за 30 дней нет."
        avg_kcal = sum(m.kcal for _, m, _ in logged) / len(logged)
        avg_prot = sum(m.protein for _, m, _ in logged) / len(logged)
        return "\n".join([
            head,
            f"Сегодня {rows[-1][0].isoformat()}.",
            f"За последние 30 дней записи есть в {len(logged)} днях из {len(rows)}.",
            f"Среднее по дням с записями: {avg_kcal:.0f} ккал, белок {avg_prot:.0f} г.",
            "Это только общий фон. Что человек ел, какие продукты дают калории "
            "и как менялся вес — смотри инструментами.",
        ])
