"""Хендлеры aiogram. Тонкий слой: ввод -> Assistant -> форматтер.

UX: постоянное reply-меню, командное меню, «печатает…».
Отмена: контекстная (inline на свежей записи) — без вопроса; меню «Отменить» — выбор.
Каталог: /products → правка КБЖУ. Профиль: /profile → автоцель по Mifflin.
Надёжность: глобальный error-handler ловит падения (в т.ч. AI) и отвечает юзеру.
"""

from __future__ import annotations

import logging
import re
from contextlib import suppress
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardMarkup,
    InputRichMessage,
    Message,
    ReplyKeyboardMarkup,
)
from openai import RateLimitError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.assistant import Assistant
from app.application.dto import Unit
from app.application.entities import User
from app.application.ports import UserRepo
from app.application.results import (
    ClarificationNeeded,
    DailyStats,
    ItemAdjusted,
    ItemRemoved,
    LabelRead,
    MealLogged,
    Result,
    VariantClarificationNeeded,
    WeightLogged,
    WeightTrend,
)
from app.bot.dashboard_image import render_weight_chart  # единственный уцелевший PNG — линия веса
from app.bot.formatters import format_result  # текстовые подсказки внутри диалога (не экраны)
from app.bot.keyboards import (
    BTN_BUG,
    BTN_GOAL,
    BTN_HISTORY,
    BTN_PROFILE,
    BTN_TODAY,
    BTN_UNDO,
    BTN_WEEK,
    BTN_WEIGHT,
    DAY_DEL_PREFIX,
    DAY_NAV_PREFIX,
    GOAL_TARGET_PREFIX,
    LABEL_G_PREFIX,
    ONBOARD_PREFIX,
    PROD_RENAME_PREFIX,
    WIZ_CANCEL,
    activity_keyboard,
    day_nav_keyboard,
    goal_target_keyboard,
    goal_type_keyboard,
    label_grams_keyboard,
    location_request_keyboard,
    main_menu_keyboard,
    meal_actions_keyboard,
    onboarding_intro_keyboard,
    portion_keyboard,
    prod_edit_keyboard,
    products_fix_keyboard,
    sex_keyboard,
    undo_picker_keyboard,
    variant_keyboard,
)
from app.bot.states import MealStates, ProfileStates
from app.bot.ui import gather, screens
from app.domain.goals import Goal, goal_from_macros, goal_from_profile, rebalance, set_macro
from app.domain.timeframe import tz_from_longitude, tz_from_utc_offset
from app.domain.value_objects import Density

router = Router()
logger = logging.getLogger(__name__)


# --- надёжность: единый перехват ошибок ---------------------------------------


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    logger.exception("Unhandled error in handler", exc_info=event.exception)
    target = event.update.message
    if target is None and event.update.callback_query is not None:
        maybe = event.update.callback_query.message
        target = maybe if isinstance(maybe, Message) else None
    if target is not None:
        if isinstance(event.exception, RateLimitError):  # баг #16: 429 от OpenAI (лимит TPM)
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
            "Запиши первый приём, например «куриная грудка 250 г». Полная инструкция — /help",
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
async def cmd_week(message: Message, app_user: User, assistant: Assistant) -> None:
    await _show_week(message, app_user, assistant)


@router.message(Command("card"))
async def cmd_card(message: Message, app_user: User, assistant: Assistant) -> None:
    """Карточка дня для шеринга — тот же экран дня, но без стрелок навигации."""
    rem, dv = await assistant.day_card(app_user)
    await _rich(message, screens.day(dv, rem))


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
    if not products:
        await _rich(message, screens.products(products, query))  # пустой экран сам объяснит
        return
    await _rich(message, screens.products(products, query), products_fix_keyboard(products))


@router.message(Command("profile"))
async def cmd_profile(message: Message, state: FSMContext) -> None:
    await _start_profile(message, state)


# --- кнопки постоянного меню (приходят как обычный текст) ----------------------


