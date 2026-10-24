"""REST мини-аппа. Слой тонкий (MA-INV-5): разобрать запрос, позвать use-case,
отдать JSON. Ни одного расчёта КБЖУ здесь быть не должно — это INV-1.

Зависимость `current_user` висит на роутере целиком, а не на каждом обработчике:
эндпоинтов будет полтора десятка, и «забыли повесить» — самый дешёвый способ
открыть дыру (MA-INV-6).
"""
from __future__ import annotations

import copy
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Annotated, Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, Path, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.assistant import Assistant
from app.application.body_scans import BodyScanService, ScanCheck, from_reading
from app.application.coach import LimitReached, clean_history
from app.application.dto import SetPick, Unit
from app.application.entities import Meal, Recipe, RecipeItem, StatedUse, User
from app.application.meal_service import RecipeRejected
from app.application.results import (
    BodyScanRead,
    ClarificationNeeded,
    CorrectionFailed,
    DailyStats,
    DishSaved,
    ItemAdjusted,
    ItemRemoved,
    LabelRead,
    MealLogged,
    PlateRead,
    Unknown,
    VariantClarificationNeeded,
    WeightLogged,
)
from app.container import Container
from app.domain.audio import filename_for
from app.domain.body_scan import BodyScan, with_fields
from app.domain.duplicates import SAFE_SPREAD, Candidate, spread, winner
from app.domain.goals import Goal
from app.domain.goals import edit_goal as consistent_goal
from app.domain.label_portion import label_grams
from app.domain.plate import MAX_COMPONENTS
from app.domain.plausibility import MAX_BATCH_G, MAX_YIELD_G, gate_density, is_alcoholic
from app.domain.prefs import day_start, sanitize
from app.domain.prefs import merge as merge_prefs
from app.domain.timeframe import local_date
from app.domain.value_objects import Density
from app.web.deps import CurrentAssistant, CurrentScans, CurrentUser, current_user, get_session
from app.web.errors import ApiError, bad_request, not_found, unavailable
from app.web.schemas import (
    SCAN_FIELDS,
    clarify_json,
    goal_json,
    item_json,
    logged_json,
    meal_json,
    overview_json,
    product_json,
    proposal_json,
    remaining_json,
    scan_json,
    user_json,
    week_json,
)

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(current_user)])


def _day_offset(value: Any) -> int:
    """Экран, с которого пишут, → сдвиг дня (`days_ago` 1 → offset −1).

    Багрепорт #33: человек листает вчерашний день, пишет туда — и запись падает
    в сегодня. Запись обязана попадать в тот день, который человек видит перед
    собой. Вперёд не пускаем совсем: будущего дня в аппке нет, а отрицательный
    `days_ago` означал бы запись в завтра по невнимательности клиента.
    """
    try:
        days_ago = int(value or 0)
    except (TypeError, ValueError, OverflowError):  # Infinity из JSON — 400/0, а не 500
        return 0
    return -max(0, min(30, days_ago))


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
    user: CurrentUser, assistant: CurrentAssistant, scans: CurrentScans, days_ago: int = 0
) -> dict[str, Any]:
    """Экран «День»: панель остатка сверху и лента приёмов (§6.2, §7.2).

    `days_ago` — навигация назад по дням. Вперёд не пускаем: будущего дня нет,
    а отрицательное значение молча показало бы завтра пустым.
    """
    if days_ago < 0 or days_ago > 365:
        raise bad_request("bad_days_ago", "День вне диапазона.")
    remaining, view = await assistant.day_card(user, days_ago)
    goal = await assistant.current_goal(user)
    # Вес нужен ради «г/кг» под белком (§6.2): именно в этих единицах цель
    # ставит нутрициолог, и именно они не устаревают при изменении массы.
    trend = await assistant.weight_trend(user, 1)
    weight = trend.logs[0].weight_kg if trend.logs else None
    return {
        "day": view.day.isoformat(),
        "weight_kg": weight,
        "days_ago": days_ago,
        "goal": goal_json(goal),
        "totals": remaining_json(remaining),
        "meals": [meal_json(m, user.timezone) for m in view.meals],
        # §6.20: полем здесь, а не отдельным эндпоинтом — экран «День» сведён
        # к одному запросу на старте, и второй вернул бы мигание (MA-INV-32).
        "pace": await _pace_json(user, assistant) if days_ago == 0 else None,
        # §6.23: плашка недели — только на сегодняшнем дне.
        "week_balance": await _week(user, assistant, scans, 0) if days_ago == 0 else None,
    }


async def _week(
    user: User, assistant: Assistant, scans: BodyScanService, weeks_ago: int
) -> dict[str, Any] | None:
    w = await assistant.week_balance(user, weeks_ago, bmr_kcal=await scans.bmr(user))
    return week_json(w)


@router.get("/week")
async def week(
    user: CurrentUser, assistant: CurrentAssistant, scans: CurrentScans, weeks_ago: int = 0
) -> dict[str, Any]:
    """Недельный баланс (§6.23): текущая и до четырёх прошлых недель."""
    if not 0 <= weeks_ago <= 4:
        raise bad_request("bad_week", "Неделя вне диапазона.")
    goal = await assistant.current_goal(user)
    if goal.daily_kcal <= 0:
        return {"week": None}
    return {"week": await _week(user, assistant, scans, weeks_ago)}


async def _pace_json(user: User, assistant: Assistant) -> dict[str, Any] | None:
    """Карточка добора дня. None — спрашивать не о чем (§6.20, MA-INV-37)."""
    pace = await assistant.day_pace(user)
    if not pace.show:
        return None
    # Чипы — еда ПОСЛЕ текущего часа: общий список частого уже под полем ввода,
    # а здесь нужна именно та еда, которой в дневнике и не хватает.
    rows = await assistant.evening_frequent(user, since_hour=pace.hour)
    return {
        "hour": pace.hour,
        "norm_kcal": pace.norm_kcal,
        "today_kcal": pace.today_kcal,
        "gap_kcal": round(pace.norm_kcal - pace.today_kcal, 1),
        "last_at": pace.last_at,
        "days_used": pace.days_used,
        "norm_points": [list(p) for p in pace.norm_points],
        "today_points": [list(p) for p in pace.today_points],
        "chips": [
            {"product_id": pid, "name": name, "grams": round(grams, 1)}
            for pid, name, grams, _times in rows
        ],
    }

@router.post("/meals")
async def log_meal(
    body: dict[str, Any], user: CurrentUser, assistant: CurrentAssistant, request: Request,
    idem: CurrentIdem,
) -> dict[str, Any]:
    """Записать приём текстом (§7.2). Разбор и подсчёт — в application (INV-1)."""
    if idem.replay is not None:
        return idem.replay
    text = str(body.get("text") or "").strip()
    if not text:
        raise bad_request("empty_text", "Нечего записывать.")
    if len(text) > 1000:
        raise bad_request("text_too_long", "Слишком длинно — раздели на два сообщения.")

    _throttle(request, user, "meals", limit=20, window=60)

    offset = _day_offset(body.get("days_ago"))
    result = await assistant.process(
        user, text, input_type=body.get("input_type") or "text", day_offset=offset
    )
    out = _result_json(result, user, request, raw_input=text, day_offset=offset)
    return idem.remember(out)


