"""REST мини-аппа. Слой тонкий (MA-INV-5): разобрать запрос, позвать use-case,
отдать JSON. Ни одного расчёта КБЖУ здесь быть не должно — это INV-1.

Зависимость `current_user` висит на роутере целиком, а не на каждом обработчике:
эндпоинтов будет полтора десятка, и «забыли повесить» — самый дешёвый способ
открыть дыру (MA-INV-6).
"""
from __future__ import annotations

from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.coach import LimitReached
from app.application.entities import User
from app.application.results import (
    ClarificationNeeded,
    CorrectionFailed,
    DailyStats,
    ItemAdjusted,
    ItemRemoved,
    MealLogged,
    VariantClarificationNeeded,
    WeightLogged,
)
from app.container import Container
from app.domain.duplicates import SAFE_SPREAD, Candidate, spread, winner
from app.domain.goals import Goal
from app.domain.prefs import merge as merge_prefs
from app.domain.prefs import sanitize
from app.domain.value_objects import Density
from app.web.deps import CurrentAssistant, CurrentUser, current_user, get_session
from app.web.errors import ApiError, bad_request, not_found, unavailable
from app.web.schemas import (
    clarify_json,
    goal_json,
    logged_json,
    meal_json,
    product_json,
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
    body: dict[str, Any], user: CurrentUser, assistant: CurrentAssistant, request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    """Записать приём текстом (§7.2). Разбор и подсчёт — в application (INV-1)."""
    text = str(body.get("text") or "").strip()
    if not text:
        raise bad_request("empty_text", "Нечего записывать.")
    if len(text) > 1000:
        raise bad_request("text_too_long", "Слишком длинно — раздели на два сообщения.")

    replay = _replay(request, user, idempotency_key)
    if replay is not None:
        return replay
    _throttle(request, user, "meals", limit=20, window=60)

    result = await assistant.process(user, text, input_type=body.get("input_type") or "text")
    out = _result_json(result, user, request, raw_input=text)
    return _remember(request, user, idempotency_key, out)


@router.post("/meals/quick")
async def log_quick(
    body: dict[str, Any], user: CurrentUser, assistant: CurrentAssistant, request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    """Запись тапом по «частому»: без модели, мгновенно (AC-MA-31).

    Через `/api/meals` тап стоил бы денег и секунд, а вдобавок мог бы вернуть
    уточняющий вопрос — на продукте, который человек ест каждый день.
    """
    try:
        product_id = int(body["product_id"])
        grams = float(body["grams"])
    except (KeyError, TypeError, ValueError) as exc:
        raise bad_request("bad_quick", "Нужны product_id и grams.") from exc
    if not 0 < grams <= 5000:
        raise bad_request("bad_grams", "Вес вне разумных пределов.")
    replay = _replay(request, user, idempotency_key)
    if replay is not None:
        return replay
    result = await assistant.log_quick(user, product_id, grams)
    out = _result_json(result, user, request, raw_input="")
    return _remember(request, user, idempotency_key, out)


@router.post("/meals/clarify")
async def clarify(
    body: dict[str, Any], user: CurrentUser, assistant: CurrentAssistant, request: Request
) -> dict[str, Any]:
    """Ответ на уточнение: клиент присылает только draft_id и свой выбор."""
    token = str(body.get("draft_id") or "")
    answer = str(body.get("answer") or "").strip()
    # Валидация ДО изъятия: черновик одноразовый, и съесть его на пустом ответе
    # значит потерять разобранный приём.
    if not answer:
        raise bad_request("empty_answer", "Пустой ответ.")
    draft = request.app.state.drafts.take(token, _uid(user))
    if draft is None:
        # Один код на «нет такого» и «чужой»: различать их — подсказывать перебор.
        raise bad_request("draft_gone", "Уточнение устарело. Запиши приём заново.")

    if draft.kind == "portion":
        # Не хватало веса. Ответ — это вес, а не имя: дописываем его к позиции
        # и продолжаем запись. Пропустить это через resolve_clarification
        # значило бы записать «300 г» как название продукта.
        result = await assistant.answer_portion(
            user, draft.payload, draft.item_index, answer, raw_input=draft.raw_input
        )
    else:
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
    _throttle(request, user, "voice", limit=10, window=60)
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
    meal = await assistant.get_meal(user, meal_id)
    if meal is None or not any(i.id == item_id for i in meal.items):
        raise not_found("item_not_found", "Позиция не найдена.")
    result = await assistant.remove_item(user, meal_id, item_id)
    stats = await assistant.today(user)
    # None означает «позиций не осталось, приём удалён целиком» — это успех,
    # а не 404. Отдавать ошибку на успешное действие значит врать интерфейсу.
    return {
        "meal": meal_json(result.meal, user.timezone) if result else None,
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
        kind = "variant" if isinstance(result, VariantClarificationNeeded) else "portion"
        token = request.app.state.drafts.put(
            _uid(user), result.draft, result.item_index, raw_input, kind
        )
        return clarify_json(result, token, kind)

    if isinstance(result, ItemRemoved):
        return {
            "kind": "item_removed",
            "names": [i.name_snapshot for i in result.items],
            "totals": remaining_json(result.remaining),
        }

    if isinstance(result, ItemAdjusted):
        return {
            "kind": "item_adjusted",
            "name": result.item.name_snapshot,
            "grams": round(result.item.grams, 1),
            "old_grams": round(result.old_grams, 1),
            "totals": remaining_json(result.remaining),
        }

    if isinstance(result, CorrectionFailed):
        # Машинный токен наружу не отдаём: человек прочтёт «not_found» и решит,
        # что сломалось приложение.
        text = {
            "not_found": "Не нашёл такой позиции в сегодняшних записях.",
            "unsupported": "Так править пока не умею — удали и запиши заново.",
        }.get(result.reason, "Не получилось поправить.")
        return {"kind": "correction_failed", "message": text}

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

@router.get("/stats/series")
async def series(
    user: CurrentUser, assistant: CurrentAssistant, days: int = 30
) -> dict[str, Any]:
    """Ряд по дням для всех графиков сразу (§6.9).

    Один эндпоинт на четыре графика, а не четыре: данные одни и те же, а
    лишние round-trip'ы на мобильной сети заметнее, чем размер ответа.
    """
    if not 1 <= days <= 365:
        raise bad_request("bad_range", "Период от 1 до 365 дней.")
    rows, goal = await assistant.daily_series(user, days)
    return {
        "goal": goal_json(goal),
        "days": [
            {
                "date": day.isoformat(),
                "kcal": round(macros.kcal, 1),
                "protein_g": round(macros.protein, 1),
                "fat_g": round(macros.fat, 1),
                "carb_g": round(macros.carb, 1),
                "meals": meals,
            }
            for day, macros, meals in rows
        ],
    }


@router.get("/stats/weight")
async def weight(
    user: CurrentUser, assistant: CurrentAssistant, limit: int = 30
) -> dict[str, Any]:
    """Замеры веса, старые→новые. Мусорные значения отсекает use-case."""
    if not 1 <= limit <= 365:
        raise bad_request("bad_range", "От 1 до 365 замеров.")
    trend = await assistant.weight_trend(user, limit)
    tz = ZoneInfo(user.timezone)
    logs = sorted(trend.logs, key=lambda log: log.measured_at)
    return {
        "points": [
            {
                "date": log.measured_at.astimezone(tz).date().isoformat(),
                "kg": log.weight_kg,
            }
            for log in logs
        ]
    }


@router.post("/weight")
async def log_weight(
    body: dict[str, Any], user: CurrentUser, assistant: CurrentAssistant, request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    """Запись веса одним полем, без модели (§8.3).

    Через общий ввод вес идёт текстом и стоит вызова LLM — на цифре, которую
    человек и так знает точно.
    """
    try:
        kg = float(body["weight_kg"])
    except (KeyError, TypeError, ValueError) as exc:
        raise bad_request("bad_weight", "Вес должен быть числом.") from exc

    replay = _replay(request, user, idempotency_key)
    if replay is not None:
        return replay
    try:
        log, delta = await assistant.log_weight(user, kg)
    except ValueError as exc:
        # Диапазон 20–400 кг держит WeightService — одна проверка на бота и аппку.
        raise bad_request("bad_weight", str(exc)) from exc
    out = {
        "id": log.id,
        "weight_kg": log.weight_kg,
        "delta_kg": delta,
        "at": log.measured_at.astimezone(ZoneInfo(user.timezone)).isoformat(),
    }
    return _remember(request, user, idempotency_key, out)


@router.delete("/weight/{log_id}")
async def delete_weight(
    log_id: int, user: CurrentUser, assistant: CurrentAssistant
) -> dict[str, Any]:
    """Удалить свой замер. Без этого опечатку «83,6» вместо «93,6» не починить,
    а именно про такой выброс и рассказывает график веса."""
    gone = await assistant.delete_weight(user, log_id)
    if gone is None:
        raise not_found("weight_not_found", "Замер не найден.")
    return {"deleted": gone.id, "weight_kg": gone.weight_kg}


@router.get("/stats/reconcile")
async def reconcile_weight(
    user: CurrentUser, assistant: CurrentAssistant, days: int = 30
) -> dict[str, Any]:
    """Сходится ли дневник с весами (§4.0) — флагманская панель.

    Ничего не обвиняет и не считает «сколько недозаписал»: кладёт рядом два
    факта и говорит, сходятся ли они. Вывод делает человек (MA-INV-28).
    """
    if not 7 <= days <= 180:
        raise bad_request("bad_range", "От 7 до 180 дней.")
    r = await assistant.reconcile_weight(user, days=days)
    return {
        "days": r.days,
        "logged_kcal_per_day": r.logged_kcal_per_day,
        "weight_delta_kg": r.weight_delta_kg,
        "gap_kcal_per_day": r.gap_kcal_per_day,
        "verdict": r.verdict,
        "agrees": r.agrees,
    }


@router.get("/stats/contributions")
async def contributions(
    user: CurrentUser, assistant: CurrentAssistant, days: int = 30, limit: int = 8
) -> dict[str, Any]:
    """Куда уходят калории (§3.3): абсолютный вклад продуктов, не доли.

    Доли нормируются на съеденное — чем меньше ешь, тем лучше картина; такая
    метрика прячет ровно ту проблему, ради которой экран нужен (§4.1).
    """
    if not 1 <= days <= 365 or not 1 <= limit <= 30:
        raise bad_request("bad_range", "Неверные параметры.")
    rows = await assistant.contributions(user, days=days, limit=limit)
    total = sum(k for _, _, k, _ in rows)
    return {
        "total_kcal": round(total, 1),
        "items": [
            {"product_id": pid, "name": name, "kcal": round(k, 1), "times": t}
            for pid, name, k, t in rows
        ],
    }


@router.get("/stats/accuracy")
async def accuracy(
    user: CurrentUser, assistant: CurrentAssistant, days: int = 30
) -> dict[str, Any]:
    """Доверие к цифре (§3.5): какая доля калорий пришла из оценок.

    По калориям, а не по числу позиций: одна оценённая тарелка борща весит
    больше десяти точно взвешенных специй.
    """
    if not 1 <= days <= 365:
        raise bad_request("bad_range", "Период от 1 до 365 дней.")
    estimated, total = await assistant.accuracy(user, days=days)
    return {
        "estimated_kcal": round(estimated, 1),
        "total_kcal": round(total, 1),
        "estimated_share": round(estimated / total, 3) if total else 0.0,
    }


@router.get("/frequent")
async def frequent(
    user: CurrentUser, assistant: CurrentAssistant, days: int = 30, limit: int = 10
) -> dict[str, Any]:
    """Чипы быстрой записи: что человек ест чаще всего и в какой граммовке (§6.3).

    Граммовка — медиана, а не среднее: одна ошибка «2000 г» сдвинула бы среднее
    так, что подсказка перестала бы попадать в реальность.
    """
    if not 1 <= days <= 365 or not 1 <= limit <= 30:
        raise bad_request("bad_range", "Неверные параметры.")
    rows = await assistant.frequent(user, days=days, limit=limit)
    return {
        "items": [
            {"product_id": pid, "name": name, "grams": round(grams, 1), "times": times}
            for pid, name, grams, times in rows
        ]
    }


@router.post("/meals/{meal_id}/repeat")
async def repeat(meal_id: int, user: CurrentUser, assistant: CurrentAssistant) -> dict[str, Any]:
    """Повторить приём одним тапом: тот же состав, тот же вес, текущее время.

    Копируется снапшот КБЖУ, а не пересчитывается из каталога (INV-3): повтор
    обязан совпасть с оригиналом до грамма. Если каталог с тех пор поправили,
    повтор всё равно повторяет то, что было съедено, а не то, что стало в базе.
    """
    result = await assistant.repeat_meal(user, meal_id)
    if result is None:
        raise not_found("meal_not_found", "Приём не найден — возможно, его удалили.")
    return logged_json(result, user.timezone)


@router.get("/products")
async def products(
    user: CurrentUser, assistant: CurrentAssistant,
    q: str | None = None, offset: int = 0, limit: int = 20,
) -> dict[str, Any]:
    """Каталог страницами: полный список нельзя — их больше двух сотен (§7.2)."""
    if offset < 0 or limit < 1 or limit > 100:
        raise bad_request("bad_paging", "Неверные параметры страницы.")
    items, total, uses = await assistant.catalog_page(
        user, offset=offset, limit=limit, query=(q or "").strip() or None
    )
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [product_json(p, uses.get(p.id or 0, 0)) for p in items],
    }


@router.get("/products/{product_id}")
async def product(
    product_id: int, user: CurrentUser, assistant: CurrentAssistant
) -> dict[str, Any]:
    p = await assistant.get_product(product_id)
    if p is None:
        raise not_found("product_not_found", "Продукт не найден.")
    counts = await assistant.usage_of(user, product_id)
    return {"product": product_json(p, counts)}


@router.patch("/products/{product_id}")
async def edit_product(
    product_id: int, body: dict[str, Any], user: CurrentUser, assistant: CurrentAssistant
) -> dict[str, Any]:
    """Правка каталога: имя и/или плотность.

    Меняем плотность — меняем будущее, но не прошлое: снапшоты в meal_items
    остаются как были (INV-3). Именно поэтому правка безопасна.
    """
    updated = None
    if "name" in body:
        name = str(body["name"] or "").strip()
        if not 2 <= len(name) <= 120:
            raise bad_request("bad_name", "Имя от 2 до 120 символов.")
        updated = await assistant.rename_product(product_id, name)

    macro_keys = ("kcal_per_100g", "protein_per_100g", "fat_per_100g", "carb_per_100g")
    if any(k in body for k in macro_keys):
        try:
            density = Density(
                kcal_per_100g=float(body["kcal_per_100g"]),
                protein_per_100g=float(body["protein_per_100g"]),
                fat_per_100g=float(body["fat_per_100g"]),
                carb_per_100g=float(body["carb_per_100g"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise bad_request("bad_density", "Нужны все четыре числа КБЖУ.") from exc
        if not _density_sane(density):
            raise bad_request("density_out_of_range", "Числа вне разумных пределов.")
        updated = await assistant.fix_product_density(product_id, density)

    if updated is None:
        raise not_found("product_not_found", "Продукт не найден или менять нечего.")
    return {"product": product_json(updated, 0)}


@router.get("/products/duplicates/groups")
async def duplicates(user: CurrentUser, assistant: CurrentAssistant) -> dict[str, Any]:
    """Группы дублей. Автоматически не сливаем: где плотности разошлись, слияние
    заметно двинуло бы цифры — решает человек (D-32)."""
    groups, uses = await assistant.duplicate_groups()
    out = []
    for g in groups:
        rows = [
            Candidate(
                id=p.id or 0, canonical_key=p.canonical_key, display_name=p.display_name,
                source=p.source, kcal_per_100g=p.density.kcal_per_100g,
                uses=uses.get(p.id or 0, 0),
            )
            for p in g
        ]
        win = winner(rows)
        out.append({
            "spread": round(spread(rows), 3),
            "safe": spread(rows) < SAFE_SPREAD,
            "suggested_winner": win.id,
            # Сколько позиций истории переедет при слиянии. Без этого числа
            # человек соглашается на необратимое вслепую (AC-MA-26).
            "affects": sum(r.uses for r in rows if r.id != win.id),
            "items": [product_json(p, uses.get(p.id or 0, 0)) for p in g],
        })
    return {"groups": out}


@router.post("/products/merge")
async def merge(
    body: dict[str, Any], user: CurrentUser, assistant: CurrentAssistant
) -> dict[str, Any]:
    """Слить дубли в одного.

    Необратимо: проигравшие строки каталога удаляются, вернуть их можно только
    из бэкапа. Поэтому выжившего называет клиент явно — угадывать нельзя.
    """
    try:
        winner_id = int(body["winner_id"])
        loser_ids = [int(x) for x in body["loser_ids"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise bad_request("bad_merge", "Нужны winner_id и loser_ids.") from exc
    if not loser_ids:
        raise bad_request("bad_merge", "Некого сливать.")
    if winner_id in loser_ids:
        raise bad_request("bad_merge", "Выживший не может быть в списке на слияние.")
    if await assistant.get_product(winner_id) is None:
        raise not_found("product_not_found", "Продукт-выживший не найден.")
    moved = await assistant.merge_products(winner_id, loser_ids)
    return {"merged": len(loser_ids), "items_repointed": moved}


def _density_sane(d: Density) -> bool:
    """Границы физики, а не вкуса: 100 г не бывают отрицательными и не бывают
    калорийнее чистого жира (900 ккал).

    Домен и так зажимает значения (`clamp_density`, INV-11.2) — но зажать «310 г
    белка» до 100 значит молча записать не то, что человек ввёл. На границе
    доверия отказ понятнее тихой правки, поэтому проверка тут дублирует зажим
    намеренно.
    """
    if not 0 <= d.kcal_per_100g <= 900:
        return False
    return all(0 <= v <= 100 for v in (d.protein_per_100g, d.fat_per_100g, d.carb_per_100g))


@router.post("/coach")
async def coach(
    body: dict[str, Any], user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Вопрос по своим данным (§8.7). Лимит трат проверяется ДО запроса."""
    question = str(body.get("question") or "").strip()
    if not question:
        raise bad_request("empty_question", "Пустой вопрос.")
    if len(question) > 500:
        raise bad_request("question_too_long", "Слишком длинный вопрос.")

    # Месячный лимит не спасает от залипшего клиента: он выжжет бюджет за минуты.
    _throttle(request, user, "coach", limit=6, window=60)
    container: Container = request.app.state.container
    service = container.coach_for(session)
    if service is None:
        raise unavailable("coach_off", "Ассистент недоступен: не настроен ключ.")
    try:
        answer = await service.ask(user, question)
    except LimitReached as exc:
        raise ApiError(
            429, "coach_limit",
            "Бюджет ассистента на этот месяц исчерпан. Он вернётся первого числа.",
        ) from exc
    return {
        "text": answer.text,
        "spent_usd": round(answer.spent_usd, 4),
        "limit_usd": answer.limit_usd,
    }


@router.get("/coach/budget")
async def coach_budget(
    user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Сколько бюджета осталось. Нужен настройкам: без счётчика строка «Лимит»
    была бы декоративной, а таких мы не выкатываем (MA-INV-31)."""
    container: Container = request.app.state.container
    spent = await container.spend_for(session).spent_this_month(purpose="coach")
    return {
        "spent_usd": round(spent, 4),
        "limit_usd": container.settings.coach_month_limit_usd,
        "available": container.coach_port is not None,
    }


@router.get("/settings")
async def settings(
    user: CurrentUser, assistant: CurrentAssistant, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Всё, что показывает экран настроек, одним запросом (§6.5)."""
    container: Container = request.app.state.container
    goal = await assistant.current_goal(user)
    spent = await container.spend_for(session).spent_this_month(purpose="coach")
    return {
        "goal": goal_json(goal),
        "timezone": user.timezone,
        "prefs": sanitize(user.ui_prefs),
        "coach": {
            "model": container.settings.coach_model,
            "limit_usd": container.settings.coach_month_limit_usd,
            "spent_usd": round(spent, 4),
            "available": container.coach_port is not None,
        },
    }


@router.patch("/settings/prefs")
async def edit_prefs(
    body: dict[str, Any], user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Правка оформления. Неизвестные ключи молча отбрасываются: клиент может
    быть новее сервера, и падать из-за этого незачем."""
    container: Container = request.app.state.container
    _, users = container.assistant_for(session)
    merged = merge_prefs(user.ui_prefs, body)
    await users.set_prefs(_uid(user), merged)
    return {"prefs": merged}


@router.patch("/settings/goal")
async def edit_goal(
    body: dict[str, Any], user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Цели. Белок можно задать в г/кг — тогда он поедет вслед за весом,
    а не устареет молча в тот самый момент, когда человек худеет (§6.5)."""
    container: Container = request.app.state.container
    assistant, users = container.assistant_for(session)
    current = await assistant.current_goal(user)

    protein = body.get("protein_g")
    if body.get("protein_per_kg") is not None:
        try:
            per_kg = float(body["protein_per_kg"])
        except (TypeError, ValueError) as exc:
            raise bad_request("bad_goal", "Граммы на килограмм должны быть числом.") from exc
        if not 0.5 <= per_kg <= 5:
            raise bad_request("bad_goal", "От 0,5 до 5 г белка на килограмм.")
        trend = await assistant.weight_trend(user, 1)
        if not trend.logs:
            raise bad_request(
                "no_weight", "Чтобы считать белок на килограмм, нужен хотя бы один замер веса."
            )
        protein = per_kg * trend.logs[0].weight_kg

    try:
        goal = Goal(
            daily_kcal=float(body.get("kcal", current.daily_kcal)),
            protein_g=float(protein if protein is not None else current.protein_g),
            fat_g=float(body.get("fat_g", current.fat_g)),
            carb_g=float(body.get("carb_g", current.carb_g)),
            target=str(body.get("target", current.target)),
        )
    except (TypeError, ValueError) as exc:
        raise bad_request("bad_goal", "Цели должны быть числами.") from exc

    if not 800 <= goal.daily_kcal <= 6000:
        raise bad_request("kcal_out_of_range", "Калорийность вне разумных пределов.")
    if not all(0 <= v <= 600 for v in (goal.protein_g, goal.fat_g, goal.carb_g)):
        raise bad_request("macros_out_of_range", "Макросы вне разумных пределов.")
    if goal.target not in ("cut", "maintain", "bulk"):
        raise bad_request("bad_target", "Неизвестный режим.")

    await users.set_goal(_uid(user), goal)
    return {"goal": goal_json(goal)}


@router.patch("/settings/timezone")
async def edit_timezone(
    body: dict[str, Any], user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Часовой пояс режет сутки. Сменив его, человек двигает границы всех дней
    задним числом — поэтому значение проверяется, а не принимается на веру."""
    name = str(body.get("timezone") or "")
    try:
        ZoneInfo(name)
    except (KeyError, ValueError, ZoneInfoNotFoundError) as exc:
        raise bad_request("bad_timezone", "Не знаю такого часового пояса.") from exc
    container: Container = request.app.state.container
    _, users = container.assistant_for(session)
    await users.set_timezone(_uid(user), name)
    return {"timezone": name}


def _uid(user: User) -> int:
    """Идентификатор пользователя. `current_user` уже отверг запрос, если его нет,
    — здесь остаётся только сузить тип для проверяльщика."""
    assert user.id is not None
    return user.id


def _replay(request: Request, user: User, key: str | None) -> dict[str, Any] | None:
    """Повтор с тем же `Idempotency-Key` возвращает первый ответ, а не пишет заново.

    Двойной тап и ретрай браузера иначе дают два приёма — цифра выглядит
    правдоподобно и молча врёт вдвое (§7.5).
    """
    if not key:
        return None
    remembered = request.app.state.idempotency.get(_uid(user), key)
    return remembered if isinstance(remembered, dict) else None


def _remember(
    request: Request, user: User, key: str | None, response: dict[str, Any]
) -> dict[str, Any]:
    if key:
        request.app.state.idempotency.put(_uid(user), key, response)
    return response


def _throttle(request: Request, user: User, bucket: str, *, limit: int, window: float) -> None:
    if not request.app.state.limiter.allow(_uid(user), bucket, limit=limit, window=window):
        raise ApiError(429, "rate_limited", "Слишком часто. Подожди немного.")
