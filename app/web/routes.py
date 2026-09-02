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
    eaten = stats.remaining.consumed
    return {
        "user": {
            "id": user.id,
            "name": user.name,
            "timezone": user.timezone,
            "onboarded": user.onboarded_at is not None,
        },
        "goal": {
            "kcal": goal.daily_kcal,
            "protein_g": goal.protein_g,
            "fat_g": goal.fat_g,
            "carb_g": goal.carb_g,
            "target": goal.target,
        },
        "today": {
            "kcal": eaten.kcal,
            "protein_g": eaten.protein,
            "fat_g": eaten.fat,
            "carb_g": eaten.carb,
            "kcal_left": stats.remaining.kcal_left,
            "protein_left": stats.remaining.protein_left,
            "meals_count": stats.meals_count,
        },
    }