@router.message(F.text == BTN_TODAY)
async def btn_today(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    await state.clear()
    await _show_today(message, app_user, assistant)


@router.message(F.text == BTN_WEEK)
async def btn_week(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    await state.clear()
    await _show_week(message, app_user, assistant)


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
    await _start_profile(message, state)


@router.message(F.text == BTN_UNDO)
async def btn_undo(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    await state.clear()
    await _show_undo_picker(message, app_user, assistant)


_BUG_PROMPT = "🐛 Опиши, что не так — одним сообщением. Приложу последние записи."
_BUG_DONE = "🐛 Спасибо! Записал багрепорт и последние приёмы — гляну."


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
    await message.answer("🐛 Спасибо! Скриншот и последние приёмы записал — гляну.")


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
            "Отправь приём заново."
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
            "Отправь приём заново."
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
) -> None:
    if await _busy_with_flow(message, state):  # не затираем незавершённый черновик голосом
        return
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
    result = await assistant.process(app_user, transcript, input_type="voice")
    await session.commit()
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
) -> None:
    """Фото = этикетка (FR-27): читаем КБЖУ → спрашиваем вес порции. (Фото готовой еды пока нет.)"""
    if await _busy_with_flow(message, state):  # не затираем незавершённый черновик фото
        return
    await state.clear()
    if not message.photo:
        return
    await _send_typing(message)
    photo = message.photo[-1]  # последний = максимальное разрешение
    buffer = await bot.download(photo)
    image = buffer.read() if buffer is not None else b""
    result = await assistant.read_label(app_user, image, photo_file_id=photo.file_id)
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
            final = await assistant.continue_meal(app_user, result.draft, raw_input="этикетка")
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
    else:
        await message.answer(format_result(result))


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
    result = await assistant.continue_meal(app_user, draft, raw_input="этикетка")
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
    result = await assistant.continue_meal(app_user, draft, raw_input="этикетка")
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
) -> None:
    await _clear_intro_keyboard(message, bot, state)  # #7: гасим висящее интро-меню онбординга
    await state.clear()
    await _send_typing(message)
    result = await assistant.process(app_user, message.text or "", input_type="text")
    await session.commit()  # фиксируем запись ДО ответа — «✅ записал» не врёт
    await _reply_message(message, result, state, app_user, assistant, raw=message.text or "")
    await _maybe_weight_chart(message, app_user, assistant, result)


