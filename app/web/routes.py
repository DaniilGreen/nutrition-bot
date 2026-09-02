"""REST мини-аппа. Слой тонкий (MA-INV-5): разобрать запрос, позвать use-case,
отдать JSON. Ни одного расчёта КБЖУ здесь быть не должно — это INV-1.

Зависимость `current_user` висит на роутере целиком, а не на каждом обработчике:
эндпоинтов будет полтора десятка, и «забыли повесить» — самый дешёвый способ
открыть дыру (MA-INV-6).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.application.entities import User
from app.application.results import (
    ClarificationNeeded,
    DailyStats,
    MealLogged,
    VariantClarificationNeeded,
    WeightLogged,
)
from app.web.deps import CurrentAssistant, CurrentUser, current_user
from app.web.errors import bad_request, not_found, unavailable
from app.web.schemas import (
    clarify_json,
    goal_json,
    logged_json,
    meal_json,
    remaining_json,
    user_json,
)

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

@router.post("/meals")
async def log_meal(
    body: dict[str, Any], user: CurrentUser, assistant: CurrentAssistant, request: Request
) -> dict[str, Any]:
    """Записать приём текстом (§7.2). Разбор и подсчёт — в application (INV-1)."""
    text = str(body.get("text") or "").strip()
    if not text:
        raise bad_request("empty_text", "Нечего записывать.")
    if len(text) > 1000:
        raise bad_request("text_too_long", "Слишком длинно — раздели на два сообщения.")
    result = await assistant.process(user, text, input_type=body.get("input_type") or "text")
    return _result_json(result, user, request, raw_input=text)


@router.post("/meals/clarify")
async def clarify(
    body: dict[str, Any], user: CurrentUser, assistant: CurrentAssistant, request: Request
) -> dict[str, Any]:
    """Ответ на уточнение: клиент присылает только draft_id и свой выбор."""
    token = str(body.get("draft_id") or "")
    answer = str(body.get("answer") or "").strip()
    draft = request.app.state.drafts.take(token, user.id)
    if draft is None:
        # Один код на «нет такого» и «чужой»: различать их — подсказывать перебор.
        raise bad_request("draft_gone", "Уточнение устарело. Запиши приём заново.")
    if not answer:
        raise bad_request("empty_answer", "Пустой ответ.")
    result = await assistant.resolve_clarification(
        user, draft.payload, draft.item_index, answer, raw_input=draft.raw_input
    )
    return _result_json(result, user, request, raw_input=draft.raw_input)


@router.post("/meals/voice")
async def log_voice(
    request: Request, user: CurrentUser, assistant: CurrentAssistant
) -> dict[str, Any]:
    """Голос сырым телом, без multipart: пакета `python-multipart` в проекте нет,
    а ради одного поля тянуть зависимость незачем (§7.2)."""
    audio = await request.body()
    if not audio:
        raise bad_request("empty_audio", "Запись пустая.")
    if len(audio) > 1_500_000:
        # Тот же предел, что у бота: длинная фраза доезжает, а мегабайтные файлы
        # держали бы воркера впустую.
        raise bad_request("audio_too_big", "Слишком длинная запись — до полутора минут.")
    text = await assistant.transcribe(audio, filename="voice.ogg")
    if not text:
        raise unavailable("stt_off", "Распознавание голоса недоступно.")
    result = await assistant.process(user, text, input_type="voice")
    return {**_result_json(result, user, request, raw_input=text), "transcript": text}


@router.delete("/meals/{meal_id}")
async def delete_meal(
    meal_id: int, user: CurrentUser, assistant: CurrentAssistant
) -> dict[str, Any]:
    deleted = await assistant.delete_meal(user, meal_id)
    if deleted is None or getattr(deleted, "meal", None) is None:
        raise not_found("meal_not_found", "Приём не найден.")
    stats = await assistant.today(user)
    return {"deleted": True, "totals": remaining_json(stats.remaining)}


@router.delete("/meals/{meal_id}/items/{item_id}")
async def delete_item(
    meal_id: int, item_id: int, user: CurrentUser, assistant: CurrentAssistant
) -> dict[str, Any]:
    """Убрать одну позицию: приём из трёх блюд не должен удаляться целиком
    из-за одной ошибки."""
    result = await assistant.remove_item(user, meal_id, item_id)
    if result is None:
        raise not_found("item_not_found", "Позиция не найдена.")
    stats = await assistant.today(user)
    return {
        "meal": meal_json(result.meal, user.timezone) if result.meal.items else None,
        "totals": remaining_json(stats.remaining),
    }


def _result_json(
    result: Any, user: User, request: Request, *, raw_input: str
) -> dict[str, Any]:
    """Единственное место, где доменный результат превращается в ответ API.

    Разложено здесь, а не в каждом обработчике: типов результата шесть, и
    «забыли обработать» означало бы молчаливый 500 на живом вводе.
    """
    if isinstance(result, MealLogged):
        return logged_json(result, user.timezone)

    if isinstance(result, (ClarificationNeeded, VariantClarificationNeeded)):
        token = request.app.state.drafts.put(
            user.id, result.draft, result.item_index, raw_input
        )
        return clarify_json(result, token)

    if isinstance(result, WeightLogged):
        return {
            "kind": "weight",
            "weight_kg": result.log.weight_kg,
            "delta_kg": result.delta_kg,
        }

    if isinstance(result, DailyStats):
        # Пользователь спросил, а не записал: отдаём сводку, не выдумывая приём.
        return {"kind": "stats", "totals": remaining_json(result.remaining)}

    reason = getattr(result, "reason", None) or "Не понял, что записать."
    return {"kind": "unknown", "message": reason}