@router.post("/meals/quick")
async def log_quick(
    body: dict[str, Any], user: CurrentUser, assistant: CurrentAssistant, request: Request,
    idem: CurrentIdem,
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
    if idem.replay is not None:
        return idem.replay
    result = await assistant.log_quick(
        user, product_id, grams, day_offset=_day_offset(body.get("days_ago"))
    )
    out = _result_json(result, user, request, raw_input="")
    return idem.remember(out)


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
    # Лимит до работы с черновиком: уточнение вариантов идёт в модель, а цикл
    # в клиенте иначе крутил бы отказы вечно и бесплатно только на вид.
    _throttle(request, user, "meals", limit=20, window=60)
    draft = request.app.state.drafts.take(token, _uid(user))
    if draft is None:
        # Один код на «нет такого» и «чужой»: различать их — подсказывать перебор.
        raise bad_request("draft_gone", "Уточнение устарело. Запиши приём заново.")

    if draft.kind == "label":
        # FR-27: ответ — вес порции, а не имя. Плотность уже прочитана с
        # упаковки и переоценке не подлежит: пересчёт через модель заменил бы
        # точные цифры этикетки на оценку (INV-2).
        item = draft.payload.items[draft.item_index]
        grams = label_grams(answer, item.unit_weight_g, item.serving_g)
        if grams is None:
            # Черновик одноразовый: не разобрали вес — кладём обратно и
            # спрашиваем ещё раз. Иначе непонятый ответ стоил бы фотографии.
            token = request.app.state.drafts.put(
                _uid(user), draft.payload, draft.item_index, draft.raw_input, "label",
                draft.day_offset,
            )
            return _label_json(draft.payload, draft.item_index, token, hint=True)
        item.grams = grams
        item.unit = Unit.GRAM
        result = await assistant.continue_meal(
            user, draft.payload, raw_input=draft.raw_input, input_type="label",
            day_offset=draft.day_offset,
        )
    elif draft.kind == "portion":
        # Не хватало веса. Ответ — это вес, а не имя: дописываем его к позиции
        # и продолжаем запись. Пропустить это через resolve_clarification
        # значило бы записать «300 г» как название продукта.
        result = await assistant.answer_portion(
            user, draft.payload, draft.item_index, answer,
            raw_input=draft.raw_input, day_offset=draft.day_offset,
        )
    else:
        result = await assistant.resolve_clarification(
            user, draft.payload, draft.item_index, answer,
            raw_input=draft.raw_input, day_offset=draft.day_offset,
        )
    # День берётся из черновика, а не из текущего запроса: человек мог уйти на
    # другой экран, пока отвечал, и запись уехала бы не туда, куда собирался.
    return _result_json(
        result, user, request, raw_input=draft.raw_input, day_offset=draft.day_offset
    )


@router.post("/meals/voice")
async def log_voice(
    request: Request, user: CurrentUser, assistant: CurrentAssistant, idem: CurrentIdem,
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
    # Ключ обязателен по смыслу: голос — самый дорогой путь записи, и повтор
    # после таймаута сети писал бы второй приём. Раньше фронт ключ слал, а
    # обработчик его не принимал — защита существовала только на бумаге.
    if idem.replay is not None:
        return idem.replay
    _throttle(request, user, "voice", limit=10, window=60)
    # Имя файла берём из типа: Whisper выбирает парсер по расширению, и
    # запись Safari (mp4), названная «voice.ogg», не распознавалась вовсе.
    text = await assistant.transcribe(
        audio, filename=filename_for(request.headers.get("content-type"))
    )
    if not text:
        raise unavailable("stt_off", "Распознавание голоса недоступно.")
    offset = _day_offset(request.query_params.get("days_ago"))
    result = await assistant.process(user, text, input_type="voice", day_offset=offset)
    out = {
        **_result_json(result, user, request, raw_input=text, day_offset=offset),
        "transcript": text,
    }
    return idem.remember(out)


@router.post("/meals/label")
async def log_label(
    request: Request, user: CurrentUser, assistant: CurrentAssistant, idem: CurrentIdem,
) -> dict[str, Any]:
    """FR-27: фото этикетки → КБЖУ/100 г → вопрос о весе порции.

    Тело сырое, как у голоса: multipart ради одного поля потребовал бы
    `python-multipart`, а зависимости в этом проекте платные (§7.2).
    """
    image = await request.body()
    if not image:
        raise bad_request("empty_image", "Фото пустое.")
    if len(image) > 4_000_000:
        # Клиент ужимает снимок до 1280 px перед отправкой; всё, что больше
        # четырёх мегабайт, — это неужатый оригинал, и платить за его разбор
        # незачем: таблица КБЖУ читается и с меньшего.
        raise bad_request("image_too_big", "Слишком большое фото — сними ближе.")
    if idem.replay is not None:
        return idem.replay
    # Vision — самый дорогой вызов в проекте, дороже голоса: лимит строже.
    _throttle(request, user, "label", limit=6, window=60)
    result = await assistant.read_label(user, image)
    if isinstance(result, LabelRead):
        token = request.app.state.drafts.put(
            _uid(user), result.draft, result.item_index, "этикетка", "label",
            _day_offset(request.query_params.get("days_ago")),
        )
        out = _label_json(
            result.draft, result.item_index, token, atwater=result.atwater_warning
        )
    else:
        out = _result_json(result, user, request, raw_input="этикетка")
    return idem.remember(out)


@router.post("/meals/photo")
async def log_photo(
    request: Request, user: CurrentUser, assistant: CurrentAssistant, idem: CurrentIdem,
    scans: CurrentScans,
) -> dict[str, Any]:
    """FR-34: снимок → этикетка (как `/meals/label`) или карточка блюда (§6.22).

    Тело сырое, как у этикетки. Подпись — заголовок `X-Caption`
    (percent-encoded): набранный в поле текст, «420» или «курица 150».
    """
    image = await request.body()
    if not image:
        raise bad_request("empty_image", "Фото пустое.")
    if len(image) > 4_000_000:
        raise bad_request("image_too_big", "Слишком большое фото — сними ближе.")
    if idem.replay is not None:
        return idem.replay
    _throttle(request, user, "label", limit=6, window=60)
    # Подпись — в заголовке, а не в адресе: строка запроса уезжает в журнал
    # доступа (тот же довод, что у initData в deps.py). Кириллица в заголовке
    # недопустима, поэтому клиент кодирует её encodeURIComponent.
    caption = unquote(request.headers.get("X-Caption") or "")[:200]
    offset = _day_offset(request.query_params.get("days_ago"))
    try:
        result = await assistant.read_photo(user, image, caption=caption, day_offset=offset)
    except LimitReached as exc:
        raise ApiError(
            429, "ai_limit",
            "Лимит на фото в этом месяце исчерпан — опиши словами, это работает всегда.",
        ) from exc
    drafts = request.app.state.drafts
    if isinstance(result, LabelRead):
        token = drafts.put(_uid(user), result.draft, result.item_index, "этикетка", "label", offset)
        out = _label_json(result.draft, result.item_index, token, atwater=result.atwater_warning)
    elif isinstance(result, PlateRead):
        token = drafts.put(_uid(user), result.reading, 0, "фото", "photo", offset)
        out = _plate_json(result, token)
    elif isinstance(result, BodyScanRead):
        out = await _scan_draft(request, user, scans, result)
    else:
        out = _result_json(result, user, request, raw_input="фото")
    return idem.remember(out)


@router.post("/meals/photo/commit")
async def commit_photo(
    body: dict[str, Any], user: CurrentUser, assistant: CurrentAssistant, request: Request,
    idem: CurrentIdem,
) -> dict[str, Any]:
    """FR-34: «Записать» на карточке блюда. Клиент шлёт только индексы и граммы
    (MA-INV-43); имена и вопросы — из черновика."""
    if idem.replay is not None:
        return idem.replay
    _throttle(request, user, "meals", limit=20, window=60)
    rows = body.get("components")
    if not isinstance(rows, list):
        raise bad_request("empty_plate", "Нечего записывать.")
    if len(rows) > MAX_COMPONENTS + 2:  # + масло и заправка «на готовку» (D-MA-20)
        raise bad_request("bad_grams", "Такой позиции на карточке нет.")
    picked: dict[int, float] = {}
    try:
        for row in rows:
            picked[int(row["i"])] = float(row["grams"])
    except (KeyError, TypeError, ValueError) as exc:
        raise bad_request("bad_grams", "Граммы должны быть числами.") from exc
    token = str(body.get("draft_id") or "")
    drafts = request.app.state.drafts
    draft = drafts.peek(token, _uid(user))
    if draft is None or draft.kind != "photo":
        raise bad_request("draft_gone", "Карточка устарела. Сними блюдо ещё раз.")
    reading = draft.payload
    if any(not 0 <= i < len(reading.components) for i in picked):
        raise bad_request("bad_grams", "Такой позиции на карточке нет.")
    grams = [picked.get(i) for i in range(len(reading.components))]
    try:
        text = assistant.photo_text(reading, grams)
    except ValueError as exc:
        # MA-INV-44: черновик не тронут — человек поправит и нажмёт снова.
        if not any(g is not None for g in grams):
            raise bad_request("empty_plate", "Оставь хотя бы одну позицию.") from exc
        raise bad_request(
            "bad_grams", "Граммы: от 1 до 3000 на позицию, всего до 5000."
        ) from exc
    taken = drafts.take(token, _uid(user))
    if taken is None:
        raise bad_request("draft_gone", "Карточка устарела. Сними блюдо ещё раз.")
    try:
        result = await assistant.log_photo(
            user, text, weighed=bool(body.get("weighed")), day_offset=draft.day_offset
        )
    except Exception:
        # Разбор упал — запись не состоялась; карточка должна пережить повтор.
        drafts.restore(token, taken)
        raise
    out = _result_json(result, user, request, raw_input=text, day_offset=draft.day_offset)
    return idem.remember(out)


_SCAN_REFUSAL = {
    "not_inbody": "Пока понимаю только бланки InBody — у других приборов обмен считается иначе.",
    "no_numbers": "Не нашёл на бланке вес и жир — сними его ровно и целиком.",
}


async def _scan_draft(
    request: Request, user: User, scans: BodyScanService, result: BodyScanRead,
    *, token: str | None = None,
) -> dict[str, Any]:
    scan, date_state, reason = from_reading(result.reading, user.timezone)
    if scan is None:
        return {"kind": "unknown", "message": _SCAN_REFUSAL[reason or "no_numbers"]}
    check = await scans.check(user, scan, date_state=date_state)
    if token is None:
        token = request.app.state.drafts.put(
            _uid(user), {"scan": check.scan, "date_state": date_state, "read": check.scan},
            0, "бланк", "scan",
        )
    return scan_check_json(check, token, user.timezone)


def scan_check_json(c: ScanCheck, token: str, tz: str) -> dict[str, Any]:
    return {
        "kind": "body_scan",
        "draft_id": token,
        "scan": scan_json(c.scan, tz),
        "bad": sorted(c.bad),
        "foreign": c.foreign,
        "date_ambiguous": c.date_ambiguous,
        "date_problem": c.date_problem,
        "existing": c.existing is not None,
        "needs_confirm": c.needs_confirm,
        "dropped_segments": c.dropped_segments,
    }


def _plate_json(result: PlateRead, token: str) -> dict[str, Any]:
    """Карточка блюда (§6.22): что увидели и что спросить."""
    r = result.reading
    return {
        "kind": "photo",
        "draft_id": token,
        "dish": r.dish,
        "weighed": result.weighed,
        "total_g": result.total_g,
        "components": [
            {"i": i, "name": c.name, "grams": g, "fixed": c.fixed, "note": c.note}
            for i, (c, g) in enumerate(zip(r.components, result.grams, strict=True))
        ],
        "liquid": r.liquid,
    }


def _label_json(
    draft: Any, index: int, token: str, *, atwater: bool = False, hint: bool = False
) -> dict[str, Any]:
    """Карточка этикетки: что прочитали и чего ещё ждём."""
    item = draft.items[index]
    return {
        "kind": "label",
        "draft_id": token,
        "item_name": item.name,
        "per100": {
            "kcal": round(item.kcal_per_100g, 1),
            "protein_g": round(item.protein_per_100g, 1),
            "fat_g": round(item.fat_per_100g, 1),
            "carb_g": round(item.carb_per_100g, 1),
        },
        "package_g": item.unit_weight_g,
        "serving_g": item.serving_g,
        # FR-27: Б/Ж/У не сходятся с калориями — вероятно, цифру прочитали
        # неверно. Молчать об этом значит запинить неверную плотность навсегда.
        "atwater_warning": atwater,
        "hint": hint,
    }


def _scan_from_fields(read: BodyScan, fields: Any, tz: str) -> BodyScan:
    """Прочитанное с бланка + правки человека. ValueError — правка не разобрана."""
    if not isinstance(fields, dict):
        raise ValueError("fields")
    scan = with_fields(read, fields)
    if fields.get("date") or fields.get("time"):
        local = scan.measured_at.astimezone(ZoneInfo(tz))
        d = date.fromisoformat(str(fields.get("date") or local.date().isoformat()))
        hh, mm = (int(x) for x in str(fields.get("time") or local.strftime("%H:%M")).split(":"))
        scan = replace(scan, measured_at=datetime(d.year, d.month, d.day, hh, mm,
                                                  tzinfo=ZoneInfo(tz)))
    return scan


@router.post("/body-scans/check")
async def check_scan(
    body: dict[str, Any], user: CurrentUser, request: Request, scans: CurrentScans,
) -> dict[str, Any]:
    """Перепроверка карточки после правки поля (§6.24): сверка живёт в одном месте —
    в домене, а не копией в JS."""
    token = str(body.get("draft_id") or "")
    draft = request.app.state.drafts.peek(token, _uid(user))
    if draft is None or draft.kind != "scan":
        raise bad_request("draft_gone", "Карточка устарела. Сфотографируй бланк ещё раз.")
    fields = body.get("fields") or {}
    try:
        scan = _scan_from_fields(draft.payload["read"], fields, user.timezone)
    except (TypeError, ValueError) as exc:
        raise bad_request("bad_scan", "Число не разобрал — проверь поле.") from exc
    # Дату поправили руками — вопрос к ней снят (MA-INV-55).
    state = "ok" if fields.get("date") else str(draft.payload["date_state"])
    check = await scans.check(user, scan, date_state=state)
    return scan_check_json(check, token, user.timezone)


@router.post("/body-scans")
async def save_scan(
    body: dict[str, Any], user: CurrentUser, request: Request, scans: CurrentScans,
    assistant: CurrentAssistant, idem: CurrentIdem,
) -> dict[str, Any]:
    """Сохранить замер с карточки (§6.24). Поля — правки человека поверх прочитанного."""
    if idem.replay is not None:
        return idem.replay
    token = str(body.get("draft_id") or "")
    drafts = request.app.state.drafts
    draft = drafts.peek(token, _uid(user))
    if draft is None or draft.kind != "scan":
        raise bad_request("draft_gone", "Карточка устарела. Сфотографируй бланк ещё раз.")
    fields = body.get("fields") or {}
    read: BodyScan = draft.payload["read"]
    try:
        scan = _scan_from_fields(read, fields, user.timezone)
    except (TypeError, ValueError) as exc:
        raise bad_request("bad_scan", "Числа замера не разобрал — проверь поля.") from exc
    corrected = [f for f in SCAN_FIELDS if getattr(scan, f) != getattr(read, f)]
    state = "ok" if fields.get("date") else str(draft.payload["date_state"])
    try:
        saved, updated = await scans.save(
            user, scan, confirmed=bool(body.get("confirmed")),
            corrected=corrected, date_state=state,
        )
    except ValueError as exc:
        if str(exc) == "future":
            raise bad_request("scan_future", "Дата замера в будущем — проверь дату.") from exc
        raise ApiError(400, "scan_mismatch",
                       "Отмеченные числа не сходятся с остальными — сверь с бланком.") from exc
    drafts.take(token, _uid(user))
    overview = await scans.overview(user)
    out = {
        "kind": "body_scan_saved",
        "scan": scan_json(saved, user.timezone),
        "updated": updated,
        "proposal": proposal_json(overview.proposal),
    }
    return idem.remember(out)


@router.get("/body-scans")
async def list_scans(user: CurrentUser, scans: CurrentScans) -> dict[str, Any]:
    """Всё для «Итогов → Состав тела» и настроек (§6.24)."""
    return overview_json(await scans.overview(user), user.timezone)


@router.delete("/body-scans/{scan_id}")
async def delete_scan(scan_id: int, user: CurrentUser, scans: CurrentScans) -> dict[str, Any]:
    if not await scans.delete(user, scan_id):
        raise not_found("scan_not_found", "Замер не найден.")
    return {"deleted": True}


@router.post("/body-scans/apply-goal")
async def apply_scan_goal(
    user: CurrentUser, scans: CurrentScans, idem: CurrentIdem,
) -> dict[str, Any]:
    """MA-INV-52: цели по замеру — только этой кнопкой."""
    if idem.replay is not None:
        return idem.replay
    try:
        goal = await scans.apply(user)
    except ValueError as exc:
        raise bad_request("no_scan", "Нужен свежий замер, чтобы пересчитать цели.") from exc
    return idem.remember({"goal": goal_json(goal)})


# MA-INV-69: снимок удалённого приёма на 10 минут — под «Вернуть». ponytail: память процесса API,
# перезапуск теряет «Вернуть» (удалённое удалено осознанно); потолок — мягкое удаление колонкой.
UNDO_TTL_S = 600
_TRASH: dict[str, tuple[float, Meal]] = {}


def _to_trash(meal: Meal) -> str:
    now = time.monotonic()
    for k in [k for k, (at, _) in _TRASH.items() if now - at > UNDO_TTL_S]:
        del _TRASH[k]
    token = secrets.token_urlsafe(12)
    # Копия, а не ссылка: удаление позиции правит тот же объект приёма, и «Вернуть» вернуло бы
    # уже обрезанный приём (поймано тестом 21.10.2026).
    _TRASH[token] = (now, copy.deepcopy(meal))
    return token


@router.delete("/meals/{meal_id}")
async def delete_meal(
    meal_id: int, user: CurrentUser, assistant: CurrentAssistant
) -> dict[str, Any]:
    deleted = await assistant.delete_meal(user, meal_id)
    if deleted is None or getattr(deleted, "meal", None) is None:
        raise not_found("meal_not_found", "Приём не найден.")
    stats = await assistant.today(user)
    return {"deleted": True, "undo": _to_trash(deleted.meal),
            "totals": remaining_json(stats.remaining)}


@router.post("/meals/restore")
async def restore_meal(
    body: dict[str, Any], user: CurrentUser, assistant: CurrentAssistant
) -> dict[str, Any]:
    """«Вернуть» (MA-INV-69): приём как был до удаления. Токен одноразовый и только свой."""
    entry = _TRASH.get(str(body.get("undo") or ""))
    if entry is None or time.monotonic() - entry[0] > UNDO_TTL_S or entry[1].user_id != user.id:
        raise not_found("undo_expired", "Вернуть уже нельзя — прошло слишком много времени.")
    _TRASH.pop(str(body["undo"]), None)
    result = await assistant.restore_meal(user, entry[1])
    if result is None:
        raise not_found("undo_expired", "Вернуть уже нельзя.")
    return logged_json(result, user.timezone)


@router.post("/meals/{meal_id}/items/{item_id}/fix")
async def fix_item(
    meal_id: int, item_id: int, body: dict[str, Any], user: CurrentUser,
    assistant: CurrentAssistant, request: Request,
) -> dict[str, Any]:
    """«Поправить словами» (§6.27): позиция заменяется разобранным из слов, в том же приёме."""
    text = str(body.get("text") or "").strip()
    if not text:
        raise bad_request("empty_text", "Напиши, что поправить.")
    if len(text) > 300:
        raise bad_request("text_too_long", "Слишком длинно — коротко, что это было.")
    _throttle(request, user, "ingredients", limit=10, window=60)
    result = await assistant.fix_item(user, meal_id, item_id, text)
    if result is None:
        raise bad_request("fix_failed", "Не понял, что поправить — скажи, что это было и сколько.")
    return logged_json(result, user.timezone)


@router.delete("/meals/{meal_id}/items/{item_id}")
async def delete_item(
    meal_id: int, item_id: int, user: CurrentUser, assistant: CurrentAssistant
) -> dict[str, Any]:
    """Убрать одну позицию: приём из трёх блюд не должен удаляться целиком
    из-за одной ошибки."""
    meal = await assistant.get_meal(user, meal_id)
    if meal is None or not any(i.id == item_id for i in meal.items):
        raise not_found("item_not_found", "Позиция не найдена.")
    # Снимок ДО удаления: «Вернуть» возвращает приём, каким он был (MA-INV-69).
    undo = _to_trash(meal)
    result = await assistant.remove_item(user, meal_id, item_id)
    stats = await assistant.today(user)
    # None означает «позиций не осталось, приём удалён целиком» — это успех,
    # а не 404. Отдавать ошибку на успешное действие значит врать интерфейсу.
    return {
        "meal": meal_json(result.meal, user.timezone) if result else None,
        "undo": undo,
        "totals": remaining_json(stats.remaining),
    }


def _result_json(
    result: Any, user: User, request: Request, *, raw_input: str, day_offset: int = 0
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
            _uid(user), result.draft, result.item_index, raw_input, kind, day_offset
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

    if isinstance(result, DishSaved):
        # FR-39.1: рецепт сохранён продуктом; в день не записан. Без этой ветки ответ
        # падал в «не понял», хотя продукт уже был заведён — человек решил бы, что не вышло.
        p = result.product
        return {
            "kind": "recipe",
            "name": p.display_name,
            "kcal_per_100g": round(p.density.kcal_per_100g, 1),
            "protein_per_100g": round(p.density.protein_per_100g, 1),
            "fat_per_100g": round(p.density.fat_per_100g, 1),
            "carb_per_100g": round(p.density.carb_per_100g, 1),
            "unit_weight_g": p.unit_weight_g,
            "is_estimate": p.is_estimate,
        }
    if isinstance(result, DailyStats):
        # Пользователь спросил, а не записал: отдаём сводку, не выдумывая приём.
        return {"kind": "stats", "totals": remaining_json(result.remaining)}

    reason = getattr(result, "reason", None) or "Не понял, что записать."
    if isinstance(result, Unknown) and result.say:
        return {"kind": "unknown", "message": result.say}
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
                # id нужен, чтобы замер можно было удалить: без него опечатку
                # «83,6» вместо «93,6» из интерфейса не убрать (§6.12).
                "id": log.id,
                "date": log.measured_at.astimezone(tz).date().isoformat(),
                "at": log.measured_at.astimezone(tz).strftime("%d.%m %H:%M"),
                "kg": log.weight_kg,
            }
            for log in logs
        ]
    }


