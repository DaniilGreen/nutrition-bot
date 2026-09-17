"""Превращение доменных объектов в JSON. Одно место на весь API, чтобы форма
ответа не расползлась по обработчикам.

Здесь только раскладка полей: ни одного расчёта КБЖУ (INV-1), ни одного
обращения к репозиториям.
"""
from __future__ import annotations

from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

from app.application.body_scans import GoalProposal, Overview
from app.application.entities import Meal, MealItem, User
from app.domain import energy
from app.domain.body_scan import BodyScan, Change
from app.domain.goals import ACTIVITY_FACTORS, TARGET_FACTORS, Goal, Remaining
from app.domain.week import WeekBalance


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
        # FR-22: цифры назвал сам человек — ни каталог, ни модель. Бот метит
        # такие строки «📋», аппка обязана вести себя так же на тех же данных.
        "is_stated": item.product_id is None and not item.is_estimate,
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


def clarify_json(result: Any, draft_id: str, kind: str) -> dict[str, Any]:
    """Уточнение. Черновик наружу не отдаётся — только его идентификатор (§7.1)."""
    return {
        "kind": "clarify",
        # "portion" — ждём вес, вариантов нет и быть не может.
        # "variant" — ждём выбор из предложенных замен имени.
        "ask": kind,
        "draft_id": draft_id,
        "item_name": result.item_name,
        "options": list(getattr(result, "options", []) or []),
    }


def product_json(p: Any, uses: int, usual_grams: float | None = None) -> dict[str, Any]:
    """Карточка продукта. `source` наружу нужен: он объясняет, почему цифре
    можно верить — этикетка это не то же, что оценка модели.

    `usual_grams` — привычная граммовка человека; без неё тап по строке
    каталога не знает, сколько записывать (MA-INV-24).
    """
    return {
        "id": p.id,
        "name": p.display_name,
        "key": p.canonical_key,
        "category": p.category,
        "kcal_per_100g": p.density.kcal_per_100g,
        "protein_per_100g": p.density.protein_per_100g,
        "fat_per_100g": p.density.fat_per_100g,
        "carb_per_100g": p.density.carb_per_100g,
        "unit_weight_g": p.unit_weight_g,
        "default_portion_g": p.default_portion_g,
        "source": p.source,
        "uses": uses,
        # Своя граммовка, иначе курируемый размер порции, иначе 100 г.
        "usual_grams": round(
            usual_grams or p.default_portion_g or p.unit_weight_g or 100.0, 1
        ),
    }


def week_json(w: WeekBalance) -> dict[str, Any]:
    """Недельный баланс (§6.23)."""
    return {
        "start": w.start.isoformat(),
        "end": w.end.isoformat(),
        "goal_kcal": round(w.goal_kcal),
        "week_goal_kcal": round(w.week_goal_kcal),
        "consumed_kcal": round(w.consumed_kcal),
        "balance_kcal": round(w.balance_kcal),
        "logged_days": w.logged_days,
        "average_kcal": round(w.average_kcal),
        "days": [
            {"date": d.day.isoformat(), "kcal": None if d.kcal is None else round(d.kcal),
             "over": d.over}
            for d in w.days
        ],
        "remaining_days": w.remaining_days,
        "per_day_kcal": w.per_day_kcal,
        "floor_hit": w.floor_hit,
        "verdict": w.verdict,
    }


SCAN_FIELDS = (
    "weight_kg", "skeletal_muscle_kg", "body_fat_kg", "body_fat_pct", "fat_free_kg",
    "visceral_level", "bmr_kcal", "water_l", "protein_kg", "minerals_kg",
    "height_cm", "age",
)


def scan_json(s: BodyScan, tz: str) -> dict[str, Any]:
    local = s.measured_at.astimezone(ZoneInfo(tz))
    out: dict[str, Any] = {f: getattr(s, f) for f in SCAN_FIELDS}
    out.update({
        "id": s.id,
        "measured_at": s.measured_at.isoformat(),
        "date": local.date().isoformat(),
        "time": local.strftime("%H:%M"),
        "sex": s.sex,
        "device": s.device,
    })
    return out


def change_json(c: Change | None) -> dict[str, Any] | None:
    if c is None:
        return None
    return {
        "days": c.days, "weight_kg": c.weight_kg, "body_fat_kg": c.body_fat_kg,
        "fat_free_kg": c.fat_free_kg, "skeletal_muscle_kg": c.skeletal_muscle_kg,
        "body_fat_pct": c.body_fat_pct, "visceral_level": c.visceral_level,
        "lean_share": None if c.lean_share is None else round(c.lean_share, 2),
    }


def proposal_json(p: GoalProposal | None) -> dict[str, Any] | None:
    if p is None:
        return None
    return {
        "current": goal_json(p.current),
        "proposed": goal_json(p.proposed),
        "changed": p.changed,
        "bmr_kcal": round(p.bmr_kcal),
        "bmr_source": p.bmr_source,
        "activity": p.activity,
        "activity_factor": ACTIVITY_FACTORS[p.activity],
        "target_factor": TARGET_FACTORS.get(p.current.target, 1.0),
        "burn_kcal": round(p.burn_kcal),
        "base": p.base,
        "tdee_kcal": p.tdee_kcal,
        "fat_free_kg": p.fat_free_kg,
    }


def overview_json(o: Overview, tz: str) -> dict[str, Any]:
    d, sp = o.diary, o.spend
    return {
        "scans": [scan_json(s, tz) for s in o.scans],
        "change": change_json(o.last_change),
        "lean_warning": o.lean_warning,
        "stale_days": o.stale_days,
        "inbody_bmr": o.inbody_bmr,
        "formula_bmr": o.formula_bmr,
        "burn_kcal": o.burn_kcal,
        "diary": None if d is None else {
            "verdict": d.verdict, "since": o.diary_since.isoformat() if o.diary_since else None,
            "deficit_kcal": d.deficit_kcal, "expected_kg": d.expected_kg,
            "actual_kg": d.actual_kg, "logged_days": d.logged_days, "days": d.days,
        },
        "energy": None if sp is None else {
            "verdict": sp.verdict, "days": sp.days, "logged_days": sp.logged_days,
            "intake_kcal": sp.intake_kcal, "tdee_kcal": sp.tdee_kcal,
            "error_kcal": sp.error_kcal, "proposable": sp.proposable,
            "need_days": ceil(energy.MIN_LOGGED_SHARE * sp.days),
        },
        "proposal": proposal_json(o.proposal),
    }