@router.message(
    StateFilter(
        MealStates.awaiting_portion,
        MealStates.awaiting_variant,
        MealStates.awaiting_density,
        MealStates.awaiting_rename,
        MealStates.awaiting_label_grams,
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
        await callback.message.answer("Запись уже удалена — повторять нечего.")
        return
    await _send_meal_card(callback.message, result, app_user, assistant)


@router.callback_query(F.data.startswith("meal_undo:"))
async def on_meal_undo(callback: CallbackQuery, app_user: User, assistant: Assistant) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    meal_id = int((callback.data or "meal_undo:0").split(":", 1)[1])
    result = await assistant.delete_meal(app_user, meal_id)
    if result.meal is None:
        await callback.answer("Уже удалено.")
        with suppress(TelegramBadRequest):
            await callback.message.delete()
        return
    kcal = round(result.meal.totals.kcal)
    if (callback.message.text or "").startswith("Что отменить"):  # пикер /undo → перерисовать
        history = await assistant.today_meals(app_user)
        if history.meals:
            await _safe_edit(
                callback.message,
                "Что отменить? (по позициям)",
                undo_picker_keyboard(history.meals, history.timezone),
            )
        else:
            await _safe_edit(callback.message, "🗒 Больше нечего отменять — старое в 🗒 История.")
    else:  # контекстная отмена под rich-карточкой: править нельзя — просто убираем её
        with suppress(TelegramBadRequest):
            await callback.message.delete()
    await callback.answer(f"🗑 Отменил приём ({kcal} ккал)")


@router.callback_query(F.data.startswith("item_del:"))
async def on_item_del(callback: CallbackQuery, app_user: User, assistant: Assistant) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    parts = (callback.data or "item_del:0:0").split(":")
    meal_id, item_id = int(parts[1]), int(parts[2])
    result = await assistant.remove_item(app_user, meal_id, item_id)
    if result is None:
        await _rich_edit(callback.message, "<h3>🗑 Запись отменена</h3><p>Позиций не осталось.</p>")
    else:
        # Правим карточку НА МЕСТЕ: повторный тап по кнопке (remove_item вернёт тот же приём без
        # изменений) даст «not modified», а не вторую карточку.
        ctx = await gather(assistant, app_user)
        await _rich_edit(
            callback.message, screens.meal_card(ctx, result), meal_actions_keyboard(result.meal)
        )
    await callback.answer("🗑 Убрал позицию")


@router.callback_query(F.data.startswith("undo_item:"))
async def on_undo_item(callback: CallbackQuery, app_user: User, assistant: Assistant) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    parts = (callback.data or "undo_item:0:0").split(":")
    meal_id, item_id = int(parts[1]), int(parts[2])
    await assistant.remove_item(app_user, meal_id, item_id)  # удаляем одну позицию
    await callback.answer("🗑 Убрано")
    history = await assistant.history(app_user, limit=5)  # перерисовываем меню без неё
    if history.meals:
        markup = undo_picker_keyboard(history.meals, history.timezone)
        with suppress(Exception):  # игнор «message is not modified»
            await callback.message.edit_text("Что отменить? (по позициям)", reply_markup=markup)
    else:
        with suppress(Exception):
            await callback.message.edit_text("🗒 Больше нечего отменять.")


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
            "🎉 Всё готово! Запиши первый приём — например «куриная грудка 250 г».\n"
            "📍 /timezone — чтобы «день» статистики шёл по твоему поясу. Инструкция — /help"
        )
    await callback.answer()


@router.callback_query(F.data.startswith(f"{DAY_NAV_PREFIX}:"))
async def on_day_nav(callback: CallbackQuery, app_user: User, assistant: Assistant) -> None:
    """Вар. A: ← / → листание дней в просмотре истории."""
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    days_ago = max(0, int((callback.data or "day_nav:0").split(":", 1)[1]))
    rem, dv = await assistant.day_card(app_user, days_ago)
    # правим карточку НА МЕСТЕ — соседний день не «прыгает» в конец чата (edit rich, Bot API 10.1)
    await _rich_edit(
        callback.message, screens.day(dv, rem), day_nav_keyboard(days_ago, dv.meals, dv.timezone)
    )
    await callback.answer()


@router.callback_query(F.data.startswith(f"{DAY_DEL_PREFIX}:"))
async def on_day_del(callback: CallbackQuery, app_user: User, assistant: Assistant) -> None:
    """Удалить ОДНУ позицию прямо с экрана «День»/«История» и перерисовать ТОТ ЖЕ день на месте.

    Позиция, а не запись целиком: сдвоенную запись (картофель+фарш) чистим по продукту. Если
    позиция была последней в приёме — remove_item удалит и сам приём.
    """
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    _, days_ago_s, meal_id_s, item_id_s = (callback.data or "day_del:0:0:0").split(":")
    days_ago = max(0, int(days_ago_s))
    await assistant.remove_item(app_user, int(meal_id_s), int(item_id_s))
    rem, dv = await assistant.day_card(app_user, days_ago)  # состояние дня ПОСЛЕ удаления
    await _rich_edit(
        callback.message, screens.day(dv, rem), day_nav_keyboard(days_ago, dv.meals, dv.timezone)
    )
    await callback.answer("🗑 Убрал позицию")


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


async def _start_profile(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ProfileStates.sex)
    await message.answer(
        "⚙️ Настрою дневную цель по профилю.\nТвой пол?", reply_markup=sex_keyboard()
    )


async def _show_today(message: Message, app_user: User, assistant: Assistant) -> None:
    await _rich(message, screens.today(await gather(assistant, app_user)))


async def _show_week(message: Message, app_user: User, assistant: Assistant) -> None:
    await _rich(message, screens.week(await gather(assistant, app_user)))


async def _show_weight(message: Message, app_user: User, assistant: Assistant) -> None:
    ctx = await gather(assistant, app_user)
    await _rich(message, screens.weight(ctx))
    if ctx.trend_ok:  # линию рисуем PNG, только когда тренд честно есть из чего строить
        await message.answer_photo(
            BufferedInputFile(render_weight_chart(_weight_points(ctx.weight)), "weight.png")
        )


async def _show_history(message: Message, app_user: User, assistant: Assistant) -> None:
    rem, dv = await assistant.day_card(app_user)  # день + стрелки ← →
    await _rich(message, screens.day(dv, rem), day_nav_keyboard(dv.days_ago, dv.meals, dv.timezone))


async def _show_undo_picker(message: Message, app_user: User, assistant: Assistant) -> None:
    history = await assistant.today_meals(app_user)  # #12: весь сегодняшний день, не последние 5
    if not history.meals:
        await message.answer("Нечего отменять.")
        return
    await message.answer(
        "Что отменить? (по позициям)",
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
    low = text.lower()
    if "мал" in low:
        return "small"
    if "средн" in low:
        return "medium"
    if "больш" in low:
        return "large"
    return None


def _first_number(text: str) -> float | None:
    m = re.search(r"\d+(?:[.,]\d+)?", text)
    return float(m.group().replace(",", ".")) if m else None


def _label_grams(
    text: str, package_g: float | None, serving_g: float | None = None
) -> float | None:
    """FR-27: вес из ответа на этикетку. «вся пачка»→нетто; «N порц»→N×вес порции; иначе число г."""
    low = text.lower().strip()
    if package_g and ("пачк" in low or "упаков" in low or "цел" in low or low in ("всё", "все")):
        return package_g
    n = _first_number(text)
    if "порц" in low:  # «2 порции» = 2×вес порции (#10); без веса порции — не угадываем граммы
        return (n or 1) * serving_g if serving_g else None
    return n if n and n > 0 else None


async def _clear_intro_keyboard(message: Message, bot: Bot, state: FSMContext) -> None:
    """#7: снять висящую inline-клаву онбординг-интро, когда юзер действует вместо тапа по ней."""
    mid = (await state.get_data()).get("intro_msg_id")
    if mid:
        with suppress(Exception):
            await bot.edit_message_reply_markup(
                chat_id=message.chat.id, message_id=int(mid), reply_markup=None
            )


async def _busy_with_flow(message: Message, state: FSMContext) -> bool:
    """True, если идёт незавершённый вопрос/визард — голос/фото не должны затирать черновик."""
    if await state.get_state() is None:
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
    message: Message, html: str, markup: InlineKeyboardMarkup | None = None
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
        await _rich(message, html, markup)


async def _latest_weight(assistant: Assistant, user: User) -> float | None:
    trend = await assistant.weight_trend(user, limit=1)
    return trend.logs[0].weight_kg if trend.logs else None


async def _send_meal_card(
    message: Message, result: MealLogged, app_user: User, assistant: Assistant
) -> None:
    """Карточка приёма единым Rich-стилем: что записал + дневной прогресс всеми барами + кнопки."""
    ctx = await gather(assistant, app_user)
    await _rich(message, screens.meal_card(ctx, result), meal_actions_keyboard(result.meal))


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
    """После записи веса — бонусом график тренда (если есть ≥2 замеров)."""
    if not isinstance(result, WeightLogged):
        return
    trend = await assistant.weight_trend(app_user)
    if len(trend.logs) >= 2:
        await message.answer_photo(
            BufferedInputFile(render_weight_chart(_weight_points(trend)), "weight.png")
        )


async def _safe_edit(message: Message, text: str, markup: object = None) -> None:
    """edit_text, проглатывая 'message is not modified' (идентичная правка — не ошибка)."""
    try:
        await message.edit_text(text, reply_markup=markup)  # type: ignore[arg-type]
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            raise