@router.patch("/meals/{meal_id}/items/{item_id}")
async def adjust_item(
    meal_id: int, item_id: int, body: dict[str, Any],
    user: CurrentUser, assistant: CurrentAssistant,
) -> dict[str, Any]:
    """Поправить вес позиции (MA-INV-22).

    Снапшот масштабируется пропорционально, а не пересчитывается из каталога:
    иначе правка веса потянула бы за собой сегодняшнюю плотность продукта, и
    съеденное вчера поехало бы (INV-3).
    """
    try:
        grams = float(body["grams"])
    except (KeyError, TypeError, ValueError) as exc:
        raise bad_request("bad_grams", "Вес должен быть числом.") from exc
    if not 0 < grams <= 5000:
        raise bad_request("bad_grams", "Вес вне разумных пределов.")

    changed = await assistant.adjust_item(user, meal_id, item_id, grams)
    if changed is None:
        raise not_found("item_not_found", "Позицию не найти или у неё нет веса.")
    item, old_grams = changed
    stats = await assistant.today(user)
    meal = await assistant.get_meal(user, meal_id)
    return {
        "item": item_json(item),
        "old_grams": round(old_grams, 1),
        "meal": meal_json(meal, user.timezone) if meal else None,
        "totals": remaining_json(stats.remaining),
    }


