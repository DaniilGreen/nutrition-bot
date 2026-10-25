"""Хендлеры aiogram. Тонкий слой: ввод -> Assistant -> форматтер.

UX: постоянное reply-меню, командное меню, «печатает…».
Отмена: контекстная (inline на свежей записи) — без вопроса; меню «Отменить» — выбор.
Каталог: /products → правка КБЖУ. Профиль: /profile → автоцель по Mifflin.
Надёжность: глобальный error-handler ловит падения (в т.ч. AI) и отвечает юзеру.
"""

from __future__ import annotations

import copy
import logging
import math
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from typing import TYPE_CHECKING, Literal, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichMessage,
    Message,
    ReplyKeyboardMarkup,
)
from openai import RateLimitError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.assistant import BODY_WEIGHT_RE, Assistant
from app.application.body_scans import BodyScanService, from_reading
from app.application.coach import LimitReached
from app.application.dto import Unit
from app.application.entities import Meal, MealItem, User
from app.application.meal_service import (
    SessionRejected,
    SnapshotStale,
    items_named,
    meal_fingerprint,
)
from app.application.ports import UserRepo
from app.application.results import (
    BodyScanRead,
    ClarificationNeeded,
    CorrectionFailed,
    DailyStats,
    DayView,
    DishSaved,
    ItemAdjusted,
    ItemPickNeeded,
    ItemRemoved,
    LabelRead,
    MealCorrected,
    MealLogged,
    MealSession,
    PlateRead,
    Result,
    Unknown,
    VariantClarificationNeeded,
    WeightLogged,
    WeightTrend,
)
from app.bot import coach_chat
from app.bot.dashboard_image import render_weight_chart  # единственный уцелевший PNG — график веса
from app.bot.formatters import (  # текстовые подсказки внутри диалога (не экраны)
    SCAN_REFUSAL,
    format_goal_proposal,
    format_photo_card,
    format_result,
    format_scan_card,
    format_set_saved,
)
from app.bot.keyboards import (
    BTN_BUG,
    BTN_FREQUENT,
    BTN_GOAL,
    BTN_HISTORY,
    BTN_PROFILE,
    BTN_TODAY,
    BTN_UNDO,
    BTN_WEEK,
    BTN_WEIGHT,
    DAY_DEL_PREFIX,
    DAY_NAV_PREFIX,
    DAY_REP_PREFIX,
    DAY_REPEAT_MODE,
    DISH_SAVE_PREFIX,
    DISH_SESSION,
    FIX_PICK_PREFIX,
    FREQ_PREFIX,
    FREQ_TOP,
    GOAL_TARGET_PREFIX,
    ITEM_WEIGHT_PREFIX,
    LABEL_G_PREFIX,
    ONBOARD_PREFIX,
    PHOTO_PREFIX,
    PROD_RENAME_PREFIX,
    RESTORE_PREFIX,
    SCAN_PREFIX,
    WIZ_CANCEL,
    activity_keyboard,
    card_meal_id,
    day_nav_keyboard,
    fix_pick_keyboard,
    frequent_keyboard,
    goal_target_keyboard,
    goal_type_keyboard,
    half_up,
    item_pick_keyboard,
    item_weight_keyboard,
    label_grams_keyboard,
    location_request_keyboard,
    main_menu_keyboard,
    meal_actions_keyboard,
    onboarding_intro_keyboard,
    open_in_app,
    photo_keyboard,
    portion_keyboard,
    prod_edit_keyboard,
    products_fix_keyboard,
    restore_button,
    scan_apply_keyboard,
    scan_keyboard,
    set_key,
    sex_keyboard,
    undo_picker_keyboard,
    variant_keyboard,
    weighable,
)
from app.bot.states import MealStates, ProfileStates
from app.bot.ui import gather, screens
from app.bot.ui.kit import plural
from app.domain.goals import (
    Goal,
    Remaining,
    goal_from_macros,
    goal_from_profile,
    rebalance,
    set_macro,
)
from app.domain.label_portion import label_grams
from app.domain.plate import Component
from app.domain.plate import split as plate_split
from app.domain.plausibility import MAX_PORTION_G, MIN_PORTION_G
from app.domain.portion_answer import parse as parse_portion
from app.domain.timeframe import tz_from_longitude, tz_from_utc_offset
from app.domain.value_objects import Density
from app.infrastructure.openai_compat import out_of_credits

if TYPE_CHECKING:
    from app.container import Container

router = Router()
logger = logging.getLogger(__name__)
_T = TypeVar("_T")


# --- надёжность: единый перехват ошибок ---------------------------------------


NO_CREDITS_ALERT_EVERY_S = 6 * 3600
_no_credits_alerted_at: float | None = None


async def _alert_owner_no_credits(bot: Bot, container: object | None) -> None:
    """Владельцу — одно сообщение о пустом балансе, не чаще раза в 6 часов (NFR-4).

    ponytail: отметка в памяти процесса — после рестарта бота придёт ещё одно. Потолок —
    лишнее сообщение на деплой, хранить отметку в базе ради этого не стоит."""
    global _no_credits_alerted_at
    now = time.monotonic()
    if container is None or (
        _no_credits_alerted_at is not None
        and now - _no_credits_alerted_at < NO_CREDITS_ALERT_EVERY_S
    ):
        return
    _no_credits_alerted_at = now
    async with container.db.session_factory() as session:  # type: ignore[attr-defined]
        _, users = container.assistant_for(session)  # type: ignore[attr-defined]
        owners = [u for u in await users.list_active() if u.role == "owner"]
    for owner in owners:
        with suppress(Exception):
            await bot.send_message(
                owner.telegram_id,
                "🔌 <b>Закончился баланс OpenAI</b>\n\nРазбор еды, голос и фото не работают, "
                "пока не пополнишь: platform.openai.com → Settings → Billing.",
            )


@router.errors()
async def on_error(event: ErrorEvent, bot: Bot, container: object | None = None) -> bool:
    logger.exception("Unhandled error in handler", exc_info=event.exception)
    target = event.update.message
    if target is None and event.update.callback_query is not None:
        maybe = event.update.callback_query.message
        target = maybe if isinstance(maybe, Message) else None
    if out_of_credits(event.exception):
        with suppress(Exception):
            await _alert_owner_no_credits(bot, container)
    if target is not None:
        if out_of_credits(event.exception):
            text = ("🔌 Нейронка временно недоступна. Запиши чуть позже — "
                    "владелец уже знает.")
        elif isinstance(event.exception, RateLimitError):  # баг #16: 429 от OpenAI (лимит TPM)
            text = "⏳ ИИ перегружен. Подожди ~10 секунд и повтори — данные не потеряны."
        else:
            text = "⚠️ Что-то пошло не так. Попробуй ещё раз через пару секунд."
        with suppress(Exception):
            await target.answer(text)
    return True


# --- команды ------------------------------------------------------------------


ONBOARD_PROMPT = (
    "Сначала настроим твою <b>дневную норму</b> — это 30 секунд "
    "(пол, вес, рост, возраст, цель). Так остаток будет точным."
)


@router.message(CommandStart())
async def on_start(message: Message, app_user: User, users: UserRepo, state: FSMContext) -> None:
    await state.clear()
    onboarded = app_user.onboarded_at is not None
    await _rich(message, screens.start(onboarded=onboarded), main_menu_keyboard())
    if not onboarded and app_user.id is not None:
        # первый запуск (FR-24/D-12): предложить настроить профиль-визард
        sent = await message.answer(ONBOARD_PROMPT, reply_markup=onboarding_intro_keyboard())
        await state.update_data(intro_msg_id=sent.message_id)  # #7: погасить при вводе еды


@router.callback_query(F.data == f"{ONBOARD_PREFIX}:setup")
async def on_onboard_setup(
    callback: CallbackQuery, app_user: User, users: UserRepo, state: FSMContext
) -> None:
    if app_user.id is not None:
        await users.set_onboarded(
            app_user.id
        )  # отмечаем сразу — визард можно не доводить (не зацикливать /start)
    if isinstance(callback.message, Message):
        with suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=None)  # убрать кнопки интро
        await _start_profile(callback.message, state)
        await state.update_data(onboarding=True)  # по завершении покажем «запиши первый приём»
    await callback.answer()


@router.callback_query(F.data == f"{ONBOARD_PREFIX}:skip")
async def on_onboard_skip(
    callback: CallbackQuery, app_user: User, users: UserRepo, state: FSMContext
) -> None:
    await state.clear()
    if app_user.id is not None:
        await users.set_onboarded(app_user.id)
    if isinstance(callback.message, Message):
        with suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            "Ок! Пока считаю остаток от 2000 ккал/день. Настроить позже — кнопка ⚙️ Профиль.\n"
            "Запиши, что съел, например «куриная грудка 250 г». Полная инструкция — /help",
            reply_markup=main_menu_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == WIZ_CANCEL)
async def on_wizard_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """«✖️ Отмена» в любом визарде (профиль/вариант) — сброс FSM без записи (FR-17)."""
    await state.clear()
    if isinstance(callback.message, Message):
        with suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            "Ок, отменил. Меню — снизу 👇", reply_markup=main_menu_keyboard()
        )
    await callback.answer()


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    await _rich(message, screens.help_(), main_menu_keyboard())


def _resolve_tz_arg(arg: str) -> str | None:
    """«+7»/«7»/«-5» → Etc/GMT∓N (UTC-офсет), либо имя IANA. None — не распознал/невалидно."""
    arg = arg.strip()
    try:
        tzname = tz_from_utc_offset(int(arg))
    except ValueError:
        tzname = arg  # не число — пробуем как имя зоны (Asia/Ho_Chi_Minh)
    with suppress(KeyError, ValueError, ZoneInfoNotFoundError):
        ZoneInfo(tzname)
        return tzname
    return None


@router.message(Command("timezone"))
async def cmd_timezone(
    message: Message, command: CommandObject, app_user: User, users: UserRepo
) -> None:
    arg = (command.args or "").strip()
    if arg:
        tzname = _resolve_tz_arg(arg)
        if tzname is None:
            await message.answer(
                "Не понял пояс. Примеры: <code>/timezone +7</code>, "
                "<code>/timezone Asia/Ho_Chi_Minh</code> — или пришли 📍 геолокацию."
            )
            return
        if app_user.id is not None:
            await users.set_timezone(app_user.id, tzname)
        await message.answer(
            f"🕒 Часовой пояс обновлён: <b>{escape(tzname, quote=False)}</b>",
            reply_markup=main_menu_keyboard(),
        )
        return
    await message.answer(
        f"🕒 Часовой пояс: <b>{escape(app_user.timezone, quote=False)}</b>\n"
        "Уточни «день» для статистики: пришли 📍 геолокацию кнопкой ниже, "
        "или вручную — <code>/timezone +7</code> (твой UTC-офсет).",
        reply_markup=location_request_keyboard(),
    )


@router.message(F.location)
async def on_location(message: Message, app_user: User, users: UserRepo) -> None:
    """FR-23: 📍-локация → часовой пояс из долготы (round(lon/15) → Etc/GMT∓N)."""
    loc = message.location
    if loc is None or app_user.id is None:
        return
    tzname = tz_from_longitude(loc.longitude)
    await users.set_timezone(app_user.id, tzname)
    await message.answer(
        f"🕒 Часовой пояс обновлён: <b>{escape(tzname, quote=False)}</b>",
        reply_markup=main_menu_keyboard(),
    )


# Слова, которыми юзер зовёт макрос. Голосом приходит и «белки», и «углеводы», и одна буква.
_MACRO_WORDS = {
    "б": "protein", "белок": "protein", "белки": "protein", "белка": "protein",
    "ж": "fat", "жир": "fat", "жиры": "fat", "жира": "fat",
    "у": "carb", "угл": "carb", "углевод": "carb", "углеводы": "carb", "углеводов": "carb",
}  # fmt: skip


def parse_macro_arg(raw: str) -> tuple[str, float] | None:
    """«белок 180» · «б180» · «жиры 70,5» → ("protein", 180.0). Не распознали — None (FR-16.1)."""
    m = re.fullmatch(r"([а-яёa-z]+)\s*(\d+(?:[.,]\d+)?)", raw.strip().casefold())
    if not m:
        return None
    macro = _MACRO_WORDS.get(m.group(1))
    return (macro, float(m.group(2).replace(",", "."))) if macro else None


@router.message(Command("goal"))
async def on_goal(
    message: Message,
    command: CommandObject,
    app_user: User,
    users: UserRepo,
    assistant: Assistant,
) -> None:
    assert app_user.id is not None
    try:
        nums = [float(a) for a in (command.args or "").strip().replace(",", ".").split()]
    except ValueError:
        nums = []

    current = await assistant.current_goal(app_user)
    named = parse_macro_arg(command.args or "")
    if named:
        # Режим 3: двигаем ОДИН макрос, калорийность держим — разницу съедает другой (FR-16.1).
        macro, grams = named
        goal = set_macro(current, macro, grams)  # type: ignore[arg-type]
    elif len(nums) == 1:
        # Режим 1: задаём КАЛОРИИ → макросы раскладываются под цель (белок якорь, жир пол,
        # углеводы остаток). Нужен вес: белок якорится на массу тела, а не на долю калорий.
        trend = await assistant.weight_trend(app_user, limit=1)
        if not trend.logs:
            await message.answer(
                "Сначала запиши вес — «вес 86.8»: без него не разложу макросы "
                "(белок считаю на кг массы, а не долей от калорий)."
            )
            return
        goal = rebalance(nums[0], trend.logs[0].weight_kg, current.target)
    elif len(nums) == 3:
        # Режим 2: задаём МАКРОСЫ → калории считаются сами (4Б+9Ж+4У).
        goal = goal_from_macros(nums[0], nums[1], nums[2], current.target)
    else:
        await message.answer(
            "Формат:\n"
            "<code>/goal 1800</code> — калории, макросы разложу под твою цель\n"
            "<code>/goal 180 80 200</code> — Б Ж У, калории посчитаю сам\n"
            "<code>/goal белок 180</code> — один макрос, калории оставлю прежними"
        )
        return
    await users.set_goal(app_user.id, goal)
    await _send_goal_card(message, goal, app_user, assistant, header="🎯 Цель обновлена")


@router.message(Command("today"))
async def cmd_today(message: Message, app_user: User, assistant: Assistant) -> None:
    await _show_today(message, app_user, assistant)


@router.message(Command("week"))
async def cmd_week(
    message: Message, app_user: User, assistant: Assistant, scans: BodyScanService
) -> None:
    await _show_week(message, app_user, assistant, scans)


@router.message(Command("card"))
async def cmd_card(message: Message, app_user: User, assistant: Assistant) -> None:
    """Карточка дня для шеринга — тот же экран дня, но без стрелок навигации."""
    rem, dv = await assistant.day_card(app_user)
    await _day_send(lambda html: _rich(message, html), dv, rem)


@router.message(Command("streak"))
async def cmd_streak(message: Message, app_user: User, assistant: Assistant) -> None:
    """Календарь «дней в цели» за 4 недели — цветные эмодзи 7 в ряд (пусто/наверх)."""
    days, goal = await assistant.day_calories(app_user, 28)
    await _rich(message, screens.streak(days, goal.daily_kcal))


@router.message(Command("month"))
async def cmd_month(message: Message, app_user: User, assistant: Assistant) -> None:
    """Отчёт за 30 дней: покрытие лога, среднее КБЖУ/день, тренд веса."""
    await _rich(message, screens.month(await gather(assistant, app_user)))


@router.message(Command("history"))
async def cmd_history(message: Message, app_user: User, assistant: Assistant) -> None:
    await _show_history(message, app_user, assistant)


@router.message(Command("undo"))
async def cmd_undo(message: Message, app_user: User, assistant: Assistant) -> None:
    await _show_undo_picker(message, app_user, assistant)


@router.message(Command("products"))
async def cmd_products(
    message: Message, command: CommandObject, app_user: User, assistant: Assistant
) -> None:
    # `/products тофу` — поиск по каталогу (FR-15.1). Голый `/products` — 10 последних оценок.
    query = (command.args or "").strip() or None
    products = await assistant.fixable_products(limit=10, query=query)
    if not products:  # пустой экран сам объяснит; каталог целиком — в аппке (FR-46)
        await _rich(message, screens.products(products, query), open_in_app("cat"))
        return
    await _rich(
        message, screens.products(products, query),
        open_in_app("cat", products_fix_keyboard(products)),
    )


@router.message(Command("ask"))
async def cmd_ask(
    message: Message, command: CommandObject, bot: Bot, app_user: User, session: AsyncSession,
    container: Container | None = None,
) -> None:
    """FR-45: вопрос ассистенту напрямую — без разбора фразы, тем же `Coach`, что у вкладки AI.
    Висящий вопрос порции или веса не снимаем: /ask — отступление, а не новая запись."""
    coach = container.coach_for(session) if container is not None else None
    await coach_chat.ask(message, app_user, coach, command.args or "",
                         on_no_credits=_no_credits_alert(bot, container))


def _no_credits_alert(bot: Bot, container: object | None) -> Callable[[], Awaitable[None]]:
    async def alert() -> None:
        await _alert_owner_no_credits(bot, container)
    return alert


async def _to_coach(
    message: Message, text: str, app_user: User, session: AsyncSession, bot: Bot,
    container: Container | None,
) -> bool:
    """FR-45: отдать текст ассистенту (D-MA-27). False — ключа нет: текст идёт прежним путём,
    разбор и «не понял»."""
    coach = container.coach_for(session) if container is not None else None
    if coach is None:
        return False
    await coach_chat.ask(message, app_user, coach, text,
                         on_no_credits=_no_credits_alert(bot, container))
    return True


