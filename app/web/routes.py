"""REST мини-аппа. Слой тонкий (MA-INV-5): разобрать запрос, позвать use-case,
отдать JSON. Ни одного расчёта КБЖУ здесь быть не должно — это INV-1.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.container import Container
from app.web.deps import CurrentUser, get_container, get_session

router = APIRouter()


@router.get("/me")
async def me(
    user: CurrentUser,
    container: Annotated[Container, Depends(get_container)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Профиль и цели. Первый экран аппки ждёт именно это (§7.2)."""
    _, users = container.assistant_for(session)
    goal = await users.get_goal(user.id) if user.id else None
    fallback = container.settings.default_goal()
    g = goal or fallback
    return {
        "user": {
            "id": user.id,
            "name": user.name,
            "timezone": user.timezone,
            "onboarded": user.onboarded_at is not None,
        },
        "goal": {
            "kcal": g.daily_kcal,
            "protein_g": g.protein_g,
            "fat_g": g.fat_g,
            "carb_g": g.carb_g,
            "target": g.target,
            # Цель не задана — отдаём дефолт, но говорим об этом честно:
            # интерфейс обязан отличать «моя цель» от «пока общая».
            "is_default": goal is None,
        },
    }
