"""E2E: багрепорт (/bug) сохраняет заметку + контекст последних приёмов (FR-26/D-17)."""
from __future__ import annotations

from datetime import UTC, datetime

from app.application.assistant import Assistant
from app.application.meal_service import MealService
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.domain.goals import Goal
from tests.e2e.test_meal_flow import _chicken
from tests.fakes import (
    InMemoryBugRepo,
    InMemoryMealRepo,
    InMemoryProductRepo,
    InMemoryUserRepo,
    InMemoryWeightRepo,
    StubNlu,
)

FIXED = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)


async def test_report_bug_saves_note_and_context() -> None:
    users, products = InMemoryUserRepo(), InMemoryProductRepo()
    meals, weights, bugs = InMemoryMealRepo(), InMemoryWeightRepo(), InMemoryBugRepo()

    def clock() -> datetime:
        return FIXED

    meal_service = MealService(products, meals, clock)
    stats = StatsService(meals, users, Goal(2000, 150, 65, 250), clock)
    assistant = Assistant(
        StubNlu(_chicken(165)), products, meal_service,
        WeightService(weights, clock), stats, None, bugs,
    )
    user = await users.get_or_create(telegram_id=1, name="t", locale="ru", timezone="UTC")

    await assistant.process(user, "курица 250 грамм")
    await assistant.report_bug(user, "посчитал курицу неверно")

    assert len(bugs.reports) == 1
    r = bugs.reports[0]
    assert r["note"] == "посчитал курицу неверно"
    assert r["user_id"] == user.id
    assert any("курица" in c["raw_input"] for c in r["context"])  # контекст приложен


async def test_report_bug_noop_without_repo() -> None:
    # bug_repo не сконфигурирован → report_bug просто молчит, не падает
    users, products = InMemoryUserRepo(), InMemoryProductRepo()
    meals, weights = InMemoryMealRepo(), InMemoryWeightRepo()

    def clock() -> datetime:
        return FIXED

    assistant = Assistant(
        StubNlu(_chicken(165)), products, MealService(products, meals, clock),
        WeightService(weights, clock), StatsService(meals, users, Goal(2000, 150, 65, 250), clock),
    )
    user = await users.get_or_create(telegram_id=1, name="t", locale="ru", timezone="UTC")
    await assistant.report_bug(user, "тест")  # не должно бросить