def _asked(result: Result) -> bool:
    """Разбор счёл фразу unknown, и она похожа на вопрос. Еда сюда не доходит: флаг ставится
    только на intent=unknown (в т.ч. отказ модели разбора — правило то же)."""
    return isinstance(result, Unknown) and result.question


@router.message(Command("profile"))
async def cmd_profile(message: Message, state: FSMContext) -> None:
    await _start_profile(message, state, app=True)


# --- кнопки постоянного меню (приходят как обычный текст) ----------------------


@router.message(F.text == BTN_TODAY)
async def btn_today(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    await state.clear()
    await _show_today(message, app_user, assistant)


@router.message(F.text == BTN_WEEK)
async def btn_week(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext,
    scans: BodyScanService,
) -> None:
    await state.clear()
    await _show_week(message, app_user, assistant, scans)


@router.message(F.text == BTN_HISTORY)
async def btn_history(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    await state.clear()
    await _show_history(message, app_user, assistant)


@router.message(F.text == BTN_WEIGHT)
async def btn_weight(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    await state.clear()
    await _show_weight(message, app_user, assistant)


@router.message(F.text == BTN_GOAL)
async def btn_goal(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    await state.clear()
    goal = await assistant.current_goal(app_user)
    await _send_goal_card(message, goal, app_user, assistant, header="🎯 Твоя дневная норма")


@router.message(F.text == BTN_PROFILE)
async def btn_profile(message: Message, state: FSMContext) -> None:
    await _start_profile(message, state, app=True)


@router.message(F.text == BTN_UNDO)
async def btn_undo(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    await state.clear()
    await _show_undo_picker(message, app_user, assistant)


_FREQ_HEAD = "⚡ <b>Частое</b> — тап сразу запишет в сегодня."
_FREQ_HEAD_SETS = "\n📌 — наборы, остальное — твоя обычная порция."
_FREQ_EMPTY = (
    "⚡ Пока пусто — тут появится то, что ешь чаще всего.\n"
    "Запиши еду пару раз как обычно; несколько позиций сохраняются «📌 Сохранить набор»."
)


@router.message(F.text == BTN_FREQUENT)
async def btn_frequent(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    """FR-40: наборы и частое кнопками. Данные — те же, что у чипов аппки (`sets`, `frequent`)."""
    await state.clear()
    sets = await assistant.recent_sets(app_user, days=30)
    # Тот же топ, что у аппки: бот не показывает продукт, которого нет в её чипах. Имя — из
    # каталога (так его запишет log_quick и покажет карточка), а не из снапшотов; продукт,
    # которого в каталоге уже нет (слили/удалили), кнопкой не показываем.
    rows = []
    for pid, _name, grams, times in await assistant.frequent(app_user, days=30, limit=FREQ_TOP):
        product = await assistant.get_product(pid) if pid else None
        if product is not None:
            rows.append((pid, product.display_name, grams, times))
    markup = frequent_keyboard(sets, rows)
    if markup is None:
        await message.answer(_FREQ_EMPTY)
        return
    # строка про 📌 — только когда набор реально попал в кнопки
    has_sets = any(b.text.startswith("📌") for row in markup.inline_keyboard for b in row)
    await message.answer(_FREQ_HEAD + (_FREQ_HEAD_SETS if has_sets else ""), reply_markup=markup)


_BUG_PROMPT = "🐛 Опиши, что не так — одним сообщением. Приложу последние записи."
_BUG_DONE = "🐛 Спасибо! Записал багрепорт и последние записи — гляну."


@router.message(Command("bug"))
async def cmd_bug(
    message: Message,
    command: CommandObject,
    app_user: User,
    assistant: Assistant,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    note = (command.args or "").strip()
    if not note:
        await state.set_state(MealStates.awaiting_bug)
        await message.answer(_BUG_PROMPT)
        return
    await assistant.report_bug(app_user, note)
    await session.commit()
    await message.answer(_BUG_DONE)


@router.message(F.text == BTN_BUG)
async def btn_bug(message: Message, state: FSMContext) -> None:
    await state.set_state(MealStates.awaiting_bug)
    await message.answer(_BUG_PROMPT)


@router.message(F.text.in_({"🧪 Демо", "🅰 Дашборд", "🅱 Тренер", "🅲 Лента"}))
async def btn_demo_legacy(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    """Перехват СТАРЫХ демо-кнопок из кэша клавиатуры (демо выбрано и удалено).

    Клавиатура пересылается только на /start, /help и в визардах — значит у обоих юзеров в чате
    ещё висят старые «🧪 Демо / 🅰 / 🅱 / 🅲». Без перехвата тап по ним провалился бы в on_text и
    бот попытался бы ЗАПИСАТЬ «🅰 Дашборд» как приём пищи (точность учёта — приоритет №1). Заодно
    самолечим клавиатуру: показываем «Сегодня» уже в новом дизайне с актуальным меню внизу.
    """
    await state.clear()
    await message.answer(
        "Дизайн выбран и раскатан на весь бот 🎉 Меню обновил ниже.",
        reply_markup=main_menu_keyboard(),
    )
    await _show_today(message, app_user, assistant)


@router.message(MealStates.awaiting_bug, F.text)
async def on_bug_note(
    message: Message,
    app_user: User,
    assistant: Assistant,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    await state.clear()
    await assistant.report_bug(app_user, message.text or "")
    await session.commit()
    await message.answer(_BUG_DONE)


@router.message(MealStates.awaiting_bug, F.photo)
async def on_bug_photo(
    message: Message,
    app_user: User,
    assistant: Assistant,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """FR-26: скриншот в багрепорт — сохраняем file_id (а не уводим в read_label как этикетку)."""
    await state.clear()
    fid = message.photo[-1].file_id if message.photo else ""
    note = (message.caption or "").strip() or "[скриншот без описания]"
    await assistant.report_bug(app_user, f"{note}\n[скриншот file_id={fid}]")
    await session.commit()
    await message.answer("🐛 Спасибо! Скриншот и последние записи сохранил — гляну.")


@router.message(MealStates.awaiting_bug, F.voice)
async def on_bug_voice(
    message: Message,
    bot: Bot,
    app_user: User,
    assistant: Assistant,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """FR-26: голосовой багрепорт — ТРАНСКРИБИРУЕМ и сохраняем текст (+ file_id), баг #9."""
    await state.clear()
    buffer = await bot.download(message.voice) if message.voice else None
    audio = buffer.read() if buffer is not None else b""
    transcript = (await assistant.transcribe(audio, filename="bug.ogg")) if audio else None
    fid = message.voice.file_id if message.voice else ""
    note = (transcript or "").strip() or "[голос не распознан]"
    await assistant.report_bug(app_user, f"🎤 {note}\n[file_id={fid}]")
    await session.commit()
    await message.answer("🐛 Спасибо! Голосовой багрепорт расшифровал и записал — гляну.")


# --- FSM-вводы (ввод чисел в конкретных состояниях) ----------------------------


@router.message(MealStates.awaiting_density, F.text)
async def on_density_input(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    nums = _parse_floats(message.text or "")
    if len(nums) != 4:
        await message.answer(
            "Нужно 4 числа через пробел: ккал белки жиры углеводы. "
            "Напр.: <code>165 31 3.6 0</code>\nИли нажми кнопку меню, чтобы выйти."
        )
        return
    data = await state.get_data()
    product_id = int(data.get("product_id", 0))
    await state.clear()
    product = await assistant.fix_product_density(
        product_id, Density(nums[0], nums[1], nums[2], nums[3])
    )
    if product is None:
        await message.answer("Продукт не найден.")
        return
    await message.answer(
        f"✅ Обновил «{escape(product.display_name, quote=False)}»: "
        f"{nums[0]:g} ккал · Б{nums[1]:g} Ж{nums[2]:g} У{nums[3]:g} на 100 г.\n"
        f"Новые записи будут считаться по этим цифрам."
    )


# --- «📌 Сохранить набор» / «📌 Приём пищи — в набор» (FR-39.2, FR-47) ----------------------

# Кнопка карточки записи, которой больше нет: слили набором (FR-47) или удалили — с её id писать
# нечего, и «уже удалено» без причины пугало бы после слияния («я же ничего не удалял»).
_GONE_CARD = "Запись вошла в набор или удалена — см. 🗒 История"
_DISH_DROPPED = "📌 Имя набора — вопрос снят."
_DISH_RENAME, _DISH_CANCEL, _DISH_RETRY = "", "x", "r"  # хвосты `dish_save:<id>[:…]`
# FR-47: отказы по коду `SessionRejected` — ничего не слито.
_SET_REFUSED = {
    "not_a_session": "Приём пищи изменился — ничего не слил.",
    "bad_set": "Набор не сохранил: нужно хотя бы две позиции, и у каждой — вес.",
    "bad_name": "Имя набора — от 1 до 64 знаков. Нажми «📌» ещё раз.",
}
_ASK_TAIL = "Пришли имя — дальше оно запишет этот же состав."


def _weighed(meals: list[Meal]) -> bool:
    """FR-39.2: набор повторяет состав по граммам — позиций ≥ 2, у каждой вес."""
    items = [i for m in meals for i in m.items]
    return len(items) >= 2 and all(i.grams and i.grams > 0 for i in items)


def _session_brief(sess: MealSession, tz_name: str) -> str:
    """«с 13:05 — 5 записей, ≈513 ккал: сыр, лаваш, …» — что именно войдёт в набор. Вопрос и
    отказ называют состав: приём пищи мог вырасти с тех пор, как нарисовали карточку, —
    человек подтверждает этот состав, а не тот, что помнит."""
    k = len(sess.meals)
    when = sess.start.astimezone(ZoneInfo(tz_name)).strftime("%H:%M")
    names = ", ".join(i.name_snapshot for m in sess.meals for i in m.items)
    names = names if len(names) <= 200 else names[:199] + "…"
    approx = "≈" if sess.estimated else ""
    return (
        f"с {when} — {k} {plural(k, 'запись', 'записи', 'записей')}, "
        f"{approx}{round(sess.totals.kcal)} ккал: {escape(names, quote=False)}"
    )


def _session_ask(sess: MealSession, tz_name: str) -> str:
    brief = _session_brief(sess, tz_name)
    return f"📌 Как назвать набор? В него войдёт приём пищи {brief}. {_ASK_TAIL}"


def _dish_kb(meal_id: int, tail: str, text: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=text, callback_data=f"{DISH_SAVE_PREFIX}:{meal_id}:{tail}"
    )]])


async def _drop_dish_question(bot: Bot | None, chat_id: int, state: FSMContext) -> None:
    """Снять висящий вопрос об имени (еда текстом, ответ на карточку, голос, фото, «✖️»):
    состояние — сбросить, вопрос — в «вопрос снят» без кнопок, как у «✍️ Своё» (FR-43)."""
    if await state.get_state() != MealStates.awaiting_dish_name.state:
        return
    data = await state.get_data()
    await state.clear()
    if bot is not None:
        await _say_prompt(bot, chat_id, data.get("dish_prompt"), _DISH_DROPPED)


@router.callback_query(F.data.startswith(f"{DISH_SAVE_PREFIX}:"))
async def on_dish_save(
    callback: CallbackQuery, app_user: User, assistant: Assistant, state: FSMContext,
    session: AsyncSession,
) -> None:
    """FR-39.2: «Сохранить набор» → спрашиваем имя; состав берём из самой записи.

    FR-47: `:s` — «📌 Приём пищи — в набор». Приём пищи берём заново, называем в вопросе, и его id
    фиксируем В МОМЕНТ ВОПРОСА: сохранится ровно то, что названо, а изменился он, пока человек
    думал над именем, — сервер откажет `not_a_session` (D-MA-29). Распался до одной записи —
    прежний путь этой записи. `:x` — «✖️ Отмена» вопроса, `:r` — «Сохранить как «имя»» из отказа."""
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    try:
        meal_id = int(parts[1])
    except (IndexError, ValueError):
        await callback.answer("Кнопка устарела.")
        return
    tail = parts[2] if len(parts) == 3 else _DISH_RENAME
    if tail == _DISH_CANCEL:
        await _dish_cancel(callback, state)
        return
    if tail == _DISH_RETRY:
        await _dish_retry(callback, app_user, assistant, state, session, meal_id)
        return
    whole = tail == DISH_SESSION
    sess = await _session(app_user, assistant, meal_id) if whole else None
    meal = None if sess is not None else await assistant.get_meal(app_user, meal_id)
    if sess is None and meal is None:
        await callback.answer(_GONE_CARD)
        return
    if not _weighed(sess.meals if sess is not None else [meal]):  # вес пропал (правка в аппке)
        await callback.answer(_SET_REFUSED["bad_set"], show_alert=True)
        return
    if sess is not None:
        ask = _session_ask(sess, app_user.timezone)
    elif meal is not None and meal.template_name:  # запись уже набор — переименовать (AC-209)
        ask = f"✏️ Как переименовать набор «{escape(meal.template_name, quote=False)}»? {_ASK_TAIL}"
    else:
        ask = f"📌 Как назвать набор? {_ASK_TAIL}"
    await _drop_dish_question(callback.message.bot, callback.message.chat.id, state)
    prompt = await callback.message.answer(ask, reply_markup=_dish_kb(meal_id, "x", "✖️ Отмена"))
    await state.set_state(MealStates.awaiting_dish_name)
    await state.update_data(
        dish_meal_id=meal_id, dish_session=sess.meal_ids if sess else None,
        dish_prompt=prompt.message_id, dish_card=callback.message.message_id,
    )
    await callback.answer()


async def _dish_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Своя отмена — не общая `wiz_cancel`: та сбросила бы вопрос ДРУГОЙ карточки."""
    assert isinstance(callback.message, Message)
    data = await state.get_data()
    if (await state.get_state() == MealStates.awaiting_dish_name.state
            and data.get("dish_prompt") == callback.message.message_id):
        await _drop_dish_question(callback.message.bot, callback.message.chat.id, state)
        await callback.answer("Отменил.")
        return
    await callback.answer("Уже не жду имя.")
    with suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(reply_markup=None)


async def _dish_retry(
    callback: CallbackQuery, user: User, assistant: Assistant, state: FSMContext,
    session: AsyncSession, meal_id: int,
) -> None:
    """«📌 Сохранить как «имя»» из отказа `not_a_session` (AC-208): тот состав, что назван в
    отказе, и то имя, что человек уже написал, — второй раз его не спрашиваем."""
    assert isinstance(callback.message, Message)
    retry = (await state.get_data()).get("dish_retry") or {}
    ids = [int(x) for x in retry.get("ids", [])]
    if meal_id not in ids:
        await callback.answer("Кнопка устарела — нажми «📌» на карточке.")
        return
    await state.update_data(dish_retry=None)
    await callback.answer()
    with suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(reply_markup=None)
    await _save_session_set(
        callback.message, user, assistant, state, session, ids, str(retry.get("name", "")),
        retry.get("card"),
    )


@router.message(MealStates.awaiting_dish_name, F.text)
async def on_dish_name_input(
    message: Message, bot: Bot, app_user: User, assistant: Assistant, state: FSMContext,
    session: AsyncSession,
) -> None:
    """FR-39.2: имя блюда → ярлык на записи. Дальше это имя повторяет состав (INV-3). FR-47:
    вопрос был про приём пищи — его записи сливаются в одну с этим именем.

    Вопрос не глотает еду (AC-205) — правило «✍️ Своё» (FR-43): именем считается только
    короткий текст без цифр и единиц, одной строкой и не ответом на карточку. Остальное —
    снять вопрос и разобрать как обычно (правка FR-44 или запись). Длиннее 64 — отказ, вопрос
    ждёт: молча обрезанное имя повторялось бы по обрезку (AC-206)."""
    name = (message.text or "").strip()
    if not name or "\n" in name or _AMOUNT_RE.search(name) or _replied_meal(message) is not None:
        await _drop_dish_question(bot, message.chat.id, state)
        await on_text(message, bot, app_user, assistant, session, state)
        return
    if len(name) > 64:
        await message.answer(_SET_REFUSED["bad_name"])
        return
    data = await state.get_data()
    await _drop_dish_question(None, message.chat.id, state)  # вопрос стал ответом — не «снят»
    await _say_prompt(bot, message.chat.id, data.get("dish_prompt"),
                      f"📌 Имя набора: «{escape(name, quote=False)}»")
    ids = data.get("dish_session")
    if ids:
        await _save_session_set(message, app_user, assistant, state, session, list(ids), name,
                                data.get("dish_card"))
        return
    saved = await assistant.remember_dish(app_user, int(data.get("dish_meal_id", 0)), name)
    if saved is None:
        await message.answer(
            "Набор не сохранил: нужно хотя бы две позиции, и у каждой — вес. "
            "Запиши состав с граммами — тогда повторю его одним словом."
        )
        return
    await message.answer(format_set_saved(saved, name), parse_mode="HTML")


async def _save_session_set(
    message: Message, user: User, assistant: Assistant, state: FSMContext,
    session: AsyncSession, ids: list[int], name: str, card_id: object = None,
) -> None:
    """FR-47: слить приём пищи набором → новая карточка слитой записи; карточка, где нажали «📌»,
    — «📌 Вошло в набор» без кнопок (AC-207). Отказ — коротко, ничего не слито: группу сверил
    application (группировке бота не верим); `not_a_session` предлагает тот же «имя» на текущий
    состав (AC-208)."""
    try:
        logged = await assistant.save_session_as_set(user, ids, name)
    except SessionRejected as exc:
        text, again = _SET_REFUSED.get(exc.code, _SET_REFUSED["bad_set"]), None
        now = getattr(exc, "session", None)
        if exc.code == "not_a_session" and now is not None and now.meal_ids and _weighed(now.meals):
            text += f" Сейчас в нём: {_session_brief(now, user.timezone)}."
            await state.update_data(dish_retry={"ids": now.meal_ids, "name": name, "card": card_id})
            again = _dish_kb(now.meal_ids[-1], _DISH_RETRY,
                             f"📌 Сохранить как «{name if len(name) <= 24 else name[:23] + '…'}»")
        elif exc.code == "not_a_session":
            text += " Нажми «📌» ещё раз."
        await message.answer(text, reply_markup=again)
        return
    await session.commit()  # фиксируем ДО карточки — «📌 Набор» не врёт
    bot = message.bot
    if len(ids) > 1 and bot is not None:  # слили: прежняя карточка — след, а не живые кнопки
        await _rich_edit_at(bot, message.chat.id, card_id,
                            f"<h3>📌 Вошло в набор «{escape(name, quote=False)}»</h3>"
                            "<p>Карточка набора — ниже.</p>", None)
    ctx, sess = await gather(assistant, user), await _session(user, assistant, logged.meal.id)
    await _rich(
        message, screens.meal_card(ctx, logged, saved=True, session=sess),
        meal_actions_keyboard(logged.meal, None, sess, trash=False),
    )


@router.message(MealStates.awaiting_rename, F.text)
async def on_rename_input(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    """FR-27 #6: новое имя продукта → переименовать."""
    data = await state.get_data()
    product_id = int(data.get("product_id", 0))
    await state.clear()
    product = await assistant.rename_product(product_id, message.text or "")
    if product is None:
        await message.answer("Не вышло переименовать — продукт не найден или пустое имя.")
        return
    await message.answer(f"✅ Теперь «{escape(product.display_name, quote=False)}».")


@router.message(MealStates.awaiting_item_grams, F.text)
async def on_item_grams_input(
    message: Message, bot: Bot, app_user: User, assistant: Assistant, session: AsyncSession,
    state: FSMContext,
) -> None:
    """FR-43: ответ на «✍️ Своё». Не вес («гречка 200», «банан») — НЕ съедаем: вопрос снимаем,
    текст идёт обычной записью (AC-128). Голое число < 10 — уточняем кнопками (AC-129). «x2»/«½»
    — множитель текущего веса, «~180»/«180 мл» — граммы (AC-132). Число вне 1…2000 г —
    переспрашиваем: молча зажать 3000 до 2000 значило бы записать не то (AC-124)."""
    if _replied_meal(message) is not None:  # FR-44: ответ на карточку — правка, а не число
        await _drop_weight_question(bot, message.chat.id, state)
        await on_text(message, bot, app_user, assistant, session, state)
        return
    data = await state.get_data()
    parsed = parse_grams_answer(message.text or "")
    times = parse_times_answer(message.text or "")
    if times is not None:  # «x2», «½» — ответ на вопрос, а не новая запись (AC-132)
        item = await _typed_item(app_user, assistant, data)
        if item is None:
            await _drop_weight_question(bot, message.chat.id, state)
            await message.answer(_WEIGHT_GONE)
            return
        parsed = float(half_up(half_up(item.grams) * times)), True  # как пресеты
    if parsed is None:
        await _drop_weight_question(bot, message.chat.id, state)
        await on_text(message, bot, app_user, assistant, session, state)
        return
    grams, unit = parsed
    if not unit and grams < _SMALL_MAX:
        await _ask_small(message, app_user, assistant, state, data, grams)
        return
    if not MIN_PORTION_G <= grams <= MAX_PORTION_G:
        await message.answer(_GRAMS_BOUNDS)
        return
    await state.clear()
    meal_id, item_id = int(data.get("iw_meal", 0)), int(data.get("iw_item", 0))
    adjusted = await _adjust_weight(app_user, assistant, meal_id, item_id, grams)
    if adjusted is None:
        await _say_prompt(bot, message.chat.id, data.get("iw_prompt"), _dropped(data))
        await message.answer(_WEIGHT_GONE)
        return
    await session.commit()  # фиксируем ДО ответа — «Поправил» не врёт
    await _finish_typed(message, data, app_user, assistant, *adjusted)


@router.message(MealStates.awaiting_portion, F.text)
async def on_portion_text(
    message: Message,
    app_user: User,
    assistant: Assistant,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Ответ на вопрос о порции ТЕКСТОМ (не тапом): «30 грамм» / «большая»."""
    data = await state.get_data()
    draft = data.get("draft")
    raw = str(data.get("raw", ""))
    if draft is None:  # черновик потерян — как в callback: просим заново (не реобрабатываем текст)
        await state.clear()
        await message.answer(
            "⏳ Этот вопрос уже неактуален (бот перезапускался или прошло время). "
            "Отправь еду заново."
        )
        return

    item = draft.items[int(data.get("item_index", 0))]
    hint = _portion_word(message.text or "")
    if hint is not None:
        item.unit = Unit.PORTION
        item.portion_hint = hint
    else:
        grams = _first_number(message.text or "")
        if grams is None:
            await message.answer(
                "Ответь размером (Маленькая / Средняя / Большая) или граммами, напр. «250»."
            )
            return
        item.unit = Unit.GRAM
        item.grams = grams
        item.portion_hint = None

    result = await assistant.continue_meal(app_user, draft, raw_input=raw)
    await session.commit()
    await state.clear()
    await _reply_message(message, result, state, app_user, assistant, raw=raw)


@router.message(MealStates.awaiting_variant, F.text)
async def on_variant_text(
    message: Message,
    bot: Bot,
    app_user: User,
    assistant: Assistant,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Ответ на уточнение ТЕКСТОМ — свой вариант (D-19), не теряем черновик (AC-32).

    Текст вмёрживается в имя позиции и плотность ПЕРЕоценивается под уточнённое блюдо
    (resolve_clarification). Так юзер не ограничен кнопками — впишет «с фалафелем» и т.п.
    """
    data = await state.get_data()
    draft = data.get("draft")
    raw = str(data.get("raw", ""))
    q_msg_id = data.get("q_msg_id")  # сообщение-вопрос: уберём его «висящую» клавиатуру
    if draft is None:
        await state.clear()
        await message.answer(
            "⏳ Этот вопрос уже неактуален (бот перезапускался или прошло время). "
            "Отправь еду заново."
        )
        return

    item_index = int(data.get("item_index", 0))
    new_name = f"{draft.items[item_index].name} {message.text or ''}".strip()  # имя + дописка
    result = await assistant.resolve_clarification(
        app_user, draft, item_index, new_name, raw_input=raw
    )
    await session.commit()
    await state.clear()
    await _reply_message(message, result, state, app_user, assistant, raw=raw)
    if q_msg_id is not None:  # снять inline-кнопки старого вопроса (ответили текстом)
        with suppress(Exception):
            await bot.edit_message_reply_markup(
                chat_id=message.chat.id, message_id=q_msg_id, reply_markup=None
            )


@router.message(ProfileStates.body, F.text)
async def on_profile_body(message: Message, state: FSMContext) -> None:
    nums = _parse_floats(message.text or "")
    if len(nums) != 3:
        await message.answer(
            "Нужно 3 числа через пробел: <b>вес рост возраст</b>. Напр.: <code>86 180 30</code>"
        )
        return
    weight, height, age = nums
    if not (20 <= weight <= 400 and 100 <= height <= 250 and 5 <= age <= 120):
        await message.answer(
            "Проверь числа: вес 20–400 кг, рост 100–250 см, возраст 5–120. "
            "Напр.: <code>86 180 30</code>"
        )
        return
    await state.update_data(weight=nums[0], height=nums[1], age=nums[2])
    await state.set_state(ProfileStates.activity)
    await message.answer("Уровень активности?", reply_markup=activity_keyboard())


# --- голос и свободный текст ---------------------------------------------------


@router.message(F.voice)
async def on_voice(
    message: Message,
    bot: Bot,
    app_user: User,
    assistant: Assistant,
    session: AsyncSession,
    state: FSMContext,
    container: Container | None = None,
) -> None:
    # Не затираем незавершённый черновик голосом; вопросы «✍️ Своё» (FR-43) и имени набора
    # (FR-47) голос снимает — это новая еда, а не ответ.
    if await _busy_with_flow(message, state, supersedes=(
        MealStates.awaiting_item_grams.state, MealStates.awaiting_dish_name.state,
    )):
        return
    await _drop_fix_question(bot, message.chat.id, state)  # «что заменить» голос снимает (FR-44)
    await state.clear()
    if message.voice is None:
        return

    await _send_typing(message)
    buffer = await bot.download(message.voice)
    audio = buffer.read() if buffer is not None else b""
    transcript = await assistant.transcribe(audio, filename="voice.ogg")

    if transcript is None:
        await message.answer("🎤 Голос сейчас недоступен — напиши текстом.")
        return
    if not transcript.strip():
        await message.answer("🎤 Не расслышал — запиши поближе к микрофону в тишине, или текстом.")
        return

    await message.answer(f"🎤 Распознал: «{escape(transcript, quote=False)}»")
    await _send_typing(message)
    # FR-45 (б, в): явный вопрос — ассистенту до разбора, иначе разбор запишет фантомный приём.
    if coach_chat.direct(app_user.id, transcript) and await _to_coach(
        message, transcript, app_user, session, bot, container
    ):
        return
    result = await assistant.process(app_user, transcript, input_type="voice")
    await session.commit()
    if _asked(result) and await _to_coach(message, transcript, app_user, session, bot, container):
        return
    await _reply_message(message, result, state, app_user, assistant, raw=transcript)
    await _maybe_weight_chart(message, app_user, assistant, result)


@router.message(F.photo)
async def on_photo(
    message: Message,
    bot: Bot,
    app_user: User,
    assistant: Assistant,
    session: AsyncSession,
    state: FSMContext,
    scans: BodyScanService,
) -> None:
    """Фото = этикетка (FR-27), блюдо (FR-34) или бланк InBody (FR-36): различает vision."""
    # Новый снимок отменяет вопрос про вес этикетки и открытую карточку блюда
    # (багрепорт #31); прочие незавершённые шаги по-прежнему держат оборону.
    if await _busy_with_flow(
        message, state,
        supersedes=(MealStates.awaiting_label_grams.state, MealStates.awaiting_photo.state,
                    MealStates.awaiting_scan.state, MealStates.awaiting_item_grams.state,
                    MealStates.awaiting_dish_name.state),
    ):
        return
    await _drop_fix_question(bot, message.chat.id, state)  # «что заменить» фото снимает (FR-44)
    await state.clear()
    if not message.photo:
        return
    await _send_typing(message)
    photo = message.photo[-1]  # последний = максимальное разрешение
    buffer = await bot.download(photo)
    image = buffer.read() if buffer is not None else b""
    try:
        result = await assistant.read_photo(
            app_user, image, caption=message.caption or "", photo_file_id=photo.file_id
        )
    except LimitReached:
        await message.answer(
            "Лимит на распознавание фото в этом месяце исчерпан — опиши блюдо словами, "
            "это работает всегда."
        )
        return
    await session.commit()  # расход vision уже записан своей сессией; тут — пины этикетки
    if isinstance(result, BodyScanRead):
        scan, date_state, reason = from_reading(result.reading, app_user.timezone)
        if scan is None:
            await message.answer(SCAN_REFUSAL[reason or "no_numbers"])
            return
        check = await scans.check(app_user, scan, date_state=date_state)
        await state.set_state(MealStates.awaiting_scan)
        await state.update_data(scan=check.scan, date_state=date_state)
        await message.answer(format_scan_card(check, app_user.timezone),
                             reply_markup=scan_keyboard(check))
        return
    if isinstance(result, PlateRead):
        await state.set_state(MealStates.awaiting_photo)
        data = {
            "plate": result.reading, "grams": result.grams, "weighed": result.weighed,
            "off": [],  # D-MA-20: убранные позиции — до записи, а не правкой после
        }
        await state.update_data(**data)
        sent = await message.answer(
            _photo_text(data), reply_markup=_photo_kb(data)
        )
        await state.update_data(card_id=sent.message_id)
        return
    if isinstance(result, LabelRead):
        item = result.draft.items[result.item_index]
        cap_g = (
            _label_grams(message.caption or "", item.unit_weight_g, item.serving_g)
            if message.caption
            else None
        )
        if cap_g:  # #5: вес в подписи к фото («этикетка 200 г») → сразу лог, без вопроса
            item.grams = cap_g
            item.unit = Unit.GRAM
            final = await assistant.continue_meal(
                app_user, result.draft, raw_input="этикетка", input_type="label"
            )
            await session.commit()
            await _reply_message(message, final, state, app_user, assistant, raw="этикетка")
            return
        await state.set_state(MealStates.awaiting_label_grams)
        await state.update_data(draft=result.draft, item_index=result.item_index, raw="этикетка")
        await _rich(
            message,
            screens.label(result),  # КБЖУ/100 г таблицей + вопрос про вес порции
            label_grams_keyboard(item.unit_weight_g, item.serving_g),
        )
    elif isinstance(result, MealLogged):
        # FR-38: скриншот меню записан сразу — тот же ответ, что у текста, с отменой.
        await _reply_message(message, result, state, app_user, assistant, raw="меню")
    else:
        await message.answer(format_result(result))


def _photo_text(data: dict) -> str:
    return format_photo_card(
        data["plate"], data["grams"], weighed=data["weighed"], off=data.get("off") or []
    )


def _photo_kb(data: dict) -> InlineKeyboardMarkup:
    return photo_keyboard(data["plate"].components, data.get("off") or [])


@router.callback_query(F.data.startswith(f"{SCAN_PREFIX}:"))
async def on_scan_btn(
    callback: CallbackQuery,
    app_user: User,
    session: AsyncSession,
    state: FSMContext,
    scans: BodyScanService,
) -> None:
    """FR-36: «Сохранить» / «Всё верно» на карточке бланка и «Применить цели»."""
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    action = (callback.data or "").split(":", 1)[1]
    if action == "apply":
        await callback.answer()
        try:
            goal = await scans.apply(app_user)
        except ValueError:
            await callback.message.answer("Нужен свежий замер InBody, чтобы пересчитать цели.")
            return
        await session.commit()
        with suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"✅ Цели обновлены: {goal.daily_kcal:g} ккал · Б {goal.protein_g:g} · "
            f"Ж {goal.fat_g:g} · У {goal.carb_g:g}.\n"
            "Прошедшие дни недели остались посчитанными по старой цели."
        )
        return
    data = await state.get_data()
    scan = data.get("scan")
    if scan is None:
        await callback.answer("Карточка устарела — пришли бланк заново.")
        return
    await callback.answer()
    try:
        saved, updated = await scans.save(
            app_user, scan, confirmed=action == "confirm",
            date_state=str(data.get("date_state") or "ok"),
        )
    except ValueError as exc:
        text = ("Дата замера в будущем — сними бланк ещё раз." if str(exc) == "future"
                else "Цифры не сходятся — нажми «Всё верно» или поправь их в мини-аппе.")
        await callback.message.answer(text)
        return
    await session.commit()
    await state.clear()
    with suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(reply_markup=None)
    overview = await scans.overview(app_user)
    head = "💾 Замер обновлён." if updated else "💾 Замер сохранён — он в «Итогах» мини-аппа."
    prop = overview.proposal
    if prop is None or not prop.changed:
        await callback.message.answer(head, reply_markup=open_in_app("body"))  # FR-46
        return
    await callback.message.answer(
        head + "\n\n" + format_goal_proposal(prop),
        reply_markup=open_in_app("body", scan_apply_keyboard()),
    )


@router.message(MealStates.awaiting_photo, F.text)
async def on_photo_weight(message: Message, bot: Bot, state: FSMContext) -> None:
    """FR-34: вес тарелки числом, пока открыта карточка → пересчёт граммов на месте."""
    data = await state.get_data()
    if data.get("plate") is None:
        await state.clear()
        await message.answer("⏳ Карточка устарела — пришли фото заново.")
        return
    total = label_grams(message.text or "", None)
    if total is None or not 1 <= total <= 5000:
        await message.answer(
            "Пришли вес тарелки числом (например 420) или нажми «✅ Записать» под карточкой."
        )
        return
    # Вес позиции, названный человеком в подписи (`fixed`), при пересчёте не двигается.
    current = [Component(c.name, c.share, g, c.fixed) for c, g in zip(
        data["plate"].components, data["grams"], strict=True)]
    data["grams"] = plate_split(current, total)
    data["weighed"] = True
    await state.update_data(grams=data["grams"], weighed=True)
    card_id = data.get("card_id")
    if card_id:
        with suppress(TelegramBadRequest):
            await bot.edit_message_text(
                _photo_text(data), chat_id=message.chat.id, message_id=card_id,
                reply_markup=_photo_kb(data),
            )
            return
    sent = await message.answer(_photo_text(data), reply_markup=_photo_kb(data))
    await state.update_data(card_id=sent.message_id)


@router.callback_query(F.data.startswith(f"{PHOTO_PREFIX}:"))
async def on_photo_btn(
    callback: CallbackQuery,
    app_user: User,
    assistant: Assistant,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """FR-34: «✖ позиция» (убрать/вернуть) и «Записать» на карточке блюда (D-MA-20)."""
    data = await state.get_data()
    if data.get("plate") is None or not isinstance(callback.message, Message):
        await callback.answer("Карточка устарела — пришли фото заново.")
        return
    parts = (callback.data or "").split(":")
    plate = data["plate"]
    off = list(data.get("off") or [])
    if parts[1] == "off" and len(parts) == 3:
        i = int(parts[2])
        if 0 <= i < len(plate.components):
            off = [x for x in off if x != i] if i in off else off + [i]
            data["off"] = off
            await state.update_data(off=off)
        await callback.answer()
        await _safe_edit(callback.message, _photo_text(data), _photo_kb(data))
        return
    if len(off) >= len(plate.components):
        await callback.answer("Всё убрано — нечего записывать.", show_alert=True)
        return
    await callback.answer()
    grams = [None if i in off else g for i, g in enumerate(data["grams"])]
    try:
        text = assistant.photo_text(plate, grams)
    except ValueError:
        await callback.message.answer("Граммы вне границ — пришли вес тарелки числом.")
        return
    # Состояние чистим ПОСЛЕ записи: упал разбор (сеть, 429) — карточка остаётся,
    # и «✅ Записать» можно нажать ещё раз, не переснимая блюдо.
    result = await assistant.log_photo(app_user, text, weighed=bool(data.get("weighed")))
    await session.commit()
    await state.clear()
    await _render_after_continue(callback.message, result, state, app_user, assistant, raw=text)


@router.message(MealStates.awaiting_label_grams, F.text)
async def on_label_grams(
    message: Message,
    app_user: User,
    assistant: Assistant,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Вес порции после этикетки: число или «вся пачка» (FR-27) → лог с точными КБЖУ."""
    data = await state.get_data()
    draft = data.get("draft")
    if draft is None:
        await state.clear()
        await message.answer("⏳ Фото устарело — пришли этикетку заново.")
        return
    item = draft.items[int(data.get("item_index", 0))]
    grams = _label_grams(message.text or "", item.unit_weight_g, item.serving_g)
    if grams is None:
        await message.answer("Сколько грамм съел? Напиши число (или «вся пачка»).")
        return
    item.grams = grams
    item.unit = Unit.GRAM
    result = await assistant.continue_meal(
        app_user, draft, raw_input="этикетка", input_type="label"
    )
    await session.commit()
    await state.clear()
    await _reply_message(message, result, state, app_user, assistant, raw="этикетка")


@router.callback_query(F.data.startswith(f"{LABEL_G_PREFIX}:"))
async def on_label_gram_btn(
    callback: CallbackQuery,
    app_user: User,
    assistant: Assistant,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Пресет веса после этикетки (FR-27): «1 порция»/«вся пачка» → граммы из callback → лог."""
    data = await state.get_data()
    draft = data.get("draft")
    if draft is None or not isinstance(callback.message, Message):
        await callback.answer("Фото устарело — пришли заново.")
        return
    await callback.answer()  # тот же класс, что #21: без этого спиннер крутится до таймаута

    item = draft.items[int(data.get("item_index", 0))]
    item.grams = float((callback.data or "label_g:0").split(":", 1)[1])
    item.unit = Unit.GRAM
    result = await assistant.continue_meal(
        app_user, draft, raw_input="этикетка", input_type="label"
    )
    await session.commit()
    await state.clear()
    await _render_after_continue(
        callback.message, result, state, app_user, assistant, raw="этикетка"
    )


@router.message(F.text)
async def on_text(
    message: Message,
    bot: Bot,
    app_user: User,
    assistant: Assistant,
    session: AsyncSession,
    state: FSMContext,
    container: Container | None = None,
) -> None:
    await _clear_intro_keyboard(message, bot, state)  # #7: гасим висящее интро-меню онбординга
    if await _answer_fix_question(message, app_user, assistant, session, state):
        return  # «гречка» в ответ на «что заменить» — выбор позиции, без модели (FR-44)
    await _drop_fix_question(bot, message.chat.id, state)
    await state.clear()
    await _send_typing(message)
    meal_id = _replied_meal(message)
    if meal_id is not None:  # FR-44: ответ на карточку приёма — правка ЭТОГО приёма
        await _reply_fix(message, app_user, assistant, session, state, meal_id)
        return
    text = message.text or ""
    # FR-45 (б, в): «можно ли есть банан на ночь?» — ассистенту ДО разбора: разбор честно видит
    # в ней еду и пишет фантомный приём. Промпт разбора не трогаем (D-MA-27).
    if coach_chat.direct(app_user.id, text) and await _to_coach(
        message, text, app_user, session, bot, container
    ):
        return
    result = await assistant.process(app_user, text, input_type="text")
    await session.commit()  # фиксируем запись ДО ответа — «✅ записал» не врёт
    if _asked(result) and await _to_coach(message, text, app_user, session, bot, container):
        return  # FR-45 (г): вопрос, а не еда — ответит ассистент
    await _reply_message(message, result, state, app_user, assistant, raw=message.text or "")
    await _maybe_weight_chart(message, app_user, assistant, result)


@router.message(
    StateFilter(
        MealStates.awaiting_portion,
        MealStates.awaiting_variant,
        MealStates.awaiting_density,
        MealStates.awaiting_rename,
        MealStates.awaiting_label_grams,
        MealStates.awaiting_photo,
        MealStates.awaiting_scan,
        MealStates.awaiting_dish_name,
        MealStates.awaiting_item_grams,
        MealStates.awaiting_bug,
        ProfileStates.body,
    )
)
async def on_unexpected_in_flow(message: Message) -> None:
    """#11: не-текст (стикер/документ/…) во время вопроса — подсказываем, а не молчим."""
    await message.answer("Жду ответ текстом или кнопкой (✖️ / кнопка меню — выйти).")


# --- callbacks ----------------------------------------------------------------


@router.callback_query(F.data.startswith("portion:"))
async def on_portion(
    callback: CallbackQuery,
    app_user: User,
    assistant: Assistant,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    draft = data.get("draft")
    if draft is None or not isinstance(callback.message, Message):
        await callback.answer("Запись устарела — отправь приём пищи заново.")
        return

    await callback.answer()  # тот же класс, что #21: иначе спиннер крутится до таймаута

    item_index = int(data.get("item_index", 0))
    raw = str(data.get("raw", ""))
    hint = (callback.data or "portion:medium").split(":", 1)[1]
    draft.items[item_index].portion_hint = hint

    result = await assistant.continue_meal(app_user, draft, raw_input=raw)
    await session.commit()
    await _render_after_continue(callback.message, result, state, app_user, assistant, raw=raw)


@router.callback_query(F.data.startswith("variant:"))
async def on_variant(
    callback: CallbackQuery,
    app_user: User,
    assistant: Assistant,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    draft = data.get("draft")
    if draft is None or not isinstance(callback.message, Message):
        await callback.answer("Запись устарела — отправь приём пищи заново.")
        return

    item_index = int(data.get("item_index", 0))
    raw = str(data.get("raw", ""))
    options = draft.items[item_index].clarify_options or []
    idx = int((callback.data or "variant:0").split(":", 1)[1])
    if idx >= len(options):
        await callback.answer("Вариант не найден — отправь заново.")
        return

    # Отвечаем Telegram СРАЗУ, до похода в LLM (баг-репорт #21). Иначе: спиннер на кнопке крутится
    # все секунды запроса («кнопки подвисают»), а через ~15 с коллбэк протухает — и answer() в
    # конце падает с «query is too old», выдавая «Что-то пошло не так» ПОСЛЕ успешной записи.
    await callback.answer()

    result = await assistant.resolve_clarification(
        app_user, draft, item_index, options[idx], raw_input=raw
    )
    await session.commit()
    await _render_after_continue(callback.message, result, state, app_user, assistant, raw=raw)


@router.callback_query(F.data.startswith("meal_repeat:"))
async def on_meal_repeat(callback: CallbackQuery, app_user: User, assistant: Assistant) -> None:
    """Кнопка «Повторить»: та же еда ещё раз (вторая бутылка, вторая порция).

    Новая карточка ОТДЕЛЬНЫМ сообщением, а не правка старой: это вторая запись, и обе должны
    остаться видимыми — иначе непонятно, добавилась она или подменила первую.
    """
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    await callback.answer()  # отвечаем Telegram сразу, чтобы не крутился спиннер

    meal_id = int((callback.data or "meal_repeat:0").split(":", 1)[1])
    result = await assistant.repeat_meal(app_user, meal_id)
    if result is None:
        await callback.message.answer(_GONE_CARD)
        return
    await _send_meal_card(callback.message, result, app_user, assistant)


TAP_WINDOW_S = 5.0
TAP_IN_FLIGHT_S = 60.0  # дольше записи не бывает; упавший без release ключ отпустится сам
# (user_id, callback) → (момент, записано ли уже). Пока не записано — первый тап ещё в работе.
# Общий для всех кнопок «тап сразу пишет»: «⚡ Частое» (FR-40) и повтор с прошлого дня (FR-42).
_taps: dict[tuple[int, str], tuple[float, bool]] = {}


def _claim_tap(user_id: int, data: str) -> str | None:
    """FR-40: та же кнопка ещё раз — двойной тап, а не вторая порция. None — тап свежий, ключ
    занят; "busy" — первый ещё пишет; "done" — записан меньше TAP_WINDOW_S назад.

    `IdempotencyStore` аппки тут не годится: там ключ намерения шлёт клиент, а у кнопки Telegram
    его нет — id колбэка свой на каждый тап, и на двойной тоже. Отличает двойной тап от второй
    порции только время. Проверка и отметка — без await между ними, поэтому два параллельных
    апдейта (aiogram гоняет их задачами) не проскочат оба.
    ponytail: словарь в памяти процесса — рестарт сбрасывает окно, второй инстанс бота защиты
    не даст; понадобится — ключ в БД.
    """
    now = time.monotonic()
    for key in [
        k for k, (at, done) in _taps.items()
        if now - at > (TAP_WINDOW_S if done else TAP_IN_FLIGHT_S)
    ]:
        del _taps[key]
    found = _taps.get((user_id, data))
    if found is not None:
        return "done" if found[1] else "busy"
    _taps[(user_id, data)] = (now, False)
    return None


async def _write_once(
    callback: CallbackQuery, session: AsyncSession, user_id: int,
    write: Callable[[], Awaitable[_T | None]],
    *, busy: str = "Уже записываю…",
    done: str = "Уже записал. Если это ещё одна порция — тапни через пару секунд.",
) -> _T | None | Literal[False]:
    """Тап, который сразу пишет: страж двойного тапа → запись → фиксация (FR-40, FR-42, FR-43).

    False — это второй тап, всплывашка уже ушла; None — писать нечего (кнопка устарела), окно
    снято. Окно двойного тапа — от момента записи; сбой (в т.ч. фиксации) окно не держит.
    busy/done — ответы второму тапу: правке веса «ещё одна порция» не к месту. Пишет не только
    запись: вопрос «✍️ Своё» под тем же стражем, чтобы двойной тап не слал два вопроса.
    """
    key = (user_id, callback.data or "")
    seen = _claim_tap(*key)
    if seen is not None:
        await callback.answer(busy if seen == "busy" else done)
        return False
    try:
        result = await write()
        if result is not None:
            await session.commit()  # фиксируем ДО ответа — «записал» не врёт
    except Exception:
        _taps.pop(key, None)  # не записали — «Уже записал» не должен держать повтор
        with suppress(TelegramBadRequest):  # уже отвечен (⚡ гасит спиннер сразу) — не страшно
            await callback.answer("Не записал — попробуй ещё раз.")  # без вечного спиннера
        raise
    if result is None:
        _taps.pop(key, None)
    else:
        _taps[key] = (time.monotonic(), True)
    return result


async def _log_frequent(user: User, assistant: Assistant, data: str) -> MealLogged | None:
    """Кнопка «⚡ Частое» → запись в сегодня тем же use-case, что у чипа аппки. None — кнопка
    устарела (продукт слили или удалили, набор переименован) или callback кривой."""
    parts = data.split(":")
    if len(parts) == 4 and parts[1] == "p":
        try:
            product_id, grams = int(parts[2]), float(parts[3])
        except ValueError:
            return None
        if not 0 < grams <= 5000:  # NaN и бесконечность сюда тоже не проходят
            return None
        result = await assistant.log_quick(user, product_id, grams)
        return result if isinstance(result, MealLogged) else None
    if len(parts) == 3 and parts[1] == "s":
        # Набор — по имени (его идентичность), а не по id приёма: тот меняется с каждым повтором.
        # Повтор самого свежего приёма с этим именем — ровно то, что делает и чип аппки (FR-39.2).
        found = next(
            (m for m in await assistant.sets(user)
             if m.template_name and set_key(m.template_name) == parts[2]),
            None,
        )
        if found is None or found.id is None:
            return None
        return await assistant.repeat_meal(user, found.id)
    return None


@router.callback_query(F.data.startswith(f"{FREQ_PREFIX}:"))
async def on_frequent_tap(
    callback: CallbackQuery, app_user: User, assistant: Assistant, session: AsyncSession
) -> None:
    """FR-40: тап пишет сразу, без модели; дальше — обычная карточка приёма с её кнопками.
    Сообщение с кнопками остаётся: можно тут же дописать второе."""
    if not isinstance(callback.message, Message):
        await callback.answer("Сообщение устарело — открой ⚡ Частое заново.", show_alert=True)
        return
    assert app_user.id is not None
    data = callback.data or ""

    async def write() -> MealLogged | None:
        await callback.answer()  # гасим спиннер сразу
        return await _log_frequent(app_user, assistant, data)

    result = await _write_once(callback, session, app_user.id, write)
    if result is False:
        return
    if result is None:
        await callback.message.answer("Этой кнопки уже нет — открой ⚡ Частое заново.")
        return
    await _send_meal_card(callback.message, result, app_user, assistant)


# --- «⏪ Вернуть» после удаления (FR-41, D-MA-23) -----------------------------

UNDO_TTL_S = 600  # то же окно, что у снимка аппки (MA-INV-69): одно число в обоих каналах
_PICKER_HEAD = "Что отменить? (по позициям)"
_ALREADY = "Уже вернул."
_EDITED = "Приём уже поправили — вернуть нельзя."


@dataclass
class _Undo:
    """Снимок удалённого: что вернуть и каким приём остался ПОСЛЕ удаления (отпечаток)."""

    token: str
    at: float
    snapshot: Meal
    after: object  # meal_fingerprint записи сразу после удаления
    what: str  # подпись кнопки «⏪ Вернуть: <что>» — чтобы пересобрать её из снимка
    done: str | None = None  # не None — снимок израсходован, это ответ на следующие тапы
    day: date | None = None  # удалили на экране «День» — какой день он показывал (FR-41)


# Человек → снимок. ОДИН на человека: новое удаление вытесняет прежнее — снимок заменяет приём
# целиком, и два снимка одного приёма спорили бы, чья правда (D-MA-23). Общий с API словарь
# невозможен: бот и API — разные процессы.
# ponytail: память процесса бота — рестарт теряет «Вернуть» (удалено осознанно); потолок —
# мягкое удаление колонкой, общее для обоих каналов.
_trash: dict[int, _Undo] = {}


# Отпечаток «какой запись осталась» — `meal_fingerprint` из application, один с аппкой (MA-INV-80):
# правка словами, удаление, «Вернуть» в аппке, имя набора и слияние приёма пищи набором (FR-47) его
# меняют — и тогда снимок вернул бы не то или задвоил позиции (D-MA-23).


def _remember(user: User, snapshot: Meal, after: Meal | None, what: str) -> str:
    """В корзину — только ПОСЛЕ фиксации удаления: откаченное удаление не вытесняет прежний снимок
    и не показывает «Вернуть» того, что на деле не удалено."""
    assert user.id is not None
    token = secrets.token_urlsafe(6)  # 8 знаков: callback «rst:<токен>:d20261024» далеко от 64 байт
    _trash[user.id] = _Undo(token, time.monotonic(), snapshot, meal_fingerprint(after), what)
    return token


def _live_restore(
    user: User, meal_id: int, where: str, now: Meal | None
) -> InlineKeyboardButton | None:
    """«⏪ Вернуть» из ТЕКУЩЕГО снимка человека — если он про этот приём, не истёк, не съеден и
    приём (`now` — какой он сейчас, None — удалён) с тех пор не правили.

    Не с клавиатуры сообщения: при двойном тапе по 🗑 второй тап несёт клавиатуру, какой она была
    ДО первого (без ⏪), и его перерисовка «Уже убрано.» может лечь последней — кнопка первого
    пропала бы с экрана, хотя снимок жив. Отпечаток — та же сверка, что у самого возврата: после
    «✏️ Вес» кнопка вернулась бы на карточку и только отказала (FR-43)."""
    undo = _trash.get(user.id or 0)
    if (undo is None or undo.done or undo.snapshot.id != meal_id
            or time.monotonic() - undo.at > UNDO_TTL_S or meal_fingerprint(now) != undo.after):
        return None
    return restore_button(undo.token, where, undo.what)


def _day_restore(user: User, day: date, meals: list[Meal]) -> InlineKeyboardButton | None:
    """«⏪ Вернуть» на экране «День» — только живая и только на своём дне: снимок текущий, не
    истёк, не съеден, удалили его с ЭТОГО дня, и приём с тех пор не правили (отпечаток по
    `meals` дня). Со старой клавиатуры не копируем: листание утащило бы на чужой день
    кнопку-зомби, которая перерисует не тот день (FR-41, FR-42, FR-43)."""
    undo = _trash.get(user.id or 0)
    if (undo is None or undo.done or undo.day != day
            or time.monotonic() - undo.at > UNDO_TTL_S):
        return None
    if meal_fingerprint(next((m for m in meals if m.id == undo.snapshot.id), None)) != undo.after:
        return None
    return restore_button(undo.token, f"d{day:%Y%m%d}", undo.what)


def _claim(user_id: int, token: str) -> _Undo | str:
    """Снимок — или текст отказа. Проверка и отметка без await между ними: параллельный второй
    тап слышит «Уже вернул.», пока первый ещё возвращает."""
    undo = _trash.get(user_id)
    if undo is None:
        return "Вернуть уже нельзя."
    if undo.token != token:
        return "Вернуть можно только последнее удалённое."
    if undo.done:
        return undo.done
    if time.monotonic() - undo.at > UNDO_TTL_S:
        return f"Вернуть уже нельзя — прошло больше {UNDO_TTL_S // 60} минут."
    undo.done = _ALREADY
    return undo


def _names(meal: Meal) -> str:
    return ", ".join(i.name_snapshot for i in meal.items)


def _picker_status(message: Message) -> str:
    """Строка «🗑 Отменил: …» над пикером — пережить перерисовку вместе со своей кнопкой."""
    text = message.text or ""
    return escape(text.split("\n\n", 1)[0]) if "\n\n" in text else ""


# (user, meal, item), которые сейчас убираются: параллельный второй тап по той же 🗑 иначе успел бы
# прочитать приём до удаления первым и положить в корзину свой снимок, вытеснив токен первого.
# ponytail: память процесса, как и корзина — один инстанс бота.
_removing: set[tuple[int, int, int]] = set()


async def _remove_item(
    user: User, assistant: Assistant, session: AsyncSession, meal_id: int, item_id: int
) -> tuple[MealLogged | None, MealItem, str] | None | Literal["busy"]:
    """Убрать позицию: снимок ДО удаления (копия — удаление правит тот же объект приёма, урок аппки
    21.10.2026), фиксация, и только потом снимок в корзину. None — позиции уже нет (двойной тап,
    убрали в аппке): корзину не трогаем, иначе обрезанный приём вытеснил бы снимок первого тапа.
    "busy" — эту же позицию прямо сейчас убирает первый тап (проверка и отметка без await)."""
    key = (user.id or 0, meal_id, item_id)
    if key in _removing:
        return "busy"
    _removing.add(key)
    try:
        meal = await assistant.get_meal(user, meal_id)
        if meal is None or not any(i.id == item_id for i in meal.items):
            return None
        snapshot = copy.deepcopy(meal)
        item = next(i for i in snapshot.items if i.id == item_id)
        result = await assistant.remove_item(user, meal_id, item_id)
        await session.commit()
        after = result.meal if result else None
        return result, item, _remember(user, snapshot, after, item.name_snapshot)
    finally:
        _removing.discard(key)


async def _redraw_picker(
    message: Message, user: User, assistant: Assistant, status: str = "",
    restore: InlineKeyboardButton | None = None,
) -> None:
    """Пикер «Отменить» на месте: строка о том, что сделано, над списком сегодняшнего (баг #12)."""
    history = await assistant.today_meals(user)
    head = f"{status}\n\n" if status else ""
    if history.meals:
        await _safe_edit(
            message, head + _PICKER_HEAD,
            undo_picker_keyboard(history.meals, history.timezone, restore),
        )
    else:
        await _safe_edit(
            message, head + "🗒 Больше нечего отменять — старое в 🗒 История.",
            InlineKeyboardMarkup(inline_keyboard=[[restore]]) if restore else None,
        )


async def _redraw_card(message: Message, user: User, assistant: Assistant, meal_id: int) -> None:
    """Устаревшая карточка лечится: показываем приём, каким он есть сейчас, с её «⏪ Вернуть»."""
    meal = await assistant.get_meal(user, meal_id)
    if meal is None:
        return
    ctx, sess = await gather(assistant, user), await _session(user, assistant, meal.id)
    logged = MealLogged(meal=meal, remaining=(await assistant.today(user)).remaining)
    await _rich_edit(
        message, screens.meal_card(ctx, logged, edited=True, session=sess),
        meal_actions_keyboard(meal, _live_restore(user, meal_id, "c", meal), sess),
    )


async def _drop_restore_button(message: Message) -> None:
    """Отказ «Вернуть» снимает только саму кнопку: навигация дня и кнопки карточки живы."""
    kb = message.reply_markup
    if not isinstance(kb, InlineKeyboardMarkup):
        return
    rows = [
        [b for b in row if not (b.callback_data or "").startswith(f"{RESTORE_PREFIX}:")]
        for row in kb.inline_keyboard
    ]
    rows = [r for r in rows if r]
    with suppress(TelegramBadRequest):
        await message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        )


@router.callback_query(F.data.startswith("meal_undo:"))
async def on_meal_undo(
    callback: CallbackQuery, app_user: User, assistant: Assistant, session: AsyncSession
) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    meal_id = int((callback.data or "meal_undo:0").split(":", 1)[1])
    in_picker = _PICKER_HEAD in (callback.message.text or "")
    result = await assistant.delete_meal(app_user, meal_id)
    if result.meal is None:
        # Карточку не трогаем: на ней может висеть «⏪ Вернуть» первого тапа (FR-41). Пикер
        # перерисовываем из живых данных — со своей строкой и кнопкой.
        await callback.answer(_GONE_CARD)
        if in_picker:
            await _redraw_picker(
                callback.message, app_user, assistant,
                _picker_status(callback.message), _live_restore(app_user, meal_id, "p", None),
            )
        return
    # Снимок — то, что вернул delete: состояние ДО удаления, дальше его никто не правит.
    snapshot = copy.deepcopy(result.meal)
    await session.commit()
    what, kcal = _names(snapshot), round(snapshot.totals.kcal)
    token = _remember(app_user, snapshot, None, what)
    if in_picker:
        await _redraw_picker(
            callback.message, app_user, assistant, f"🗑 Отменил: {escape(what)} · {kcal} ккал",
            restore_button(token, "p", what),
        )
    else:  # под карточкой: она говорит, что отменено, и держит «⏪ Вернуть» (раньше удалялась)
        await _rich_edit(
            callback.message, screens.meal_deleted(snapshot, app_user.timezone),
            InlineKeyboardMarkup(inline_keyboard=[[restore_button(token, "c", what)]]),
            fallback_text=f"🗑 Отменил запись: {escape(what)} · {kcal} ккал",
        )
    await callback.answer(f"🗑 Отменил запись ({kcal} ккал)")


@router.callback_query(F.data.startswith("item_del:"))
async def on_item_del(
    callback: CallbackQuery, app_user: User, assistant: Assistant, session: AsyncSession
) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    parts = (callback.data or "item_del:0:0").split(":")
    meal_id, item_id = int(parts[1]), int(parts[2])
    removed = await _remove_item(app_user, assistant, session, meal_id, item_id)
    if removed == "busy":
        await callback.answer("Уже убираю…")
        return
    if removed is None:  # позиции нет: двойной тап — или записи нет вовсе (слили, удалили)
        gone = await assistant.get_meal(app_user, meal_id) is None
        await callback.answer(_GONE_CARD if gone else "Уже убрано.")
        await _redraw_card(callback.message, app_user, assistant, meal_id)
        return
    result, item, token = removed
    back = restore_button(token, "c", item.name_snapshot)
    if result is None:
        await _rich_edit(
            callback.message, "<h3>🗑 Запись отменена</h3><p>Позиций не осталось.</p>",
            InlineKeyboardMarkup(inline_keyboard=[[back]]),
        )
    else:
        # Правим карточку НА МЕСТЕ: повторный тап по кнопке не даст вторую карточку.
        ctx = await gather(assistant, app_user)
        sess = await _session(app_user, assistant, result.meal.id)
        await _rich_edit(
            callback.message, screens.meal_card(ctx, result, edited=True, session=sess),
            meal_actions_keyboard(result.meal, back, sess),
            fallback_text=(
                f"🗑 Убрал: {escape(item.name_snapshot)} · "
                f"запись теперь {round(result.meal.totals.kcal)} ккал"
            ),
        )
    await callback.answer("🗑 Убрал позицию")


@router.callback_query(F.data.startswith("undo_item:"))
async def on_undo_item(
    callback: CallbackQuery, app_user: User, assistant: Assistant, session: AsyncSession
) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    parts = (callback.data or "undo_item:0:0").split(":")
    meal_id, item_id = int(parts[1]), int(parts[2])
    removed = await _remove_item(app_user, assistant, session, meal_id, item_id)
    if removed == "busy":
        await callback.answer("Уже убираю…")
        return
    if removed is None:
        await callback.answer("Уже убрано.")
        await _redraw_picker(
            callback.message, app_user, assistant,
            _picker_status(callback.message),
            _live_restore(app_user, meal_id, "p", await assistant.get_meal(app_user, meal_id)),
        )
        return
    _, item, token = removed
    await callback.answer("🗑 Убрано")
    await _redraw_picker(
        callback.message, app_user, assistant,
        f"🗑 Убрал: {escape(item.name_snapshot)} · {round(item.macros.kcal)} ккал",
        restore_button(token, "p", item.name_snapshot),
    )


async def _days_ago(user: User, assistant: Assistant, where: str) -> int | None:
    """«d20261024» → сколько дней назад этот день СЕЙЧАС: после полуночи «вчера» — уже 1, а не 0.
    Сегодня — по той же границе дня, что у экрана «День» (`day_view`)."""
    try:
        day = datetime.strptime(where[1:], "%Y%m%d").date()
    except ValueError:
        return None
    today = (await assistant.day_view(user, 0)).day
    return max(0, (today - day).days)


@router.callback_query(F.data.startswith(f"{RESTORE_PREFIX}:"))
async def on_restore(
    callback: CallbackQuery, app_user: User, assistant: Assistant, session: AsyncSession
) -> None:
    """FR-41: «⏪ Вернуть» — тот же `restore_meal`, что у аппки; сообщение правится на месте."""
    parts = (callback.data or "").split(":")
    if not isinstance(callback.message, Message) or len(parts) != 3:
        await callback.answer("Вернуть уже нельзя.")
        return
    assert app_user.id is not None
    _, token, where = parts
    undo = _claim(app_user.id, token)
    if isinstance(undo, str):
        await callback.answer(undo)
        if undo != _ALREADY:  # второй тап: сообщение перерисует тот, что вернул
            await _drop_restore_button(callback.message)
        return
    try:
        # Запись с тех пор правили (словами, в аппке), слили набором (FR-47) или удалили/вернули
        # в аппке — снимок молча откатил бы правку или задвоил позиции. Отказ, а не «вернул».
        # Сверку делает `restore_meal` под блокировкой записи: правка между сверкой и возвратом
        # не проскочит (MA-INV-80).
        stale = False
        try:
            result = await assistant.restore_meal(app_user, undo.snapshot, expect=undo.after)
        except SnapshotStale:
            result, stale = None, True
        if result is not None:
            await session.commit()  # фиксируем ДО показа — «Вернул» не врёт
    except Exception:
        undo.done = None  # снимок не съеден — можно тапнуть ещё
        raise
    if result is None:
        undo.done = _EDITED if stale else "Вернуть уже нельзя."
        await callback.answer(undo.done)
        await _drop_restore_button(callback.message)
        return
    await callback.answer("⏪ Вернул")
    meal = result.meal
    what, kcal = escape(_names(meal)), round(meal.totals.kcal)
    days_ago = await _days_ago(app_user, assistant, where) if where[:1] == "d" else None
    if where == "p":
        await _redraw_picker(
            callback.message, app_user, assistant, f"⏪ Вернул: {what} · {kcal} ккал"
        )
    elif days_ago is not None:
        await _redraw_day(callback.message, app_user, assistant, days_ago, repeat=False)
    else:
        ctx, sess = await gather(assistant, app_user), await _session(app_user, assistant, meal.id)
        await _rich_edit(
            callback.message, screens.meal_card(ctx, result, restored=True, session=sess),
            meal_actions_keyboard(meal, None, sess),
            fallback_text=f"⏪ Вернул: {what} · {kcal} ккал",
        )


# --- «✏️ Вес» на карточке приёма (FR-43, D-MA-25) ------------------------------

_WEIGHT_GONE = "Этой позиции уже нет — вес не поправил."
_GRAMS_BOUNDS = (
    f"Вес — от {MIN_PORTION_G:g} до {MAX_PORTION_G:g} г за позицию. Пришли другое число."
)
_SMALL_MAX = 10  # голое число меньше — скорее «порции», чем граммы (AC-129)
_GRAMS_RE = re.compile(
    r"\s*[~≈]?\s*(\d{1,2}[ \u00a0\u202f]\d{3}|\d{1,5})(?:[.,](\d{1,3}))?\s*"
    r"(кг|kg|граммов|грамма|грамм|гр|г|g|мл|ml|литра|литр|л|l)?\.?\s*",
    re.IGNORECASE,
)
_THOUSAND = ("кг", "kg", "литра", "литр", "л", "l")  # мл/л — как граммы: объём ≈ масса (INV-7)
_TIMES_RE = re.compile(
    r"\s*(?:[xх×*]\s*(\d{1,2}(?:[.,]\d{1,2})?|½)|(\d{1,2}(?:[.,]\d{1,2})?)\s*[xх×]|(½))\s*",
    re.IGNORECASE,
)


def parse_grams_answer(text: str) -> tuple[float, bool] | None:
    """Ответ на «Сколько граммов?»: (граммы, единица названа). Одно число с необязательной
    г/кг/мл/л, «1 000 г», «~180». «150 г гречки», «банан» — None: это уже запись, а не ответ."""
    m = _GRAMS_RE.fullmatch(text or "")
    if m is None:
        return None
    whole, frac, unit = m.groups()
    grams = float(re.sub(r"\D", "", whole) + (f".{frac}" if frac else ""))
    if (unit or "").lower() in _THOUSAND:
        grams *= 1000
    return round(grams, 2), unit is not None


def parse_times_answer(text: str) -> float | None:
    """Множитель текущего веса: «x2», «×2», «х2» (кириллица), «*2», «2x», «½», «x 1,5»."""
    m = _TIMES_RE.fullmatch(text or "")
    if m is None:
        return None
    raw = next(g for g in m.groups() if g)
    k = 0.5 if raw == "½" else float(raw.replace(",", "."))
    return k if k > 0 else None


def _split_base(op: str) -> tuple[float, float] | None:
    """«300@150» → (граммы, вес-основа кнопки). Граммы — в 1…2000 г, всё конечное; иначе None."""
    grams_s, at, base_s = op.partition("@")
    try:
        grams, base = float(grams_s), float(base_s)
    except ValueError:
        return None
    ok = at and math.isfinite(grams) and math.isfinite(base) and base > 0
    return (grams, base) if ok and MIN_PORTION_G <= grams <= MAX_PORTION_G else None


def _moved(now: MealItem, base: float) -> bool:
    """Вес позиции уже не тот, от которого посчитана кнопка (поправили в аппке, словами)."""
    return abs(now.grams - base) >= 0.05


def _g(grams: float) -> str:
    return f"{round(grams, 1):g}"


async def _adjust_weight(
    user: User, assistant: Assistant, meal_id: int, item_id: int, grams: float
) -> tuple[MealLogged, str] | None:
    """Тот же `adjust_item`, что у шторки аппки (PATCH /api/meals/…/items/…): снапшот КБЖУ
    масштабируется пропорционально (INV-3), модели нет. None — позиции нет или у неё нет веса.
    Второе — след правки, как тост аппки: «гречка: было 150 г, стало 75 г»."""
    changed = await assistant.adjust_item(user, meal_id, item_id, grams)
    meal = await assistant.get_meal(user, meal_id) if changed else None
    if changed is None or meal is None:
        return None
    item, old = changed
    logged = MealLogged(meal=meal, remaining=(await assistant.today(user)).remaining)
    return logged, f"✏️ {item.name_snapshot}: было {_g(old)} г, стало {_g(item.grams)} г"


async def _weight_card(
    user: User, assistant: Assistant, logged: MealLogged
) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка после правки веса: «✏️ Поправил» (MA-INV-70) и обычные кнопки — без «⏪ Вернуть»:
    отпечаток приёма теперь другой, и снимок всё равно отказал бы (FR-41)."""
    ctx, sess = await gather(assistant, user), await _session(user, assistant, logged.meal.id)
    return (
        screens.meal_card(ctx, logged, edited=True, session=sess),
        meal_actions_keyboard(logged.meal, None, sess),
    )


async def _rich_edit_at(
    bot: Bot, chat_id: int, message_id: object, html: str, markup: InlineKeyboardMarkup | None
) -> bool:
    """`_rich_edit` по id: ответ на «✍️ Своё» — новое сообщение, а править надо карточку выше."""
    if not isinstance(message_id, int):
        return False
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, reply_markup=markup,
            rich_message=InputRichMessage(html=html, skip_entity_detection=True),
        )
    except TelegramBadRequest as exc:
        if "not modified" in str(exc).lower():
            return True
        logger.warning("правка карточки не удалась: %s", exc)
        return False
    return True


async def _say_prompt(
    bot: Bot, chat_id: int, prompt_id: object, text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Переписать вопрос «✍️ Своё» текстом без кнопок (markup — с этими). False — не вышло (нет
    id, >48 ч)."""
    if not isinstance(prompt_id, int):
        return False
    try:
        extra = {"reply_markup": markup} if markup is not None else {}
        await bot.edit_message_text(text=text, chat_id=chat_id, message_id=prompt_id, **extra)
    except TelegramBadRequest:
        return False
    return True


def _dropped(data: dict) -> str:
    return f"✍️ Вес «{escape(str(data.get('iw_name', '')), quote=False)}» — вопрос снят."


async def _drop_weight_question(bot: Bot | None, chat_id: int, state: FSMContext) -> None:
    """Снять висящий «✍️ Своё» (новый текст, голос, фото, любой тап `iw:`): состояние — сбросить,
    вопрос — в «вопрос снят». Иначе он остался бы сиротой с живой «✖️ Отмена» (AC-128, AC-130)."""
    if await state.get_state() != MealStates.awaiting_item_grams.state:
        return
    data = await state.get_data()
    await state.clear()
    if bot is not None:
        await _say_prompt(bot, chat_id, data.get("iw_prompt"), _dropped(data))


async def _finish_typed(
    message: Message, data: dict, user: User, assistant: Assistant, logged: MealLogged, trace: str
) -> None:
    """Правка из «✍️ Своё» записана: карточка выше — на месте (MA-INV-70; не вышло — новая
    «✏️ Поправил»), вопрос — след правки без кнопок (не вышло — следом отдельным сообщением)."""
    bot = message.bot
    assert bot is not None
    html, markup = await _weight_card(user, assistant, logged)
    if not await _rich_edit_at(bot, message.chat.id, data.get("iw_card"), html, markup):
        await _rich(message, html, markup)
    trace = escape(trace, quote=False)
    if not await _say_prompt(bot, message.chat.id, data.get("iw_prompt"), trace):
        await message.answer(trace)


def _cancel_row(meal_id: int, item_id: int) -> list[InlineKeyboardButton]:
    """Своя отмена вопроса — не общая `wiz_cancel`: та сбросила бы вопрос ДРУГОЙ карточки."""
    return [InlineKeyboardButton(
        text="✖️ Отмена", callback_data=f"{ITEM_WEIGHT_PREFIX}:{meal_id}:{item_id}:x"
    )]


def _ru(x: float) -> str:
    return f"{round(x, 2):g}".replace(".", ",")  # множитель «×0,96» — до сотых


async def _typed_item(user: User, assistant: Assistant, data: dict) -> MealItem | None:
    """Позиция висящего вопроса «✍️ Своё», какой она есть сейчас; None — её уже нет."""
    meal = await assistant.get_meal(user, int(data.get("iw_meal", 0)))
    item_id = int(data.get("iw_item", 0))
    return next((i for i in weighable(meal) if i.id == item_id), None) if meal else None


async def _ask_small(
    message: Message, user: User, assistant: Assistant, state: FSMContext, data: dict,
    value: float,
) -> None:
    """Голое число < 10 граммами не пишем (AC-129): меньше 1 («0,2») — скорее кг, `0,2 кг · 200 г`;
    1…2 — `2 г` / `×2 · 300 г` (множитель как у пресетов); больше — только `5 г` и просьба о
    единице. Уточнение заменяет прежний вопрос; состояние ждёт дальше — можно и написать вес."""
    item = await _typed_item(user, assistant, data)
    if item is None:
        await _drop_weight_question(message.bot, message.chat.id, state)
        await message.answer(_WEIGHT_GONE)
        return
    meal_id, item_id = int(data.get("iw_meal", 0)), int(data.get("iw_item", 0))
    head, base, at = f"{ITEM_WEIGHT_PREFIX}:{meal_id}:{item_id}", half_up(item.grams), item.grams
    rows: list[list[InlineKeyboardButton]] = []
    says: list[str] = []

    def offer(text: str, grams: float, say: str) -> None:
        data_ = f"{head}:={_g(grams)}@{at:g}"  # @основа — как у пресетов (вес мог поменяться)
        rows.append([InlineKeyboardButton(text=text, callback_data=data_)])
        says.append(say)

    kg = round(value * 1000, 1)
    if value < 1 and MIN_PORTION_G <= kg <= MAX_PORTION_G:
        offer(f"{_ru(value)} кг · {_g(kg)} г", kg, f"{_ru(value)} кг ({_g(kg)} г)")
    elif MIN_PORTION_G <= value:
        offer(f"{_ru(value)} г", value, f"{_ru(value)} г")
    times = half_up(base * value) if value <= 2 else 0
    if MIN_PORTION_G <= times <= MAX_PORTION_G and times != base:
        offer(f"×{_ru(value)} · {times} г", times, f"×{_ru(value)} порции")
    if not rows:
        await message.answer(_GRAMS_BOUNDS)
        return
    said = escape((message.text or "").strip(), quote=False)
    text = f"«{said}» — это {' или '.join(says)}? Сейчас {_g(item.grams)} г."
    if value > 2:
        text = (f"«{said}» — это {says[0]}? Если нет — пришли вес с единицей: «500 г» "
                "или «0,5 кг».")
    bot = message.bot
    assert bot is not None
    await _say_prompt(bot, message.chat.id, data.get("iw_prompt"), _dropped(data))
    rows.append(_cancel_row(meal_id, item_id))
    asked = await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await state.update_data(iw_prompt=asked.message_id)


async def _swap_keyboard(message: Message, markup: InlineKeyboardMarkup) -> None:
    """Навигация «✏️ Вес» меняет только кнопки: текст карточки не трогаем, чат не прыгает."""
    with suppress(TelegramBadRequest):  # двойной тап — «not modified»
        await message.edit_reply_markup(reply_markup=markup)


async def _weight_question_tap(
    callback: CallbackQuery, app_user: User, assistant: Assistant, session: AsyncSession,
    state: FSMContext, meal_id: int, item_id: int, op: str,
) -> None:
    """Кнопки под вопросом «✍️ Своё»: `x` — отменить ЭТОТ вопрос, `=<г>` — уточнение голого числа.
    Действуют, только если это и есть висящий вопрос: чужой (другой карточки) не трогаем."""
    message = callback.message
    assert isinstance(message, Message) and message.bot is not None and app_user.id is not None
    data = await state.get_data()
    mine = (
        await state.get_state() == MealStates.awaiting_item_grams.state
        and data.get("iw_prompt") == message.message_id
        and (data.get("iw_meal"), data.get("iw_item")) == (meal_id, item_id)
    )
    if not mine:
        await callback.answer("Этот вопрос уже снят.")
        with suppress(TelegramBadRequest):
            await message.edit_reply_markup(reply_markup=None)
        return
    if op == "x":
        await state.clear()
        await _say_prompt(message.bot, message.chat.id, message.message_id, _dropped(data))
        await callback.answer("Ок, отменил")
        return
    split = _split_base(op[1:])
    if split is None:
        await callback.answer("Кнопка устарела.")
        return
    (grams, base), trace, moved = split, "", None

    async def write() -> MealLogged | None:
        nonlocal trace, moved
        now = await _typed_item(app_user, assistant, data)
        if now is not None and _moved(now, base):
            moved = now  # «×2 · 300 г» считали от 150 — от нового веса это уже не ×2
            return None
        adjusted = await _adjust_weight(app_user, assistant, meal_id, item_id, grams)
        if adjusted is None:
            return None
        await state.clear()
        logged, trace = adjusted
        return logged

    result = await _write_once(
        callback, session, app_user.id, write, busy="Уже правлю…", done="Уже поправил."
    )
    if result is False:
        return
    if moved is not None:  # вопрос остаётся: число можно прислать заново
        await callback.answer("Вес уже поменялся — выбери заново.")
        await _say_prompt(
            message.bot, message.chat.id, message.message_id,
            f"✍️ Вес «{escape(moved.name_snapshot, quote=False)}» уже поменялся — сейчас "
            f"{_g(moved.grams)} г. Пришли число заново.",
        )
        return
    if result is None:
        await _drop_weight_question(message.bot, message.chat.id, state)
        await callback.answer(_WEIGHT_GONE)
        return
    await callback.answer(trace[:200])
    await _finish_typed(message, data, app_user, assistant, result, trace)


@router.callback_query(F.data.startswith(f"{ITEM_WEIGHT_PREFIX}:"))
async def on_item_weight(
    callback: CallbackQuery, app_user: User, assistant: Assistant, session: AsyncSession,
    state: FSMContext,
) -> None:
    """FR-43: «✏️ Вес» → позиция (если их с весом несколько) → ½ · ×1,5 · ×2 · ✍️ Своё.

    iw:<meal> открыть · iw:<meal>:back к карточке · iw:<meal>:<item> пресеты ·
    iw:<meal>:<item>:<г>@<вес> пресет · iw:<meal>:<item>:own спросить число ·
    iw:<meal>:<item>:=<г>@<вес> уточнение голого числа · iw:<meal>:<item>:x отменить вопрос.
    """
    parts = (callback.data or "").split(":")
    try:
        meal_id = int(parts[1])
        item_id = int(parts[2]) if len(parts) > 2 and parts[2] != "back" else None
    except (IndexError, ValueError):  # int(), а не isdigit(): тот пропустил бы «²»
        meal_id = item_id = None
    tail = parts[2:]
    op = tail[1] if len(tail) == 2 else None
    if (meal_id is None or len(tail) > 2 or (tail[:1] == ["back"] and len(tail) > 1)
            or not isinstance(callback.message, Message)):
        await callback.answer("Кнопка устарела.")
        return
    assert app_user.id is not None
    card, bot = callback.message, callback.message.bot
    if item_id is not None and op is not None and (op == "x" or op.startswith("=")):
        await _weight_question_tap(
            callback, app_user, assistant, session, state, meal_id, item_id, op
        )
        return
    if op != "own":  # «✍️ Своё» снимает прежний вопрос сам — уже под стражем двойного тапа
        await _drop_weight_question(bot, card.chat.id, state)
    meal = await assistant.get_meal(app_user, meal_id)
    if meal is None:
        await callback.answer(_GONE_CARD)
        return
    if tail == ["back"]:
        await _swap_keyboard(card, meal_actions_keyboard(
            meal, _live_restore(app_user, meal_id, "c", meal),
            await _session(app_user, assistant, meal.id),
        ))
        await callback.answer()
        return
    items = weighable(meal)
    if item_id is None and len(items) > 1:
        await _swap_keyboard(card, item_pick_keyboard(meal))
        await callback.answer()
        return
    if item_id is None:
        item = items[0] if items else None
    else:
        item = next((i for i in items if i.id == item_id), None)
    if item is None or item.id is None:
        await callback.answer(_WEIGHT_GONE)
        await _redraw_card(card, app_user, assistant, meal_id)
        return
    item_id, name = item.id, item.name_snapshot
    if op is None:
        await _swap_keyboard(card, item_weight_keyboard(meal, item))
        await callback.answer(f"✏️ {name[:60]} · сейчас {_g(item.grams)} г")
        return
    if op == "own":
        await _ask_grams(callback, app_user, assistant, session, state, meal, item)
        return
    split = _split_base(op)
    if split is None:
        await callback.answer("Кнопка устарела.")
        return
    (grams, base), trace, moved = split, "", None

    async def write() -> MealLogged | None:
        nonlocal trace, moved
        # Сверка — уже под стражем: второй тап слышит «Уже поправил.», а не «вес поменялся».
        fresh = await assistant.get_meal(app_user, meal_id)
        now = next((i for i in weighable(fresh) if i.id == item_id), None) if fresh else None
        if now is None:
            return None
        if _moved(now, base):
            moved = fresh, now  # вес поменяли (в аппке, словами): «×2» от 150 — не «½» от 600
            return None
        adjusted = await _adjust_weight(app_user, assistant, meal_id, item_id, grams)
        if adjusted is None:
            return None
        logged, trace = adjusted
        return logged

    result = await _write_once(
        callback, session, app_user.id, write, busy="Уже правлю…", done="Уже поправил."
    )
    if result is False:  # второй тап — карточку перерисовывает первый
        return
    if moved is not None:
        await callback.answer("Вес уже поменялся — выбери заново.")
        await _swap_keyboard(card, item_weight_keyboard(*moved))
        return
    if result is None:
        await callback.answer(_WEIGHT_GONE)
        await _redraw_card(card, app_user, assistant, meal_id)
        return
    await callback.answer(trace[:200])
    html, markup = await _weight_card(app_user, assistant, result)
    await _rich_edit(
        card, html, markup,
        fallback_text=f"{escape(trace)} · запись теперь {round(result.meal.totals.kcal)} ккал",
    )


async def _ask_grams(
    callback: CallbackQuery, app_user: User, assistant: Assistant, session: AsyncSession,
    state: FSMContext, meal: Meal, item: MealItem,
) -> None:
    """«✍️ Своё»: карточке — обычные кнопки, вопрос — отдельным сообщением со своей отменой.
    Под стражем двойного тапа: второй тап не шлёт второй вопрос (AC-130)."""
    card = callback.message
    assert isinstance(card, Message) and app_user.id is not None
    assert meal.id is not None and item.id is not None
    meal_id, item_id = meal.id, item.id

    async def ask() -> Message | None:
        await _drop_weight_question(card.bot, card.chat.id, state)  # вопрос другой карточки
        await state.set_state(MealStates.awaiting_item_grams)
        await _swap_keyboard(card, meal_actions_keyboard(
            meal, _live_restore(app_user, meal_id, "c", meal),
            await _session(app_user, assistant, meal.id),
        ))
        prompt = await card.answer(
            f"✍️ Сколько граммов: {escape(item.name_snapshot, quote=False)}? Сейчас "
            f"{_g(item.grams)} г. Пришли число — «180», «180 г» или «0,2 кг».",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[_cancel_row(meal_id, item_id)]),
        )
        await state.update_data(
            iw_meal=meal_id, iw_item=item_id, iw_card=card.message_id,
            iw_prompt=prompt.message_id, iw_name=item.name_snapshot,
        )
        return prompt

    asked = await _write_once(
        callback, session, app_user.id, ask,
        busy="Уже спрашиваю…", done="Уже спросил — жду число.",
    )
    if asked is not False:
        await callback.answer()


# --- «Поправить словами» ответом на карточку (FR-44, D-MA-26) ------------------------

_FIX_HINT = "Ответь на карточку: «это был булгур», «гречки было 150» или «удали гречку»."
_FIX_STALE = "Запись уже поменялась — ответь на карточку заново."
_NEW_MEAL = "➕ Это новая еда, а не правка — записал отдельно."
_NO_WEIGHT = "В этой записи нет позиции с весом — граммы не поправить."
_AMOUNT_RE = re.compile(r"\d|\b(?:г|гр|грамм\w*|кг|мл|л|шт)\b", re.IGNORECASE)


def _replied_meal(message: Message) -> int | None:
    """Id приёма карточки, на которую ответили: из callback_data её кнопок (D-MA-26). Хранить
    message_id → meal_id не нужно — связь уже лежит в самом сообщении и переживает рестарт.
    Чей приём — сверяет `get_meal` по пользователю, чужой id в кнопке ничего не даёт."""
    reply = message.reply_to_message
    return card_meal_id(reply.reply_markup) if reply is not None else None


def _fix_trace(r: MealCorrected) -> str:
    """След правки (без HTML): «✏️ Поправил: гречка → булгур 150 г · запись теперь 420 ккал»."""
    def named(i: MealItem) -> str:
        return f"{i.name_snapshot} {_g(i.grams)} г" if i.grams else i.name_snapshot

    if r.kind == "adjust":
        head = "; ".join(
            f"✏️ {o.name_snapshot}: было {_g(o.grams)} г, стало {_g(n.grams)} г"
            for o, n in zip(r.old, r.new, strict=False)
        )
    elif r.kind == "delete":
        head = "🗑 Убрал: " + ", ".join(i.name_snapshot for i in r.old)
    elif len(r.old) == len(r.new) > 1:  # каждая позиция на своём месте
        head = "✏️ Поправил: " + "; ".join(
            f"{o.name_snapshot} → {named(n)}" for o, n in zip(r.old, r.new, strict=True)
        )
    else:
        head = f"✏️ Поправил: {r.old[0].name_snapshot} → {', '.join(named(i) for i in r.new)}"
    tail = f"запись теперь {round(r.meal.totals.kcal)} ккал" if r.meal else "запись отменена"
    return f"{head} · {tail}"


def _fix_refusal(r: CorrectionFailed | Unknown) -> str:
    if isinstance(r, CorrectionFailed) and r.reason == "meal_gone":
        return _GONE_CARD
    if isinstance(r, CorrectionFailed) and r.reason == "one_at_a_time":
        return "Поправь по одной позиции за раз."
    if isinstance(r, CorrectionFailed) and r.reason == "not_found" and r.target:
        return f"В этой записи нет «{escape(r.target, quote=False)}». {_FIX_HINT}"
    return f"Не понял, что поправить. {_FIX_HINT}"


async def _show_fix(
    message: Message, user: User, assistant: Assistant, card_id: object, prompt_id: object,
    result: MealCorrected, snapshot: Meal | None = None,
) -> None:
    """Карточка, на которую ответили, — на месте «✏️ Поправил» (MA-INV-70), следом строка-след:
    правка старого сообщения беззвучна. Вопрос «что заменить» (prompt_id) сам становится следом.
    Карточку не приняли (>48 ч) — кнопки карточки едут со следом; нового «✅ Записал» нет.
    snapshot — приём до замены/удаления: «⏪ Вернуть», как у 🗑 (FR-41); у веса его нет (FR-43)."""
    bot = message.bot
    assert bot is not None
    restore = None
    if snapshot is not None and result.kind in ("fix", "delete"):  # фиксация уже была
        what = ", ".join(i.name_snapshot for i in result.old)
        restore = restore_button(_remember(user, snapshot, result.meal, what), "c", what)
    if result.meal is None:
        html = "<h3>🗑 Запись отменена</h3><p>Позиций не осталось.</p>"
        markup = InlineKeyboardMarkup(inline_keyboard=[[restore]] if restore else [])
    else:
        ctx, sess = await gather(assistant, user), await _session(user, assistant, result.meal.id)
        logged = MealLogged(meal=result.meal, remaining=result.remaining)
        html = screens.meal_card(ctx, logged, edited=True, session=sess)
        markup = meal_actions_keyboard(result.meal, restore, sess)
    keep = None if await _rich_edit_at(bot, message.chat.id, card_id, html, markup) else markup
    trace = escape(_fix_trace(result), quote=False)
    if not await _say_prompt(bot, message.chat.id, prompt_id, trace, keep):
        await message.answer(trace, reply_markup=keep)


def _weight_target(item: MealItem, weight: dict) -> float:
    """Граммы из ответа-числа: «200»/«0,2 кг» — как есть, «x2»/«½» — от текущего веса, как
    пресеты «✏️ Вес» (половина вверх, FR-43)."""
    if weight.get("times"):
        return float(half_up(half_up(item.grams) * weight["times"]))
    return float(weight["grams"])


async def _reply_weight(
    message: Message, user: User, assistant: Assistant, session: AsyncSession,
    state: FSMContext, meal_id: int, weight: dict,
) -> None:
    """«200», «150 г», «x2» ответом на карточку — вес позиции без модели: тот же разбор числа, что
    у «✍️ Своё» (FR-43), и тот же `adjust_item`. Позиций с весом несколько — кнопки позиций."""
    reply = message.reply_to_message
    assert reply is not None
    meal = await assistant.get_meal(user, meal_id)
    if meal is None:
        await message.answer(_GONE_CARD)
        return
    items = weighable(meal)
    if not items:
        await message.answer(_NO_WEIGHT)
        return
    if weight.get("grams") is not None and not weight["unit"] and weight["grams"] < _SMALL_MAX:
        said = escape((message.text or "").strip(), quote=False)
        await message.answer(  # «2» — скорее «две порции», чем 2 г (AC-129)
            f"«{said}» — это граммы или порции? Пришли с единицей: «{said} г» или «x{said}»."
        )
        return
    if len(items) > 1:
        label = escape((message.text or "").strip(), quote=False)
        asked = await message.answer(
            f"✏️ Чей вес «{label}»? Выбери позицию.", reply_markup=fix_pick_keyboard(meal, items)
        )
        await state.update_data(
            rf_meal=meal_id, rf_card=reply.message_id, rf_prompt=asked.message_id,
            rf_weight=weight,
        )
        return
    grams = _weight_target(items[0], weight)
    if not MIN_PORTION_G <= grams <= MAX_PORTION_G:
        await message.answer(_GRAMS_BOUNDS)
        return
    assert items[0].id is not None
    result = await assistant.adjust_meal_item(user, meal_id, items[0].id, grams)
    if result is None:
        await message.answer(_WEIGHT_GONE)
        return
    await session.commit()  # фиксируем ДО ответа — «Поправил» не врёт
    await _show_fix(message, user, assistant, reply.message_id, None, result)


async def _reply_fix(
    message: Message, user: User, assistant: Assistant, session: AsyncSession,
    state: FSMContext, meal_id: int,
) -> None:
    """FR-44: ответ на карточку → `correct_meal` (один разбор, тот же use-case, что у аппки)."""
    text = message.text or ""
    reply = message.reply_to_message
    assert reply is not None
    grams, times = parse_grams_answer(text), parse_times_answer(text)
    # Голое число — вес позиции, модель не нужна. «72,5 кг», «вес 72» — вес тела, не 72 500 г.
    if (grams is not None or times is not None) and not BODY_WEIGHT_RE.search(text):
        weight = {"grams": grams[0], "unit": grams[1]} if grams else {"times": times}
        await _reply_weight(message, user, assistant, session, state, meal_id, weight)
        return
    before = await assistant.get_meal(user, meal_id)
    snapshot = copy.deepcopy(before) if before is not None else None  # для «⏪ Вернуть»
    result = await assistant.correct_meal(user, meal_id, text)
    await session.commit()  # фиксируем ДО ответа — «Поправил» не врёт
    if isinstance(result, MealCorrected):
        await _show_fix(message, user, assistant, reply.message_id, None, result, snapshot)
    elif isinstance(result, ItemPickNeeded):
        what = ", ".join(i.name for i in result.draft.items if i.name.strip()) or text
        asked = await message.answer(
            f"✏️ Что заменить на «{escape(what, quote=False)}»?",
            reply_markup=fix_pick_keyboard(result.meal, result.meal.items),
        )
        await state.update_data(
            rf_meal=result.meal.id, rf_card=reply.message_id, rf_prompt=asked.message_id,
            rf_draft=result.draft, rf_text=text,
        )
    elif isinstance(result, CorrectionFailed) or (isinstance(result, Unknown) and not result.say):
        await message.answer(_fix_refusal(result))
    else:  # новая еда («ещё яблоко») — обычной записью, со строкой; вопрос, заготовка — как текст
        if isinstance(result, MealLogged | ClarificationNeeded | VariantClarificationNeeded):
            await message.answer(_NEW_MEAL)
        await _reply_message(message, result, state, user, assistant, raw=text)
        await _maybe_weight_chart(message, user, assistant, result)


async def _forget_fix(state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_data({k: v for k, v in data.items() if not k.startswith("rf_")})


async def _drop_fix_question(bot: Bot | None, chat_id: int, state: FSMContext) -> None:
    """Новый текст, голос, фото снимают висящий «что заменить» — не сирота с живыми кнопками."""
    prompt = (await state.get_data()).get("rf_prompt")
    if prompt is None:
        return
    await _forget_fix(state)
    if bot is not None:
        await _say_prompt(bot, chat_id, prompt, "✏️ Вопрос снят.")


async def _pick_write(
    user: User, assistant: Assistant, data: dict, item_id: int
) -> tuple[MealCorrected, Meal | None] | str:
    """Выбранная позиция висящего вопроса: замена разбором из ответа (модель второй раз не
    зовём) или вес из ответа-числа. Текст — отказ; позиции/приёма нет — `_FIX_STALE`."""
    meal_id = int(data.get("rf_meal") or 0)
    before = await assistant.get_meal(user, meal_id)
    weight = data.get("rf_weight")
    if weight:
        item = next((i for i in weighable(before) if i.id == item_id), None) if before else None
        if item is None:
            return _FIX_STALE
        grams = _weight_target(item, weight)
        if not MIN_PORTION_G <= grams <= MAX_PORTION_G:
            return _GRAMS_BOUNDS
        res = await assistant.adjust_meal_item(user, meal_id, item_id, grams)
        return (res, None) if res else _FIX_STALE
    snapshot = copy.deepcopy(before) if before else None
    # Копия разбора: упавшая запись не должна оставить его поправленным под старый вес.
    draft = copy.deepcopy(data.get("rf_draft"))
    if before is None or draft is None:
        return _FIX_STALE
    fixed = await assistant.fix_meal_item(
        user, meal_id, item_id, draft, str(data.get("rf_text") or "")
    )
    return (fixed, snapshot) if fixed else _FIX_STALE


async def _answer_fix_question(
    message: Message, user: User, assistant: Assistant, session: AsyncSession,
    state: FSMContext,
) -> bool:
    """Ответ на висящий «что заменить» — ответом на сам вопрос или голым именем («гречка», без
    чисел и единиц): позиция по имени (`items_named`), без модели. Иначе или не совпало ровно с
    одной — False: вопрос снимается, текст идёт обычным путём."""
    data = await state.get_data()
    if data.get("rf_prompt") is None:
        return False
    reply = message.reply_to_message
    to_question = reply is not None and reply.message_id == data.get("rf_prompt")
    # Выбор — ответ на сам вопрос или голое имя. «гречка 200 г» — обычная запись: съесть её
    # выбором значило бы молча заменить гречку черновиком (ревью FR-44).
    if not to_question and (reply is not None or _AMOUNT_RE.search(message.text or "")):
        return False
    meal = await assistant.get_meal(user, int(data.get("rf_meal") or 0))
    pool = (weighable(meal) if data.get("rf_weight") else meal.items) if meal else []
    hits = items_named(pool, message.text or "")
    if len(hits) != 1 or hits[0].id is None:
        return False
    done = await _pick_write(user, assistant, data, hits[0].id)
    if isinstance(done, str):
        await message.answer(done)
        if done == _FIX_STALE:
            await _drop_fix_question(message.bot, message.chat.id, state)
        return True
    await session.commit()
    await _forget_fix(state)
    await _show_fix(message, user, assistant, data.get("rf_card"), data.get("rf_prompt"), *done)
    return True


@router.callback_query(F.data.startswith(f"{FIX_PICK_PREFIX}:"))
async def on_fix_pick(
    callback: CallbackQuery, app_user: User, assistant: Assistant, session: AsyncSession,
    state: FSMContext,
) -> None:
    """FR-44: позиция для замены — rf:<meal>:<item>; rf:<meal>:x — отмена. Только висящий вопрос."""
    parts = (callback.data or "").split(":")
    message = callback.message
    try:
        meal_id = int(parts[1])
    except (IndexError, ValueError):
        meal_id = None
    if meal_id is None or len(parts) != 3 or not isinstance(message, Message):
        await callback.answer("Кнопка устарела.")
        return
    assert message.bot is not None and app_user.id is not None
    data = await state.get_data()
    if data.get("rf_prompt") != message.message_id or data.get("rf_meal") != meal_id:
        await callback.answer("Этот вопрос уже снят.")
        with suppress(TelegramBadRequest):
            await message.edit_reply_markup(reply_markup=None)
        return
    if parts[2] == "x":
        await _forget_fix(state)
        await _say_prompt(message.bot, message.chat.id, message.message_id, "✏️ Ок, не правлю.")
        await callback.answer("Ок, отменил")
        return
    try:
        item_id = int(parts[2])
    except ValueError:
        await callback.answer("Кнопка устарела.")
        return
    refusal = ""

    async def write() -> tuple[MealCorrected, Meal | None] | None:
        nonlocal refusal
        done = await _pick_write(app_user, assistant, data, item_id)
        if isinstance(done, str):
            refusal = done
            return None
        return done

    result = await _write_once(
        callback, session, app_user.id, write, busy="Уже правлю…", done="Уже поправил."
    )
    if result is False:
        return
    # Окно двойного тапа держит сам вопрос (снят — «уже снят»); ключ `rf:<meal>:<item>` не должен
    # мешать следующему вопросу про ту же позицию.
    _taps.pop((app_user.id, callback.data or ""), None)
    if result is None:
        await callback.answer(refusal[:200])
        if refusal == _FIX_STALE:
            await _forget_fix(state)
            await _say_prompt(message.bot, message.chat.id, message.message_id, f"✏️ {refusal}")
        return
    await _forget_fix(state)
    await callback.answer(_fix_trace(result[0])[:200])
    await _show_fix(message, app_user, assistant, data.get("rf_card"), message.message_id, *result)


@router.callback_query(F.data.startswith("prod_fix:"))
async def on_prod_fix(
    callback: CallbackQuery, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    product_id = int((callback.data or "prod_fix:0").split(":", 1)[1])
    product = await assistant.get_product(product_id)
    if product is None:
        await callback.answer("Продукт не найден.")
        return
    await state.set_state(MealStates.awaiting_density)
    await state.update_data(product_id=product_id)
    d = product.density
    await callback.message.answer(
        f"✏️ <b>{escape(product.display_name, quote=False)}</b>\n"
        f"Сейчас на 100 г: {d.kcal_per_100g:g} ккал · "
        f"Б{d.protein_per_100g:g} Ж{d.fat_per_100g:g} У{d.carb_per_100g:g}\n\n"
        f"Пришли 4 числа через пробел — ккал, белки, жиры, углеводы на 100 г.\n"
        f"Напр.: <code>165 31 3.6 0</code>\nИли переименуй кнопкой ниже.",
        reply_markup=prod_edit_keyboard(product_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith(f"{PROD_RENAME_PREFIX}:"))
async def on_prod_rename(
    callback: CallbackQuery, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    """FR-27 #6: кнопка «Переименовать» в /products → ждём новое имя текстом."""
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    product_id = int((callback.data or "prod_rename:0").split(":", 1)[1])
    await state.set_state(MealStates.awaiting_rename)
    await state.update_data(product_id=product_id)
    await callback.message.answer("✏️ Пришли новое название продукта.")
    await callback.answer()


@router.callback_query(ProfileStates.sex, F.data.startswith("prof_sex:"))
async def on_profile_sex(callback: CallbackQuery, state: FSMContext) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    sex = (callback.data or "prof_sex:male").split(":", 1)[1]
    await state.update_data(sex=sex)
    await state.set_state(ProfileStates.body)
    await callback.message.answer(
        "Пришли через пробел: <b>вес рост возраст</b>\nНапр.: <code>86 180 30</code>"
    )
    await callback.answer()


@router.callback_query(ProfileStates.activity, F.data.startswith("prof_act:"))
async def on_profile_activity(callback: CallbackQuery, state: FSMContext) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    activity = (callback.data or "prof_act:medium").split(":", 1)[1]
    await state.update_data(activity=activity)
    await state.set_state(ProfileStates.goal)
    await callback.message.answer("Какая цель?", reply_markup=goal_type_keyboard())
    await callback.answer()


@router.callback_query(ProfileStates.goal, F.data.startswith("prof_goal:"))
async def on_profile_goal(
    callback: CallbackQuery,
    app_user: User,
    assistant: Assistant,
    users: UserRepo,
    state: FSMContext,
) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    target = (callback.data or "prof_goal:maintain").split(":", 1)[1]
    data = await state.get_data()
    await state.clear()
    assert app_user.id is not None

    if not all(k in data for k in ("sex", "weight", "height", "age", "activity")):
        # визард потерял данные (рестарт/MemoryStorage) — не падаем KeyError, просим начать заново
        await callback.message.answer(
            "Профиль не достроился (бот перезапускался). Начни заново — ⚙️ Профиль."
        )
        await callback.answer()
        return
    goal = goal_from_profile(
        sex=data["sex"],
        weight_kg=float(data["weight"]),
        height_cm=float(data["height"]),
        age=int(data["age"]),
        activity=data["activity"],
        target=target,
    )
    await users.set_goal(app_user.id, goal)
    await assistant.log_weight(app_user, float(data["weight"]))
    await users.set_onboarded(app_user.id)  # профиль пройден → юзер онбординнут (FR-24)

    await _send_goal_card(callback.message, goal, app_user, assistant, header="✅ Дневная цель")
    if data.get("onboarding"):  # первый запуск → передаём к записи еды + справке
        await callback.message.answer(
            "🎉 Всё готово! Запиши, что съел, — например «куриная грудка 250 г».\n"
            "📍 /timezone — чтобы «день» статистики шёл по твоему поясу. Инструкция — /help"
        )
    await callback.answer()


@router.callback_query(F.data.startswith(f"{DAY_NAV_PREFIX}:"))
async def on_day_nav(callback: CallbackQuery, app_user: User, assistant: Assistant) -> None:
    """Вар. A: ← / → листание дней в просмотре истории; `:r` — тот же день в режиме повтора (FR-42).

    «дней назад», а не дата: экран сам относительный, перерисовывается целиком с заголовком дня —
    сдвиг на полуночи виден и безвреден. Дата нужна там, где тап ПИШЕТ (🔁) или возвращает (⏪)."""
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    parts = (callback.data or "day_nav:0").split(":")
    try:
        days_ago = max(0, int(parts[1]))
    except (IndexError, ValueError):
        days_ago = 0
    repeat = parts[2:] == [DAY_REPEAT_MODE]
    await _redraw_day(callback.message, app_user, assistant, days_ago, repeat)
    await callback.answer()


async def _redraw_day(
    message: Message, user: User, assistant: Assistant, days_ago: int, repeat: bool,
    note: str = "",
) -> None:
    """Экран «День» на месте: соседний день не «прыгает» в конец чата (edit rich, Bot API 10.1).
    «⏪ Вернуть» — только живая и только своего дня, и только в режиме 🗑 (`_day_restore`)."""
    rem, dv = await assistant.day_card(user, days_ago)
    kb = day_nav_keyboard(
        days_ago, dv.meals, dv.timezone, _day_restore(user, dv.day, dv.meals), repeat, dv.day
    )
    await _day_send(lambda html: _rich_edit(message, html, kb), dv, rem, repeat, note)


@router.callback_query(F.data.startswith(f"{DAY_REP_PREFIX}:"))
async def on_day_repeat(
    callback: CallbackQuery, app_user: User, assistant: Assistant, session: AsyncSession
) -> None:
    """FR-42: позиция (или приём целиком) с прошлого дня — в СЕГОДНЯ, тем же use-case, что у
    «↻ сегодня» аппки (`repeat_item` / `repeat_meal`, снапшот КБЖУ один-в-один, INV-3). Без модели.

    Человек остаётся на листаемом дне и в режиме повтора; строка-подсказка под днём становится
    следом записи — всплывашка гаснет через секунду (D-MA-24). День в callback — датой: после
    полуночи перерисуется ТОТ день, а не тот, что теперь «столько-то дней назад»."""
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    assert app_user.id is not None
    parts = (callback.data or "").split(":")
    try:
        meal_id = int(parts[2])
        item_id = int(parts[3]) if len(parts) == 4 else None
    except (IndexError, ValueError):
        meal_id = item_id = None
    days_ago = await _days_ago(app_user, assistant, parts[1]) if len(parts) > 2 else None
    if meal_id is None or days_ago is None or not parts[1].startswith("d") or len(parts) > 4:
        await callback.answer("Кнопка устарела — открой 🗒 История заново.")
        return

    async def write() -> MealLogged | None:
        if item_id is None:
            return await assistant.repeat_meal(app_user, meal_id)
        return await assistant.repeat_item(app_user, meal_id, item_id)

    result = await _write_once(callback, session, app_user.id, write)
    if result is False:  # «Уже записал…» — строка под днём уже говорит, что записано
        return
    if result is None:  # удалили (тут или в аппке) — лечим экран живыми данными, режим тот же
        await callback.answer("Этой записи уже нет — ничего не записал.")
        await _redraw_day(callback.message, app_user, assistant, days_ago, True)
        return
    meal, rem = result.meal, result.remaining
    if item_id is None:
        what = _names(meal)
    else:
        it = meal.items[0]
        what = it.name_snapshot + (f" · {round(it.grams)} г" if it.grams else "")
    what = what if len(what) <= 60 else what[:59] + "…"
    await callback.answer(f"🔁 Записал в сегодня: {what} · {round(meal.totals.kcal)} ккал")
    await _redraw_day(
        callback.message, app_user, assistant, days_ago, True,
        f"🔁 Записал в сегодня: {what} — сегодня {round(rem.consumed.kcal)} "
        f"из {round(rem.goal.daily_kcal)} ккал",
    )


@router.callback_query(F.data.startswith(f"{DAY_DEL_PREFIX}:"))
async def on_day_del(
    callback: CallbackQuery, app_user: User, assistant: Assistant, session: AsyncSession
) -> None:
    """Удалить ОДНУ позицию прямо с экрана «День»/«История» и перерисовать ТОТ ЖЕ день на месте.

    Позиция, а не запись целиком: сдвоенную запись (картофель+фарш) чистим по продукту. Если
    позиция была последней в приёме — remove_item удалит и сам приём.
    """
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    _, days_ago_s, meal_id_s, item_id_s = (callback.data or "day_del:0:0:0").split(":")
    days_ago, meal_id = max(0, int(days_ago_s)), int(meal_id_s)
    removed = await _remove_item(app_user, assistant, session, meal_id, int(item_id_s))
    if removed == "busy":
        await callback.answer("Уже убираю…")
        return
    if removed is not None:  # снимок помнит день: «⏪ Вернуть» — только на нём (FR-41)
        day = (await assistant.day_view(app_user, days_ago)).day
        undo = _trash.get(app_user.id or 0)
        if undo is not None and undo.token == removed[2]:  # за await его могли вытеснить
            undo.day = day
    # Уже убрано (двойной тап, в аппке) — экран лечим живыми данными, «⏪ Вернуть» — из снимка.
    await _redraw_day(callback.message, app_user, assistant, days_ago, repeat=False)
    await callback.answer("🗑 Убрал позицию" if removed else "Уже убрано.")


@router.callback_query(F.data.startswith(f"{GOAL_TARGET_PREFIX}:"))
async def on_goal_target(
    callback: CallbackQuery, app_user: User, users: UserRepo, assistant: Assistant
) -> None:
    """Сменить цель (похудение/поддержание/набор) с экрана 🎯 Цель — без визарда.

    Калории ОСТАВЛЯЕМ как есть, пересчитываем только раскладку: смена цели — это «как делить»,
    а не «сколько есть». Сколько — задаётся /goal или профилем.
    """
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    target = (callback.data or f"{GOAL_TARGET_PREFIX}:maintain").split(":", 1)[1]
    assert app_user.id is not None
    current = await assistant.current_goal(app_user)
    wkg = await _latest_weight(assistant, app_user)
    if wkg is None:
        await callback.answer("Сначала запиши вес — «вес 86.8»", show_alert=True)
        return
    goal = rebalance(current.daily_kcal, wkg, target)
    await users.set_goal(app_user.id, goal)
    await _rich_edit(
        callback.message, screens.goal_screen(goal, wkg), goal_target_keyboard(target)
    )
    await callback.answer(f"🎯 {screens.TARGET_RU.get(target, target)}")


@router.callback_query()  # замыкающий: ловит любую устаревшую кнопку (после рестарта/времени)
async def on_stale_callback(callback: CallbackQuery) -> None:
    """Не пойманный callback → гасим вечный спиннер Telegram и снимаем мёртвую клавиатуру."""
    await callback.answer("Кнопка устарела — открой меню заново")
    if isinstance(callback.message, Message):
        with suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=None)


# --- helpers ------------------------------------------------------------------


async def _start_profile(message: Message, state: FSMContext, *, app: bool = False) -> None:
    """app — кнопка «📱 Открыть в приложении» на настройки (FR-46): из «⚙️ Профиль» и /profile.
    Визарду первого запуска её не даём — выход в аппку оборвал бы настройку цели."""
    await state.clear()
    await state.set_state(ProfileStates.sex)
    await message.answer(
        "⚙️ Настрою дневную цель по профилю.\nТвой пол?",
        reply_markup=open_in_app("set", sex_keyboard()) if app else sex_keyboard(),
    )


async def _show_today(message: Message, app_user: User, assistant: Assistant) -> None:
    await _rich(message, screens.today(await gather(assistant, app_user)))


async def _show_week(
    message: Message, app_user: User, assistant: Assistant, scans: object | None = None
) -> None:
    await _rich(
        message, screens.week(await gather(assistant, app_user, scans)), open_in_app("tot-week")
    )


async def _show_weight(message: Message, app_user: User, assistant: Assistant) -> None:
    ctx = await gather(assistant, app_user)
    await _rich(message, screens.weight(ctx))
    if len(ctx.weight_points) >= 2:  # FR-8, D-40: линия через сами замеры — нужно хотя бы два
        png = render_weight_chart(
            ctx.weight_points, tz=ctx.weight.timezone, target=ctx.goal.target
        )
        await message.answer_photo(BufferedInputFile(png, "weight.png"))


async def _show_history(message: Message, app_user: User, assistant: Assistant) -> None:
    rem, dv = await assistant.day_card(app_user)  # день + стрелки ← →
    kb = day_nav_keyboard(dv.days_ago, dv.meals, dv.timezone, day=dv.day)
    await _day_send(lambda html: _rich(message, html, kb), dv, rem)


async def _day_send(
    send: Callable[[str], Awaitable[object]], dv: DayView, rem: Remaining,
    repeat: bool = False, note: str = "",
) -> None:
    """Экран дня — с запасным видом (AC-211): Telegram отверг вложенный список приёма пищи
    (FR-47) — тот же день плоско, а не упавший экран. Живой Bot API вложенный принял;
    ponytail: страховка на смену разметки, без отдельной ветки для каждого места."""
    try:
        await send(screens.day(dv, rem, repeat, note))
    except TelegramBadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        logger.warning("экран дня не принят, рисую плоско: %s", exc)
        await send(screens.day(dv, rem, repeat, note, flat=True))


async def _show_undo_picker(message: Message, app_user: User, assistant: Assistant) -> None:
    history = await assistant.today_meals(app_user)  # #12: весь сегодняшний день, не последние 5
    if not history.meals:
        await message.answer("Нечего отменять.")
        return
    await message.answer(
        _PICKER_HEAD,
        reply_markup=undo_picker_keyboard(history.meals, history.timezone),
    )


async def _send_typing(message: Message) -> None:
    with suppress(Exception):  # индикатор «печатает…» — не критично, если не отправится
        await message.bot.send_chat_action(message.chat.id, "typing")


def _parse_floats(text: str) -> list[float]:
    out: list[float] = []
    for token in text.replace(",", ".").split():
        try:
            out.append(float(token))
        except ValueError:
            return []
    return out


def _portion_word(text: str) -> str | None:
    """Размер порции словом. Обёртка над доменом: раньше здесь была своя копия,
    и «небольшая» понималась как БОЛЬШАЯ — подстрока «больш» перевешивала."""
    return parse_portion(text).size


def _first_number(text: str) -> float | None:
    m = re.search(r"\d+(?:[.,]\d+)?", text)
    return float(m.group().replace(",", ".")) if m else None


def _label_grams(
    text: str, package_g: float | None, serving_g: float | None = None
) -> float | None:
    """FR-27: вес из ответа на этикетку. Обёртка над доменом — то же правило
    нужно мини-аппу, а копия правила в двух адаптерах разъезжается."""
    return label_grams(text, package_g, serving_g)


async def _clear_intro_keyboard(message: Message, bot: Bot, state: FSMContext) -> None:
    """#7: снять висящую inline-клаву онбординг-интро, когда юзер действует вместо тапа по ней."""
    mid = (await state.get_data()).get("intro_msg_id")
    if mid:
        with suppress(Exception):
            await bot.edit_message_reply_markup(
                chat_id=message.chat.id, message_id=int(mid), reply_markup=None
            )


async def _busy_with_flow(
    message: Message, state: FSMContext, *, supersedes: str | tuple[str, ...] | None = None
) -> bool:
    """True, если идёт незавершённый вопрос/визард — голос/фото не должны затирать черновик.

    `supersedes` — состояние, которое новое сообщение вправе отменить само собой.
    Багрепорт #31: человек прислал ВТОРУЮ этикетку, не ответив про вес первой, и
    бот раз за разом требовал ответить про первую. Новое фото этикетки — это и есть
    отказ от предыдущей: держать его в заложниках у вопроса, на который человек
    уже передумал отвечать, значит запереть его без выхода.
    """
    current = await state.get_state()
    if current is None:
        return False
    allowed = (supersedes,) if isinstance(supersedes, str) else (supersedes or ())
    if current in allowed:
        if current == MealStates.awaiting_item_grams.state:  # вопрос «✍️ Своё» — «вопрос снят»
            await _drop_weight_question(message.bot, message.chat.id, state)
        if current == MealStates.awaiting_dish_name.state:  # вопрос имени набора — тоже (FR-47)
            await _drop_dish_question(message.bot, message.chat.id, state)
        await state.clear()
        return False
    await message.answer(
        "⏳ Сейчас жду ответ на прошлый шаг — ответь текстом или кнопкой "
        "(или ✖️ / кнопка меню, чтобы выйти). Голос и фото пока в сторонке."
    )
    return True


async def _rich(
    message: Message,
    html: str,
    markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None,
) -> Message:
    """Отправить экран единым Rich-стилем (Bot API 10.1).

    skip_entity_detection: иначе Telegram примет «1860/2200» за номер карты и подчеркнёт.
    """
    bot = message.bot
    assert bot is not None  # у полученного сообщения bot всегда привязан
    return await bot.send_rich_message(
        chat_id=message.chat.id,
        rich_message=InputRichMessage(html=html, skip_entity_detection=True),
        reply_markup=markup,
    )


async def _rich_edit(
    message: Message, html: str, markup: InlineKeyboardMarkup | None = None,
    *, fallback_text: str | None = None,
) -> None:
    """Править rich-сообщение НА МЕСТЕ (edit_text расширен полем rich_message в Bot API 10.1).

    Так навигация ←→ и правка карточки не «прыгают» в конец чата, а повторный тап по кнопке даёт
    «message is not modified» вместо второй карточки. Недоступно (>48 ч) — шлём заново.
    """
    try:
        await message.edit_text(
            rich_message=InputRichMessage(html=html, skip_entity_detection=True),
            reply_markup=markup,
        )
    except TelegramBadRequest as exc:
        if "not modified" in str(exc).lower():
            return  # тот же контент (двойной тап) — не ошибка
        logger.warning("правка карточки не удалась: %s", exc)
        if fallback_text:
            # MA-INV-70: вторая карточка «✅ Записал …» внизу чата выглядела записью заново.
            await message.answer(fallback_text, reply_markup=markup)  # «⏪ Вернуть» не теряется
            return
        await _rich(message, html, markup)


async def _latest_weight(assistant: Assistant, user: User) -> float | None:
    trend = await assistant.weight_trend(user, limit=1)
    return trend.logs[0].weight_kg if trend.logs else None


async def _send_meal_card(
    message: Message, result: MealLogged, app_user: User, assistant: Assistant
) -> None:
    """Карточка записи единым Rich-стилем: что записал + дневной прогресс всеми барами + кнопки.
    Запись — часть приёма пищи из нескольких записей: строка его суммы и «📌» на весь (FR-47)."""
    ctx = await gather(assistant, app_user)
    sess = await _session(app_user, assistant, result.meal.id)
    await _rich(
        message, screens.meal_card(ctx, result, session=sess),
        meal_actions_keyboard(result.meal, None, sess),
    )


async def _session(user: User, assistant: Assistant, meal_id: int | None) -> MealSession | None:
    """FR-47: приём пищи записи — только если в нём больше одной записи: иначе ни строки суммы,
    ни «📌 Приём пищи — в набор» (одна запись — её прежний «Сохранить набор»). Группу считает
    application (`session_of`): у бота своего правила разрыва нет (MA-INV-79).

    ponytail: +2 запроса на каждую карточку (запись и записи её дня) поверх `gather` — при двух
    людях и десятке карточек в день незаметно; потолок — отдать сессию из того же `day_view`."""
    if meal_id is None:
        return None
    found = await assistant.session_of(user, meal_id)
    return found if found is not None and len(found.meals) > 1 else None


async def _reply_screen(
    message: Message, result: Result, app_user: User, assistant: Assistant
) -> None:
    """Финальный результат (не вопрос) — единым Rich-стилем, каждый тип на свой экран."""
    if isinstance(result, MealLogged):
        await _send_meal_card(message, result, app_user, assistant)
    elif isinstance(result, DailyStats):  # intent=QUERY «сколько осталось»
        await _rich(message, screens.today(await gather(assistant, app_user)))
    elif isinstance(result, ItemRemoved):
        await _rich(message, screens.item_removed(result))
    elif isinstance(result, ItemAdjusted):
        await _rich(message, screens.item_adjusted(result))
    elif isinstance(result, DishSaved) and result.product.id:  # FR-46: состав — в аппке
        await message.answer(
            format_result(result), reply_markup=open_in_app(f"recipe-{result.product.id}")
        )
    else:  # WeightLogged / CorrectionFailed / Unknown — короткий текст + возможный график веса
        await message.answer(format_result(result))
        await _maybe_weight_chart(message, app_user, assistant, result)


async def _ask_clarification(
    message: Message, result: Result, state: FSMContext, *, raw: str, edit: bool
) -> None:
    """Вопрос-уточнение (порция/вариант) — короткий текст с клавиатурой. edit: править или слать."""
    if isinstance(result, ClarificationNeeded):
        await state.set_state(MealStates.awaiting_portion)
        await state.update_data(draft=result.draft, item_index=result.item_index, raw=raw)
        options = result.draft.items[result.item_index].portion_options
        if edit:
            await _safe_edit(message, format_result(result), portion_keyboard(options))
        else:
            await message.answer(format_result(result), reply_markup=portion_keyboard(options))
    else:
        assert isinstance(result, VariantClarificationNeeded)
        await state.set_state(MealStates.awaiting_variant)
        if edit:
            await state.update_data(
                draft=result.draft,
                item_index=result.item_index,
                raw=raw,
                q_msg_id=message.message_id,
            )
            await _safe_edit(message, format_result(result), variant_keyboard(result.options))
        else:
            sent = await message.answer(
                format_result(result), reply_markup=variant_keyboard(result.options)
            )
            await state.update_data(
                draft=result.draft,
                item_index=result.item_index,
                raw=raw,
                q_msg_id=sent.message_id,
            )


async def _reply_message(
    message: Message,
    result: Result,
    state: FSMContext,
    app_user: User,
    assistant: Assistant,
    *,
    raw: str,
) -> None:
    if isinstance(result, ClarificationNeeded | VariantClarificationNeeded):
        await _ask_clarification(message, result, state, raw=raw, edit=False)
    else:
        await _reply_screen(message, result, app_user, assistant)


async def _render_after_continue(
    message: Message,
    result: Result,
    state: FSMContext,
    app_user: User,
    assistant: Assistant,
    *,
    raw: str,
) -> None:
    """После ответа на уточнение: следующий вопрос правим на месте, финал — новым Rich-сообщением.

    Rich нельзя вклеить в старое сообщение-вопрос (нет editRichMessage), поэтому на финале вопрос
    удаляем и шлём карточку заново.
    """
    if isinstance(result, ClarificationNeeded | VariantClarificationNeeded):
        await _ask_clarification(message, result, state, raw=raw, edit=True)
        return
    await state.clear()
    if isinstance(result, MealLogged | DailyStats | ItemRemoved | ItemAdjusted):
        with suppress(TelegramBadRequest):
            await message.delete()  # сообщение-вопрос убираем — rich не вклеить в него
        await _reply_screen(message, result, app_user, assistant)
    else:
        await _safe_edit(message, format_result(result))


def _weight_points(trend: WeightTrend) -> list[tuple[datetime, float]]:
    return [(log.measured_at, log.weight_kg) for log in reversed(trend.logs)]  # старые→новые


async def _send_goal_card(
    message: Message, goal: Goal, app_user: User, assistant: Assistant, *, header: str = ""
) -> None:
    """Экран цели единым Rich-стилем; header — необязательная строка над таблицей.

    Кнопки цели — под сообщением: сменить одно поле, не проходя весь визард заново.
    """
    wkg = await _latest_weight(assistant, app_user)
    head = f"<p><b>{header}</b></p>" if header else ""
    await _rich(message, head + screens.goal_screen(goal, wkg), goal_target_keyboard(goal.target))


async def _maybe_weight_chart(
    message: Message, app_user: User, assistant: Assistant, result: Result
) -> None:
    """После записи веса — бонусом тот же график, что на «⚖️ Вес» (FR-7, FR-8), если замеров ≥ 2."""
    if not isinstance(result, WeightLogged):
        return
    trend = await assistant.weight_trend(app_user, limit=60)  # глубина — как у экрана (gather)
    if len(trend.logs) >= 2:
        goal = await assistant.current_goal(app_user)
        png = render_weight_chart(_weight_points(trend), tz=trend.timezone, target=goal.target)
        await message.answer_photo(BufferedInputFile(png, "weight.png"))


async def _safe_edit(message: Message, text: str, markup: object = None) -> None:
    """edit_text, проглатывая 'message is not modified' (идентичная правка — не ошибка)."""
    try:
        await message.edit_text(text, reply_markup=markup)  # type: ignore[arg-type]
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            raise
