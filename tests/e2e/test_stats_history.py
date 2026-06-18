"""E2E для M2.2: недельная статистика, история, отмена последней записи."""
from __future__ import annotations

from app.application.dto import Intent, ParsedMessage
from app.application.results import (
    DailyStats,
    History,
    MealDeleted,
    WeeklyStats,
)
from tests.e2e.test_meal_flow import _build, _chicken, _new_user
from tests.fakes import StubNlu


async def test_weekly_aggregates() -> None:
    assistant, users, _ = _build(StubNlu(_chicken(165), _chicken(165)))
    user = await _new_user(users)

    await assistant.process(user, "курица 250 грамм")
    await assistant.process(user, "курица 250 грамм")

    week = await assistant.weekly(user)
    assert isinstance(week, WeeklyStats)
    assert week.consumed.kcal == 825.0  # 412.5 * 2
    assert week.days_logged == 1
    assert week.meals_count == 2


async def test_history_lists_logged_meals() -> None:
    assistant, users, _ = _build(StubNlu(_chicken(165), _chicken(165)))
    user = await _new_user(users)

    await assistant.process(user, "курица 250 грамм")
    await assistant.process(user, "курица 250 грамм")

    history = await assistant.history(user)
    assert isinstance(history, History)
    assert len(history.meals) == 2
    assert history.timezone == "UTC"


async def test_undo_deletes_last_then_empty() -> None:
    assistant, users, _ = _build(
        StubNlu(_chicken(165), ParsedMessage(intent=Intent.QUERY, confidence=0.9))
    )
    user = await _new_user(users)

    await assistant.process(user, "курица 250 грамм")

    deleted = await assistant.undo(user)
    assert isinstance(deleted, MealDeleted)
    assert deleted.meal is not None

    stats = await assistant.process(user, "сколько осталось")
    assert isinstance(stats, DailyStats)
    assert stats.meals_count == 0

    again = await assistant.undo(user)
    assert again.meal is None
