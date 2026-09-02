"""Превращение доменных объектов в JSON. Одно место на весь API, чтобы форма
ответа не расползлась по обработчикам.

Здесь только раскладка полей: ни одного расчёта КБЖУ (INV-1), ни одного
обращения к репозиториям.
"""
from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from app.application.entities import Meal, MealItem, User
from app.domain.goals import Goal, Remaining


def goal_json(goal: Goal) -> dict[str, Any]:
    return {
        "kcal": goal.daily_kcal,
        "protein_g": goal.protein_g,
        "fat_g": goal.fat_g,
        "carb_g": goal.carb_g,
        "target": goal.target,
    }


def user_json(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "name": user.name,
        "timezone": user.timezone,
        "onboarded": user.onboarded_at is not None,
    }


def item_json(item: MealItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name_snapshot,
        "grams": round(item.grams, 1),
        "kcal": round(item.macros.kcal, 1),
        "protein_g": round(item.macros.protein, 1),
        "fat_g": round(item.macros.fat, 1),
        "carb_g": round(item.macros.carb, 1),
        "product_id": item.product_id,
        # INV-13: «≈» рисует фронт по этому флагу. Скрыть его нельзя —
        # пользователь обязан видеть, где цифра оценочная.
        "is_estimate": item.is_estimate,
    }


def meal_json(meal: Meal, tz: str) -> dict[str, Any]:
    local = meal.eaten_at.astimezone(ZoneInfo(tz))
    return {
        "id": meal.id,
        "at": local.strftime("%H:%M"),
        "at_iso": local.isoformat(),
        "input_type": meal.input_type,
        "raw_input": meal.raw_input,
        "kcal": round(meal.totals.kcal, 1),
        "protein_g": round(meal.totals.protein, 1),
        "fat_g": round(meal.totals.fat, 1),
        "carb_g": round(meal.totals.carb, 1),
        "estimated": any(i.is_estimate for i in meal.items),
        "items": [item_json(i) for i in meal.items],
    }


def remaining_json(rem: Remaining) -> dict[str, Any]:
    """Верхняя панель Е (§6.2): крупно то, что ещё можно, а не что съедено."""
    return {
        "kcal": round(rem.consumed.kcal, 1),
        "protein_g": round(rem.consumed.protein, 1),
        "fat_g": round(rem.consumed.fat, 1),
        "carb_g": round(rem.consumed.carb, 1),
        "kcal_left": rem.kcal_left,
        "protein_left": rem.protein_left,
        "fat_left": rem.fat_left,
        "carb_left": rem.carb_left,
    }


def logged_json(result: Any, tz: str) -> dict[str, Any]:
    """Ответ на успешную запись: сам приём + остаток дня, чтобы фронт не ходил
    за днём второй раз (§7.2)."""
    return {
        "kind": "logged",
        "meal": meal_json(result.meal, tz),
        "totals": remaining_json(result.remaining),
    }


def clarify_json(result: Any, draft_id: str) -> dict[str, Any]:
    """Уточнение. Черновик наружу не отдаётся — только его идентификатор (§7.1)."""
    return {
        "kind": "clarify",
        "draft_id": draft_id,
        "item_name": result.item_name,
        "options": list(getattr(result, "options", []) or []),
    }
