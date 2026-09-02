"""REST мини-аппа. Слой тонкий (MA-INV-5): разобрать запрос, позвать use-case,
отдать JSON. Ни одного расчёта КБЖУ здесь быть не должно — это INV-1.

Зависимость `current_user` висит на роутере целиком, а не на каждом обработчике:
эндпоинтов будет полтора десятка, и «забыли повесить» — самый дешёвый способ
открыть дыру (MA-INV-6).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.web.deps import CurrentAssistant, CurrentUser, current_user
from app.web.errors import bad_request
from app.web.schemas import goal_json, meal_json, remaining_json, user_json

router = APIRouter(dependencies=[Depends(current_user)])


@router.get("/me")
async def me(user: CurrentUser, assistant: CurrentAssistant) -> dict[str, Any]:
    """Профиль, цель и съеденное за сегодня — всё, что нужно первому экрану (§7.2).

    Цель берём через use-case, а не `goal or default` тут: подстановка дефолта
    уже живёт в `StatsService.current_goal`, и второй экземпляр разъехался бы
    с ботом.
    """
    goal = await assistant.current_goal(user)
    stats = await assistant.today(user)
    return {
        "user": user_json(user),
        "goal": goal_json(goal),
        "today": {**remaining_json(stats.remaining), "meals_count": stats.meals_count},
    }


@router.get("/day")
async def day(
    user: CurrentUser, assistant: CurrentAssistant, days_ago: int = 0
) -> dict[str, Any]:
    """Экран «День»: панель остатка сверху и лента приёмов (§6.2, §7.2).

    `days_ago` — навигация назад по дням. Вперёд не пускаем: будущего дня нет,
    а отрицательное значение молча показало бы завтра пустым.
    """
    if days_ago < 0 or days_ago > 365:
        raise bad_request("bad_days_ago", "День вне диапазона.")
    remaining, view = await assistant.day_card(user, days_ago)
    goal = await assistant.current_goal(user)
    return {
        "day": view.day.isoformat(),
        "days_ago": days_ago,
        "goal": goal_json(goal),
        "totals": remaining_json(remaining),
        "meals": [meal_json(m, user.timezone) for m in view.meals],
    }
