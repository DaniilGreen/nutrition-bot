"""E2E для M3: голос → транскрипт → существующий пайплайн."""
from __future__ import annotations

from app.application.assistant import Assistant
from app.application.meal_service import MealService
from app.application.results import MealLogged
from app.application.stats_service import StatsService
from app.application.weight_service import WeightService
from app.domain.goals import Goal
from tests.e2e.test_meal_flow import FIXED_NOW, _chicken, _new_user
from tests.fakes import (
    InMemoryMealRepo,
    InMemoryProductRepo,
    InMemoryUserRepo,
    InMemoryWeightRepo,
    StubNlu,
    StubStt,
)


def _build_voice(nlu: StubNlu, stt: StubStt | None) -> tuple[Assistant, InMemoryUserRepo]:
    users = InMemoryUserRepo()
    products = InMemoryProductRepo()
    meals = InMemoryMealRepo()
    weights = InMemoryWeightRepo()

    def clock():
        return FIXED_NOW

    assistant = Assistant(
        nlu,
        products,
        MealService(products, meals, clock),
        WeightService(weights, clock),
        StatsService(meals, users, Goal(2000, 150, 65, 250), clock),
        stt,
    )
    return assistant, users


async def test_voice_transcribes_and_logs() -> None:
    assistant, users = _build_voice(StubNlu(_chicken(165)), StubStt("курица 250 грамм"))
    user = await _new_user(users)

    transcript = await assistant.transcribe(b"ogg-bytes")
    assert transcript == "курица 250 грамм"

    result = await assistant.process(user, transcript, input_type="voice")
    assert isinstance(result, MealLogged)
    assert result.meal.input_type == "voice"
    assert result.meal.totals.kcal == 412.5


async def test_voice_unavailable_without_stt() -> None:
    assistant, users = _build_voice(StubNlu(_chicken(165)), None)
    transcript = await assistant.transcribe(b"ogg-bytes")
    assert transcript is None