@router.post("/weight")
async def log_weight(
    body: dict[str, Any], user: CurrentUser, assistant: CurrentAssistant, request: Request,
    idem: CurrentIdem,
) -> dict[str, Any]:
    """Запись веса одним полем, без модели (§8.3).

    Через общий ввод вес идёт текстом и стоит вызова LLM — на цифре, которую
    человек и так знает точно.
    """
    try:
        kg = float(body["weight_kg"])
    except (KeyError, TypeError, ValueError) as exc:
        raise bad_request("bad_weight", "Вес должен быть числом.") from exc

    if idem.replay is not None:
        return idem.replay
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
    return idem.remember(out)


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
    user: CurrentUser, assistant: CurrentAssistant,
    days: int = 30, limit: int = 8, by: str = "kcal",
) -> dict[str, Any]:
    """Куда уходят калории (§3.3): абсолютный вклад продуктов, не доли.

    Доли нормируются на съеденное — чем меньше ешь, тем лучше картина; такая
    метрика прячет ровно ту проблему, ради которой экран нужен (§4.1).
    """
    if not 1 <= days <= 365 or not 1 <= limit <= 30:
        raise bad_request("bad_range", "Период вне допустимого диапазона.")
    if by not in ("kcal", "protein"):
        raise bad_request("bad_metric", "Считаем по калориям или по белку.")
    rows = await assistant.contributions(user, days=days, limit=limit, by=by)
    total = sum(k for _, _, k, _ in rows)
    # AC-MA-36: если два продукта из графика сидят в одной группе дублей —
    # помечаем оба. Радар дублей там, где человек и так смотрит на расход.
    dup = await assistant.duplicate_index()
    seen: dict[int, int] = {}
    for pid, _, _, _ in rows:
        g = dup.get(pid or 0)
        if g is not None:
            seen[g] = seen.get(g, 0) + 1
    return {
        "by": by,
        "total": round(total, 1),
        "total_kcal": round(total, 1),   # старое имя: фронт может быть свежее сервера
        "items": [
            {
                "product_id": pid,
                "name": name,
                "amount": round(k, 1),
                "kcal": round(k, 1),
                "times": t,
                # Метим только когда в графике ДВА и больше из одной группы:
                # одинокая строка дублем не выглядит и подсказка была бы шумом.
                "duplicate_group": (
                    dup.get(pid or 0) if seen.get(dup.get(pid or 0, -1), 0) > 1 else None
                ),
            }
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
        raise bad_request("bad_range", "Период вне допустимого диапазона.")
    rows = await assistant.frequent(user, days=days, limit=limit)
    return {
        "items": [
            {"product_id": pid, "name": name, "grams": round(grams, 1), "times": times}
            for pid, name, grams, times in rows
        ]
    }


@router.post("/meals/{meal_id}/repeat")
async def repeat(
    meal_id: int, user: CurrentUser, assistant: CurrentAssistant, request: Request,
    idem: CurrentIdem,
) -> dict[str, Any]:
    """Повторить приём одним тапом: тот же состав, тот же вес, текущее время.

    Копируется снапшот КБЖУ, а не пересчитывается из каталога (INV-3): повтор
    обязан совпасть с оригиналом до грамма. Если каталог с тех пор поправили,
    повтор всё равно повторяет то, что было съедено, а не то, что стало в базе.
    """
    if idem.replay is not None:
        return idem.replay
    result = await assistant.repeat_meal(
        user, meal_id, day_offset=_day_offset(request.query_params.get("days_ago"))
    )
    if result is None:
        raise not_found("meal_not_found", "Приём не найден — возможно, его удалили.")
    return idem.remember(logged_json(result, user.timezone))


@router.post("/meals/{meal_id}/items/{item_id}/repeat")
async def repeat_item(
    meal_id: int, item_id: int, body: dict[str, Any], user: CurrentUser,
    assistant: CurrentAssistant, request: Request, idem: CurrentIdem,
) -> dict[str, Any]:
    """Одна позиция ещё раз, по умолчанию в сегодня (§6.25, MA-INV-67).

    День — из тела, а не с экрана: кнопка «↻ сегодня» на вчерашнем дне пишет именно в
    сегодня, а шторка даёт выбрать «тот день» явно."""
    grams: float | None = None
    if body.get("grams") is not None:
        try:
            grams = float(body["grams"])
        except (TypeError, ValueError) as exc:
            raise bad_request("bad_grams", "Вес вне разумных пределов.") from exc
        if not 0 < grams <= 5000:
            raise bad_request("bad_grams", "Вес вне разумных пределов.")
    if idem.replay is not None:
        return idem.replay
    result = await assistant.repeat_item(
        user, meal_id, item_id, grams=grams, day_offset=_day_offset(body.get("days_ago"))
    )
    if result is None:
        raise not_found("meal_not_found", "Запись не найдена — возможно, её удалили.")
    return idem.remember(logged_json(result, user.timezone))


SORTS = ("recent", "frequent", "mine", "name")


def _last_json(last: tuple[datetime, float] | None, user: User) -> dict[str, Any]:
    """«Последний раз» для строки каталога и шторки (§6.25): день считается по поясу и
    границе дня человека, иначе после полуночи по UTC «вчера» стало бы «сегодня»."""
    if last is None:
        return {"last_date": None, "last_time": None, "last_days_ago": None, "last_grams": None}
    at, grams = last
    start = day_start(user.ui_prefs)
    day = local_date(at, user.timezone, start)
    today = local_date(datetime.now(UTC), user.timezone, start)
    return {
        "last_date": day.isoformat(),
        "last_time": at.astimezone(ZoneInfo(user.timezone)).strftime("%H:%M"),
        "last_days_ago": (today - day).days,
        "last_grams": round(grams, 1),
    }


@router.get("/products")
async def products(
    user: CurrentUser, assistant: CurrentAssistant,
    q: str | None = None, offset: int = 0, limit: int = 20, sort: str = "name",
) -> dict[str, Any]:
    """Каталог страницами: полный список нельзя — их больше двух сотен (§7.2).
    `sort=recent|frequent` — только съеденное этим человеком (§6.25)."""
    if offset < 0 or limit < 1 or limit > 100:
        raise bad_request("bad_paging", "Страница вне диапазона.")
    if sort not in SORTS:
        raise bad_request("bad_sort", "Сортировка: recent, frequent, mine или name.")
    items, total, uses, grams, last = await assistant.catalog_page(
        user, offset=offset, limit=limit, query=(q or "").strip() or None, sort=sort
    )
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [
            _stated_json(p, user) if isinstance(p, StatedUse)
            else product_json(p, uses.get(p.id or 0, 0), grams.get(p.id or 0))
            | _last_json(last.get(p.id or 0), user)
            for p in items
        ],
    }


def _stated_json(s: StatedUse, user: User) -> dict[str, Any]:
    """«Свои цифры» в «Недавних» (§6.25): ккал порции, а не на 100 г; повтор — снимок позиции
    `meal_id`/`item_id` (MA-INV-67). `id` строкой — не путать с product_id."""
    return {
        "stated": True,
        "id": f"s{s.item_id}",
        "name": s.name,
        "meal_id": s.meal_id,
        "item_id": s.item_id,
        "grams": round(s.grams, 1),
        "kcal": round(s.macros.kcal, 1),
        "protein_g": round(s.macros.protein, 1),
        "fat_g": round(s.macros.fat, 1),
        "carb_g": round(s.macros.carb, 1),
        "uses": s.uses,
    } | _last_json((s.last_at, s.grams), user)


@router.get("/sets")
async def sets(user: CurrentUser, assistant: CurrentAssistant) -> dict[str, Any]:
    """FR-39.5: свои наборы для «Моё». Состав — чтобы было видно, что внутри, без захода."""
    return {
        "items": [
            {
                "name": m.template_name,
                "kcal": round(m.totals.kcal, 1),
                "protein_g": round(m.totals.protein, 1),
                "fat_g": round(m.totals.fat, 1),
                "carb_g": round(m.totals.carb, 1),
                "items": [
                    {
                        "name": i.name_snapshot,
                        "grams": round(i.grams, 1),
                        "kcal": round(i.macros.kcal, 1),
                        # Б/Ж/У позиции — чтобы лист состава считал итог вживую при правке
                        # граммов (предпросмотр; записанное пересчитывает сервер, MA-INV-68).
                        "protein_g": round(i.macros.protein, 2),
                        "fat_g": round(i.macros.fat, 2),
                        "carb_g": round(i.macros.carb, 2),
                    }
                    for i in m.items
                ],
            }
            for m in await assistant.sets(user)
        ]
    }


_INT4 = 2**31 - 1
# id в пути новых маршрутов: за пределом int4 asyncpg падал 500; так — 400 через обработчик
# кривого запроса (main_api.malformed).
DbId = Annotated[int, Path(ge=1, le=_INT4)]


def _id(v: Any) -> int:
    """Целый id из JSON. bool — подкласс int в Python, «1.5» и «"0"» — не id: всё это отказ,
    а не тихое int(1.5) = 1. Верхняя граница — int4, иначе asyncpg падает 500 (ревью 15.10)."""
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= _INT4:
        raise ValueError("id должен быть целым")
    return v


def _picks(raw: Any, *, max_items: int, max_grams: float) -> list[SetPick]:
    """Позиции набора/рецепта: ровно одно из `src`/`product_id`, граммы в границах.
    Любая кривизна — ValueError, ловится как 400 (не 500)."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= max_items:
        raise ValueError("состав пуст или слишком длинный")
    picks: list[SetPick] = []
    for x in raw:
        if not isinstance(x, dict):
            raise ValueError("позиция — не объект")
        g = x.get("grams")
        if isinstance(g, bool) or not isinstance(g, (int, float)):
            raise ValueError("граммы — не число")
        grams = float(g)
        if not 0 < grams <= max_grams:  # NaN и бесконечность сюда не проходят
            raise ValueError("граммы вне границ")
        src, pid = x.get("src"), x.get("product_id")
        if (src is None) == (pid is None):
            raise ValueError("ровно одно из src/product_id")
        name = x.get("name")
        picks.append(SetPick(
            grams=grams,
            src=_id(src) if src is not None else None,
            product_id=_id(pid) if pid is not None else None,
            name=str(name)[:255] if isinstance(name, str) else None,
        ))
    return picks


@router.post("/meals/{meal_id}/template")
async def save_set(
    meal_id: DbId, body: dict[str, Any], user: CurrentUser, assistant: CurrentAssistant
) -> dict[str, Any]:
    """FR-39.2: дать приёму имя набора прямо из карточки «Дня»."""
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 64:
        raise bad_request("bad_name", "Имя набора — от 1 до 64 символов.")
    meal = await assistant.remember_dish(user, meal_id, name)
    if meal is None:
        raise bad_request("bad_set", "Нужно хотя бы две позиции, и у каждой — вес.")
    return {"name": meal.template_name}


@router.post("/sets/log")
async def log_set(
    body: dict[str, Any], user: CurrentUser, assistant: CurrentAssistant, request: Request,
    idem: CurrentIdem,
) -> dict[str, Any]:
    """FR-39.6: записать набор с правкой состава. Клиент шлёт ЧТО и СКОЛЬКО, цифры считает
    сервер — граница доверия: присланным ккал не верим, их тут даже нет в теле."""
    _throttle(request, user, "meals", limit=20, window=60)
    name = str(body.get("name") or "").strip()
    if not name:
        raise bad_request("bad_set", "Нужны имя набора и состав.")
    try:
        picks = _picks(body.get("items"), max_items=30, max_grams=5000)
    except (TypeError, ValueError, OverflowError) as exc:
        raise bad_request("bad_set", "Состав набора не разобрать.") from exc
    if idem.replay is not None:
        return idem.replay
    result = await assistant.log_set(
        user, name, picks, update=bool(body.get("update")),
        day_offset=_day_offset(body.get("days_ago")),
    )
    if result is None:
        raise bad_request("bad_set", "Набор не найден или его состав изменился — открой заново.")
    return idem.remember(_result_json(result, user, request, raw_input=name))


async def _ingredients_json(
    assistant: Assistant, items: list[RecipeItem], skipped: list[str]
) -> dict[str, Any]:
    out = []
    for it in items:
        p = await assistant.get_product(it.product_id or 0)
        if p is not None:
            out.append({"product": product_json(p, 0), "grams": round(it.grams, 1)})
    return {"items": out, "skipped": skipped}


@router.post("/ingredients/parse")
async def ingredients_parse(
    body: dict[str, Any], user: CurrentUser, assistant: CurrentAssistant, request: Request,
) -> dict[str, Any]:
    """FR-39.8: свой ингредиент текстом в листе состава. Разбор моделью → каталог; в состав
    ничего не пишется — лист добавит строки, сохранит человек."""
    text = str(body.get("text") or "").strip()
    if not text:
        raise bad_request("empty_text", "Напиши, что добавить.")
    if len(text) > 300:
        raise bad_request("text_too_long", "Слишком длинно — по одному-два продукта.")
    _throttle(request, user, "ingredients", limit=10, window=60)
    items, skipped = await assistant.ingredients_from_text(text)
    return await _ingredients_json(assistant, items, skipped)


@router.post("/ingredients/label")
async def ingredients_label(
    request: Request, user: CurrentUser, assistant: CurrentAssistant,
) -> dict[str, Any]:
    """FR-39.8: ингредиент по фото этикетки (FR-27). Тело — само фото, как у /meals/label."""
    image = await request.body()
    if not image:
        raise bad_request("empty_image", "Фото пустое.")
    if len(image) > 4_000_000:
        raise bad_request("image_too_big", "Слишком большое фото — сними ближе.")
    _throttle(request, user, "label", limit=6, window=60)
    item = await assistant.ingredient_from_label(image)
    if item is None:
        raise bad_request(
            "label_unread", "Не разобрал этикетку — сфоткай таблицу КБЖУ ближе и чётче."
        )
    return await _ingredients_json(assistant, [item], [])


@router.get("/products/{product_id}")
async def product(
    product_id: int, user: CurrentUser, assistant: CurrentAssistant
) -> dict[str, Any]:
    p = await assistant.get_product(product_id)
    if p is None:
        raise not_found("product_not_found", "Продукт не найден.")
    counts = await assistant.usage_of(user, product_id)
    tz = ZoneInfo(user.timezone)
    # §6.8: «где я это ел» — единственный способ понять, откуда взялась
    # плотность, и стоит ли её править.
    history = await assistant.product_history(user, product_id, limit=5)
    grams = [g for _, g, _ in history if g > 0]
    return {
        "product": product_json(
            p, counts, sorted(grams)[len(grams) // 2] if grams else None
        ) | _last_json(await assistant.last_use_of(user, product_id), user),
        "recent_items": [
            {
                "at": at.astimezone(tz).strftime("%d.%m %H:%M"),
                "grams": round(g, 1),
                "kcal": round(k, 1),
            }
            for at, g, k in history
        ],
        # FR-39.7: у рецепта — состав. None при source=recipe значит «состава нет» (рецепт до
        # 15.10.2026), и карточка предлагает собрать его заново.
        "recipe": _recipe_json(await assistant.recipe_of(product_id))
        if p.source == "recipe" else None,
    }


def _recipe_json(r: Recipe | None) -> dict[str, Any] | None:
    if r is None:
        return None
    return {
        "yield_g": r.yield_g,
        "total_g": round(sum(i.grams for i in r.items), 1),
        "items": [
            {
                "name": i.name,
                "grams": i.grams,
                "product_id": i.product_id,
                # Текущая плотность ингредиента ПОСЛЕ того же gate, что применит сервер, —
                # предпросмотр в листе совпадает с результатом (MA-INV-68).
                "kcal_per_100g": round(g.kcal_per_100g, 2),
                "protein_per_100g": round(g.protein_per_100g, 2),
                "fat_per_100g": round(g.fat_per_100g, 2),
                "carb_per_100g": round(g.carb_per_100g, 2),
            }
            for i in r.items
            for g in [gate_density(i.density, alcohol=i.alcohol or is_alcoholic(i.name))]
        ],
    }


@router.put("/products/{product_id}/recipe")
async def edit_recipe(
    product_id: DbId, body: dict[str, Any], user: CurrentUser, assistant: CurrentAssistant,
    request: Request,
) -> dict[str, Any]:
    """FR-39.7: новый состав рецепта. Клиент шлёт ЧТО и СКОЛЬКО, плотность считает сервер."""
    _throttle(request, user, "products", limit=20, window=60)
    try:
        picks = _picks(body.get("items"), max_items=50, max_grams=MAX_BATCH_G)
        y = body.get("yield_g")
        if y is not None and (isinstance(y, bool) or not isinstance(y, (int, float))):
            raise ValueError("вес готового — не число")
        yield_g = float(y) if y else None
        if yield_g is not None and not 0 < yield_g <= MAX_YIELD_G:
            raise ValueError("вес готового вне границ")
    except (TypeError, ValueError, OverflowError) as exc:
        raise bad_request("bad_recipe", "Состав рецепта не разобрать.") from exc
    try:
        updated = await assistant.edit_recipe(product_id, picks, yield_g=yield_g)
    except RecipeRejected as exc:
        raise bad_request("bad_recipe", str(exc)) from exc
    if updated is None:
        # Свой код: аппка по нему перезагружает карточку, а не открывает старый состав снова.
        raise bad_request(
            "recipe_stale", "Рецепт не найден или его состав изменился — открой карточку заново."
        )
    return {"product": product_json(updated, await assistant.usage_of(user, product_id))}


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
        answer = await service.ask(user, question, clean_history(body.get("history")))
    except LimitReached as exc:
        raise ApiError(
            429, "coach_limit",
            "Бюджет ассистента на этот месяц исчерпан. Он вернётся первого числа.",
        ) from exc
    return {
        "text": answer.text,
        "spent_usd": round(answer.spent_usd, 4),
        "limit_usd": answer.limit_usd,
        # AC-MA-40: у названных продуктов — кнопка «Записать» с своей граммовкой.
        "actions": [
            {"product_id": a.product_id, "name": a.name, "grams": round(a.grams, 1)}
            for a in answer.actions
        ],
        # §7.2: дисклеймер константой с сервера, а не текстом во фронте —
        # иначе он разъедется между ботом и аппкой.
        "disclaimer": "Это не медицинский совет. Считает по твоим записям за 30 дней.",
    }


@router.post("/coach/stream")
async def coach_stream(
    body: dict[str, Any], user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StreamingResponse:
    """Тот же вопрос, но ответ приходит кусками (§6.21.1).

    MA-INV-41: всё, что может отказать, проверяется ДО первого байта — после
    отправки заголовков статус уже не поменять, и отказ пришлось бы слать
    событием, а фронт разбирал бы две формы одной ошибки.
    """
    question = str(body.get("question") or "").strip()
    if not question:
        raise bad_request("empty_question", "Пустой вопрос.")
    if len(question) > 500:
        raise bad_request("question_too_long", "Слишком длинный вопрос.")
    _throttle(request, user, "coach", limit=6, window=60)
    history = clean_history(body.get("history"))  # недоверенный ввод (MA-INV-66)

    container: Container = request.app.state.container
    check = container.coach_for(session)
    if check is None:
        raise unavailable("coach_off", "Ассистент недоступен: не настроен ключ.")
    try:
        await check.ensure_budget()
    except LimitReached as exc:
        raise ApiError(
            429, "coach_limit",
            "Бюджет ассистента на этот месяц исчерпан. Он вернётся первого числа.",
        ) from exc

    async def events() -> AsyncIterator[str]:
        # MA-INV-42: своя сессия. Тело стрима отдаётся уже после возврата
        # обработчика, а инструменты ходят в БД посреди потока — закрытая
        # сессия убила бы ответ на середине.
        async with container.db.session_factory() as own:
            service = container.coach_for(own)
            if service is None:  # ключ пропал между проверкой и потоком
                yield _sse({"kind": "error", "code": "coach_off",
                            "message": "Ассистент недоступен."})
                return
            try:
                async for chunk in service.ask_stream(user, question, history):
                    yield _sse(_chunk_json(chunk))
                await own.commit()
            except Exception:
                log.exception("поток ассистента оборвался")
                # Заголовки давно ушли: отказ отдаём событием, а не кодом.
                yield _sse({"kind": "error", "code": "stream_failed",
                            "message": "Ответ оборвался. Попробуй ещё раз."})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # Caddy соседа буферизует ответы по умолчанию: без этого поток доедет
        # до человека одним куском в конце, то есть стриминга не будет вовсе.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _sse(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _chunk_json(chunk: Any) -> dict[str, Any]:
    if chunk.kind == "end":
        return {
            "kind": "end",
            "text": chunk.text,
            "spent_usd": round(chunk.spent_usd, 4),
            "limit_usd": chunk.limit_usd,
            "actions": [
                {"product_id": a.product_id, "name": a.name, "grams": round(a.grams, 1)}
                for a in chunk.actions
            ],
        }
    return {"kind": chunk.kind, "text": chunk.text}


@router.get("/coach/budget")
async def coach_budget(
    user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Сколько бюджета осталось. Нужен настройкам: без счётчика строка «Лимит»
    была бы декоративной, а таких мы не выкатываем (MA-INV-31)."""
    container: Container = request.app.state.container
    spend = container.spend_for(session)
    return {
        "spent_usd": round(await spend.spent_this_month(), 4),
        # Из базы, как и проверка бюджета (MA-INV-65): иначе полоска во вкладке AI
        # показывала бы потолок из окружения после того, как владелец его поднял.
        "limit_usd": await spend.month_limit(container.settings.ai_month_limit_usd),
        "available": container.coach_port is not None,
    }


@router.post("/bug")
async def report_bug(
    body: dict[str, Any], user: CurrentUser, assistant: CurrentAssistant, request: Request
) -> dict[str, Any]:
    """Багрепорт из аппки (FR-26, §6.5).

    Вместе с текстом уходят последние приёмы — без них «посчитало не так»
    невозможно воспроизвести, а именно такие сообщения и приходят.
    """
    note = str(body.get("note") or "").strip()
    if not note:
        raise bad_request("empty_note", "Опиши, что пошло не так.")
    if len(note) > 2000:
        raise bad_request("note_too_long", "Слишком длинно — оставь суть.")
    _throttle(request, user, "bug", limit=5, window=300)
    await assistant.report_bug(user, note)
    return {"ok": True}


@router.get("/settings")
async def settings(
    user: CurrentUser, assistant: CurrentAssistant, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Всё, что показывает экран настроек, одним запросом (§6.5)."""
    container: Container = request.app.state.container
    goal = await assistant.current_goal(user)
    spend = container.spend_for(session)
    spent = await spend.spent_this_month()
    limit = await spend.month_limit(container.settings.ai_month_limit_usd)
    out: dict[str, Any] = {
        "goal": goal_json(goal),
        "timezone": user.timezone,
        "prefs": sanitize(user.ui_prefs),
        # Траты и потолок видят все: иначе отказ ассистента по лимиту нечем объяснить.
        "coach": {
            "model": container.settings.coach_model,
            "limit_usd": limit,
            "spent_usd": round(spent, 4),
            "available": container.coach_port is not None,
        },
    }
    if user.role == OWNER:
        # Менять — только владельцу (MA-INV-65); остальным группа не рисуется вовсе.
        out["ai_limit"] = {"usd": limit, "options": list(LIMIT_OPTIONS)}
    return out


OWNER = "owner"
LIMIT_OPTIONS = (10, 15, 20, 25, 30, 50)
LIMIT_RANGE = (1.0, 100.0)


@router.patch("/settings/ai-limit")
async def edit_ai_limit(
    body: dict[str, Any], user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Месячный потолок трат на модель — только владелец (MA-INV-65).

    Роль проверяется здесь, а не в интерфейсе: скрытая кнопка защитой не считается.
    """
    if user.role != OWNER:
        raise ApiError(403, "owner_only", "Лимит меняет только владелец.")
    try:
        usd = float(body.get("usd"))
    except (TypeError, ValueError):
        usd = float("nan")
    if not LIMIT_RANGE[0] <= usd <= LIMIT_RANGE[1]:     # NaN сюда тоже не пройдёт
        raise bad_request("bad_limit", "Лимит — от $1 до $100 в месяц.")
    container: Container = request.app.state.container
    spend = container.spend_for(session)
    await spend.set_month_limit(usd)
    return {"limit_usd": usd, "spent_usd": round(await spend.spent_this_month(), 4)}


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
        asked = Goal(
            daily_kcal=float(body.get("kcal", current.daily_kcal)),
            protein_g=float(protein if protein is not None else current.protein_g),
            fat_g=float(body.get("fat_g", current.fat_g)),
            carb_g=float(body.get("carb_g", current.carb_g)),
            target=str(body.get("target", current.target)),
        )
    except (TypeError, ValueError) as exc:
        raise bad_request("bad_goal", "Цели должны быть числами.") from exc

    if not 800 <= asked.daily_kcal <= 6000:
        raise bad_request("kcal_out_of_range", "Калорийность вне разумных пределов.")
    if not all(0 <= v <= 600 for v in (asked.protein_g, asked.fat_g, asked.carb_g)):
        raise bad_request("macros_out_of_range", "Макросы вне разумных пределов.")
    if asked.target not in ("cut", "maintain", "bulk"):
        raise bad_request("bad_target", "Неизвестный режим.")

    # MA-INV-54: форма шлёт все четыре числа, но менял человек обычно одно. Разницу
    # поглощает тот же макрос, что и в боте (FR-16.1), и цель остаётся согласованной.
    base = replace(current, target=asked.target)
    goal, changed = consistent_goal(
        base, kcal=asked.daily_kcal, protein_g=asked.protein_g,
        fat_g=asked.fat_g, carb_g=asked.carb_g,
    )
    if changed == "none" and asked.target == current.target:
        return {"goal": goal_json(current), "changed": "none", "moved": {}}
    if not 800 <= goal.daily_kcal <= 6000:
        raise bad_request("kcal_out_of_range",
                          "С такими макросами калории выходят за разумные пределы.")
    await users.set_goal(_uid(user), goal)
    trend = await assistant.weight_trend(user, 1)
    weight = trend.logs[0].weight_kg if trend.logs else None
    return {
        "goal": goal_json(goal),
        "changed": changed,
        "moved": {
            k: round(getattr(goal, f) - getattr(current, f))
            for k, f in (("kcal", "daily_kcal"), ("protein_g", "protein_g"),
                         ("fat_g", "fat_g"), ("carb_g", "carb_g"))
            if round(getattr(goal, f) - getattr(current, f))
        },
        # Ниже 1.2 г/кг на сушке — потеря мышц; не запрещаем, но говорим.
        "low_protein": bool(weight and goal.protein_g < 1.2 * weight),
    }


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




@dataclass
class IdemSlot:
    """Занятый ключ идемпотентности на время одного запроса."""

    store: Any
    user_id: int
    key: str
    replay: dict[str, Any] | None = None

    def remember(self, response: dict[str, Any]) -> dict[str, Any]:
        self.store.put(self.user_id, self.key, response)
        return response


async def idempotent(
    request: Request,
    user: CurrentUser,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AsyncIterator[IdemSlot]:
    """MA-INV-10: каждый пишущий POST идемпотентен по `Idempotency-Key` (§7.5).

    Зависимостью, а не проверкой внутри обработчика: пишущих эндпоинтов шесть,
    и «забыли повесить» здесь стоит удвоенных калорий. Так уже было — голос
    принимал заголовок в сигнатуре и нигде его не использовал, защита
    существовала только на бумаге.

    Ключ обязателен: без него двойной тап снова пишет дважды, а молчаливое
    «работает и так» скрывает дыру до первой удвоенной записи.
    """
    if not idempotency_key:
        raise bad_request(
            "no_idempotency_key", "Запрос без ключа записи. Переоткрой аппку."
        )
    store = request.app.state.idempotency
    uid = _uid(user)
    state = store.claim(uid, idempotency_key)
    if state == "in_flight":
        # Первый запрос ещё идёт. Фронт на этот код молчит: кнопка уже «пишет».
        raise ApiError(409, "in_flight", "Уже записываю.")
    slot = IdemSlot(store, uid, idempotency_key)
    if state == "replay":
        remembered = store.get(uid, idempotency_key)
        slot.replay = remembered if isinstance(remembered, dict) else None
    try:
        yield slot
    finally:
        # Обработчик мог упасть или отказать по валидации — ключ обязан
        # отпуститься, иначе повторить запись нельзя до конца TTL. После
        # `remember` вызов холостой: ключ уже снят с работы.
        store.release(uid, idempotency_key)


CurrentIdem = Annotated[IdemSlot, Depends(idempotent)]


def _throttle(request: Request, user: User, bucket: str, *, limit: int, window: float) -> None:
    if not request.app.state.limiter.allow(_uid(user), bucket, limit=limit, window=window):
        raise ApiError(429, "rate_limited", "Слишком часто. Подожди немного.")
