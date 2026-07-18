"""E2E для M2.2 + меню: недельная статистика, история, отмена (последней и по id)."""
from __future__ import annotations

import dataclasses
from datetime import date

from app.application.dto import Intent, ParsedMessage
from app.application.results import (
    DailyStats,
    History,
    MealDeleted,
    MealLogged,
    WeeklyStats,
)
from tests.e2e.test_meal_flow import _build, _chicken, _new_user
from tests.fakes import StubNlu


async def test_backdate_yesterday() -> None:
    """FR-33: «вчера курица» → приём на вчерашний день; сегодня его нет, вчера — есть."""
    meal = dataclasses.replace(_chicken(165), day_offset=-1)
    assistant, users, _ = _build(StubNlu(meal))
    user = await _new_user(users)

    result = await assistant.process(user, "вчера курица 250 грамм")
    assert isinstance(result, MealLogged)
    assert result.meal.eaten_at.date() == date(2026, 6, 17)  # FIXED_NOW − 1 день
    # остаток в карточке — за ВЧЕРА (включает записанный приём), а не за сегодня
    assert result.remaining.consumed.kcal > 0

    assert (await assistant.today(user)).meals_count == 0  # сегодня чисто
    yesterday = await assistant.day_view(user, days_ago=1)
    assert len(yesterday.meals) == 1  # приём лёг во вчера


async def test_backdate_tomorrow_does_not_touch_today() -> None:
    """FR-33: «завтра …» → предзапись на завтра; сегодняшний остаток не дёргается."""
    meal = dataclasses.replace(_chicken(165), day_offset=1)
    assistant, users, _ = _build(StubNlu(meal))
    user = await _new_user(users)

    result = await assistant.process(user, "завтра курица 250 грамм")
    assert isinstance(result, MealLogged)
    assert result.meal.eaten_at.date() == date(2026, 6, 19)  # FIXED_NOW + 1 день
    assert (await assistant.today(user)).meals_count == 0  # завтрашнее не в сегодня


async def test_no_offset_logs_today() -> None:
    """Без «вчера/завтра» — обычный сегодняшний приём (offset по умолчанию 0)."""
    assistant, users, _ = _build(StubNlu(_chicken(165)))
    user = await _new_user(users)
    result = await assistant.process(user, "курица 250 грамм")
    assert isinstance(result, MealLogged)
    assert result.meal.eaten_at.date() == date(2026, 6, 18)  # FIXED_NOW
    assert (await assistant.today(user)).meals_count == 1


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


async def test_today_returns_daily() -> None:
    assistant, users, _ = _build(StubNlu(_chicken(165)))
    user = await _new_user(users)

    await assistant.process(user, "курица 250 грамм")
    today = await assistant.today(user)

    assert isinstance(today, DailyStats)
    assert today.meals_count == 1
    assert today.remaining.consumed.kcal == 412.5


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


async def test_delete_meal_by_id() -> None:
    assistant, users, _ = _build(StubNlu(_chicken(165)))
    user = await _new_user(users)

    logged = await assistant.process(user, "курица 250 грамм")
    assert isinstance(logged, MealLogged)
    assert logged.meal.id is not None

    deleted = await assistant.delete_meal(user, logged.meal.id)
    assert deleted.meal is not None

    today = await assistant.today(user)
    assert today.meals_count == 0

    again = await assistant.delete_meal(user, logged.meal.id)
    assert again.meal is None
