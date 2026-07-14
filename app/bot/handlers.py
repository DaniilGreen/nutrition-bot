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
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputRichMessage,
    Message,
)
from openai import RateLimitError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.assistant import Assistant
from app.application.dto import Unit
from app.application.entities import User
from app.application.ports import UserRepo
from app.application.results import (
    ClarificationNeeded,
    LabelRead,
    MealLogged,
    Result,
    VariantClarificationNeeded,
    WeightLogged,
    WeightTrend,
)
from app.bot.dashboard_image import (
    render_dashboard,
    render_day_card,
    render_goal_card,
    render_heatmap,
    render_label_card,
    render_month_card,
    render_today,
    render_weekly_chart,
    render_weight_chart,
)
from app.bot.formatters import (
    day_photo_caption,
    format_goal,
    format_result,
    meal_photo_caption,
    today_photo_caption,
)
from app.bot.keyboards import (
    BTN_BUG,
    BTN_DEMO,
    BTN_GOAL,
    BTN_HISTORY,
    BTN_PROFILE,
    BTN_TODAY,
    BTN_UNDO,
    BTN_WEEK,
    BTN_WEIGHT,
    DAY_NAV_PREFIX,
    LABEL_G_PREFIX,
    ONBOARD_PREFIX,
    PROD_RENAME_PREFIX,
    WIZ_CANCEL,
    activity_keyboard,
    day_nav_keyboard,
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
from app.bot.rich_demo import build_demo_markdown
from app.bot.states import MealStates, ProfileStates
from app.domain.goals import Goal, goal_from_profile
from app.domain.timeframe import tz_from_longitude, tz_from_utc_offset
from app.domain.value_objects import Density

router = Router()
logger = logging.getLogger(__name__)

START_TEXT = (
    "👋 Привет! Я считаю КБЖУ — просто расскажи, что съел.\n\n"
    "Текстом или голосом:\n"
    "• «куриная грудка 250 г»\n"
    "• «два банана»\n"
    "• «фо бо средняя порция»\n\n"
    "📷 Фото этикетки — пришли снимок таблицы КБЖУ, прочитаю цифры сам\n"
    "⚖️ Вес тела: «вес 86.8»\n"
    "🎯 Твоя дневная норма КБЖУ — посчитаю по профилю: /profile (пол·вес·рост·возраст)\n"
    "📖 Полная инструкция — /help\n"
    "Кнопки меню — снизу 👇"
)

HELP_TEXT = (
    "📖 <b>Как пользоваться</b>\n\n"
    "Я считаю калории, белки, жиры и углеводы по твоим сообщениям и показываю, "
    "сколько осталось до дневной цели. Пиши или наговаривай свободно — разберу сам.\n\n"
    "🍽 <b>Записать еду</b>\n"
    "• «куриная грудка 250 г» — точный вес\n"
    "• «два банана», «3 яйца» — штуки\n"
    "• «ложка мёда», «стакан кефира» — бытовые меры\n"
    "• «фо бо», «шакшука» — хватит и одного названия блюда\n"
    "🎤 Голос работает так же — наговори как удобно.\n\n"
    "🤚 <b>Не указал сколько?</b>\n"
    "На «овсянку» или «рис» без количества спрошу размер — жми «Маленькая / Средняя / Большая» "
    "(или впиши граммы). Можно сразу словом: «большая тарелка борща».\n\n"
    "📷 <b>Фото этикетки</b>\n"
    "Пришли снимок таблицы пищевой ценности — прочитаю КБЖУ сам, спрошу только вес порции.\n\n"
    "🏷 <b>Знаешь КБЖУ из меню/этикетки?</b>\n"
    "Назови цифры прямо: «рис с гречей из кафе, 400 ккал, 30 белка, 10 жира, 50 углеводов» — "
    "запишу ровно так, без пересчёта (или просто сфоткай этикетку — см. выше).\n\n"
    "🤔 <b>Уточню вариант</b>\n"
    "Для «творог» или «курица» спрошу: 5% / 9%, гриль / отварная — способ влияет на калории. "
    "Можно уточнить сразу: «творог 9%», «курица гриль».\n\n"
    "⚖️ <b>Вес тела</b>\n"
    "«вес 86.8» — запишу и покажу тренд (кнопка ⚖️ Вес).\n\n"
    "✏️ <b>Исправить</b>\n"
    "• «удали рис» — уберу позицию\n"
    "• ↩️ Отменить — снять последнюю запись или отдельный продукт из неё\n\n"
    "📊 <b>Посмотреть</b> (всё картинками)\n"
    "• 📊 Сегодня — съедено, остаток и кольцо Б/Ж/У\n"
    "• 📈 Неделя — график калорий по дням\n"
    "• 🗒 История — день со стрелками ← →\n"
    "• /month — отчёт за 30 дней\n"
    "• /streak — календарь «дней в цели»\n"
    "• /card — карточка дня для шеринга\n\n"
    "🎯 <b>Настрой под себя</b> — так остаток будет точным:\n"
    "• ⚙️ Профиль — пол, вес, рост, возраст, цель → посчитаю норму\n"
    "• /goal 1800 — задать цель вручную\n"
    "• /products — поправить КБЖУ, если оценил неточно (помечено «≈»)\n"
    "• /timezone — часовой пояс по геолокации, чтобы «день» совпадал с твоим\n\n"
    "💡 Чем точнее профиль и каталог — тем точнее учёт. Сомневаешься в цифре? Глянь /products."
)


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
async def on_start(
    message: Message, app_user: User, users: UserRepo, state: FSMContext
) -> None:
    await state.clear()
    if app_user.onboarded_at is None and app_user.id is not None:
        # первый запуск (FR-24/D-12): приветствие + предложение настроить профиль-визард
        await message.answer(START_TEXT, reply_markup=main_menu_keyboard())
        sent = await message.answer(ONBOARD_PROMPT, reply_markup=onboarding_intro_keyboard())
        await state.update_data(intro_msg_id=sent.message_id)  # #7: чтобы погасить при вводе еды
        return
    await message.answer(START_TEXT, reply_markup=main_menu_keyboard())


@router.callback_query(F.data == f"{ONBOARD_PREFIX}:setup")
async def on_onboard_setup(
    callback: CallbackQuery, app_user: User, users: UserRepo, state: FSMContext
) -> None:
    if app_user.id is not None:
        await users.set_onboarded(app_user.id)  # отмечаем сразу — визард можно не доводить (не зацикливать /start)
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
        await callback.message.answer("Ок, отменил. Меню — снизу 👇", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_menu_keyboard())


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


@router.message(Command("goal"))
async def on_goal(
    message: Message, command: CommandObject, app_user: User, users: UserRepo
) -> None:
    arg = (command.args or "").strip().replace(",", ".")
    try:
        kcal = float(arg)
    except ValueError:
        await message.answer("Формат: <code>/goal 1800</code> — дневная цель по калориям.")
        return

    assert app_user.id is not None
    goal = Goal(
        daily_kcal=kcal,
        protein_g=round(kcal * 0.30 / 4),
        fat_g=round(kcal * 0.25 / 9),
        carb_g=round(kcal * 0.45 / 4),
    )
    await users.set_goal(app_user.id, goal)
    await _send_goal_card(message, goal, f"🎯 Цель обновлена\n{format_goal(goal)}")


@router.message(Command("today"))
async def cmd_today(message: Message, app_user: User, assistant: Assistant) -> None:
    await _show_today(message, app_user, assistant)


@router.message(Command("week"))
async def cmd_week(message: Message, app_user: User, assistant: Assistant) -> None:
    await _show_week(message, app_user, assistant)


@router.message(Command("card"))
async def cmd_card(message: Message, app_user: User, assistant: Assistant) -> None:
    """Карточка дня картинкой — для шеринга (дата · бары · кольцо)."""
    rem, dv = await assistant.day_card(app_user)
    png = render_day_card(rem, dv.day)
    await message.answer_photo(BufferedInputFile(png, "card.png"), caption="📤 Карточка дня")


@router.message(Command("streak"))
async def cmd_streak(message: Message, app_user: User, assistant: Assistant) -> None:
    """Календарь-хитмап за 4 недели: дни в цели зелёным, перебор красным."""
    days, goal = await assistant.day_calories(app_user, 28)
    if not any(k > 0 for _, k in days):
        await message.answer("Пока пусто — запиши пару дней, и тут появится календарь 📅")
        return
    png = render_heatmap(days, goal.daily_kcal)
    await message.answer_photo(BufferedInputFile(png, "streak.png"), caption="📅 Дни в цели")


@router.message(Command("month"))
async def cmd_month(message: Message, app_user: User, assistant: Assistant) -> None:
    """Отчёт за 30 дней картинкой: среднее КБЖУ, дней в цели, Δ веса."""
    rep = await assistant.month_report(app_user)
    if rep.days_logged == 0:
        await message.answer("За последние 30 дней записей нет — поедим, потом покажу отчёт 📅")
        return
    png = render_month_card(rep)
    await message.answer_photo(BufferedInputFile(png, "month.png"), caption="📅 Отчёт за 30 дней")


@router.message(Command("history"))
async def cmd_history(message: Message, app_user: User, assistant: Assistant) -> None:
    await _show_history(message, app_user, assistant)


@router.message(Command("undo"))
async def cmd_undo(message: Message, app_user: User, assistant: Assistant) -> None:
    await _show_undo_picker(message, app_user, assistant)


@router.message(Command("products"))
async def cmd_products(message: Message, app_user: User, assistant: Assistant) -> None:
    products = await assistant.fixable_products(limit=10)
    if not products:
        await message.answer(
            "Пока всё точно — здесь появятся продукты, где я прикинул КБЖУ сам (≈), "
            "когда запишешь такое блюдо."
        )
        return
    await message.answer(
        "Продукты, где я прикинул КБЖУ сам (≈). Что поправить?",
        reply_markup=products_fix_keyboard(products),
    )


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
    await _send_goal_card(
        message, goal,
        f"🎯 Твоя дневная норма\n{format_goal(goal)}\n\n"
        "Пересчитать по профилю — ⚙️ Профиль (/profile); вручную — <code>/goal 1800</code>.",
    )


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
    message: Message, command: CommandObject, app_user: User, assistant: Assistant,
    session: AsyncSession, state: FSMContext,
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


@router.message(F.text == BTN_DEMO)
async def btn_demo(
    message: Message, bot: Bot, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    """ВРЕМЕННО: демо дизайна на Rich-таблицах (Bot API 10.1) — все разделы одним сообщением."""
    await state.clear()
    st = await assistant.today(app_user)
    week_days, goal = await assistant.day_calories(app_user, 7)
    rep = await assistant.month_report(app_user)
    dv = await assistant.day_view(app_user)
    products = await assistant.fixable_products(limit=6)
    md = build_demo_markdown(st, week_days, goal, rep, dv, products)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧪 Кнопка тут работает?", callback_data="demo_ping")]
        ]
    )
    try:
        await bot.send_rich_message(
            chat_id=message.chat.id,
            rich_message=InputRichMessage(markdown=md),
            reply_markup=kb,
        )
    except TelegramBadRequest as e:  # разметку не приняли — показываем ошибку и сырой markdown
        await message.answer(f"❌ Rich-сообщение отклонено:\n{escape(str(e), quote=False)}")
        await message.answer(f"<pre>{escape(md[:3000], quote=False)}</pre>")
        return

    # ГИБРИД: цифры — таблицами (выше), графика — нашими PNG альбомом (ниже).
    # Встроить картинки ВНУТРЬ rich-сообщения нельзя до Bot API 10.2 (у aiogram 3.29 — 10.1):
    # там media только по HTTP(S)-URL, а наши PNG рендерятся в память. ponytail: альбом рядом.
    album: list[InputMediaPhoto] = [
        InputMediaPhoto(
            media=BufferedInputFile(render_today(st.remaining), "today.png"),
            caption="📊 Сегодня — бары + кольцо Б/Ж/У",
        )
    ]
    if any(k > 0 for _, k in week_days):
        album.append(
            InputMediaPhoto(
                media=BufferedInputFile(render_weekly_chart(week_days, goal.daily_kcal), "w.png"),
                caption="📈 Неделя",
            )
        )
    month_days, _ = await assistant.day_calories(app_user, 28)
    if any(k > 0 for _, k in month_days):
        album.append(
            InputMediaPhoto(
                media=BufferedInputFile(render_heatmap(month_days, goal.daily_kcal), "s.png"),
                caption="📅 Дни в цели",
            )
        )
    trend = await assistant.weight_trend(app_user)
    if len(trend.logs) >= 2:
        album.append(
            InputMediaPhoto(
                media=BufferedInputFile(render_weight_chart(_weight_points(trend)), "kg.png"),
                caption="⚖️ Вес",
            )
        )
    # list инвариантен, а media ждёт union без Animation — шлём только фото, поэтому безопасно
    await bot.send_media_group(chat_id=message.chat.id, media=album)  # type: ignore[arg-type]


@router.message(MealStates.awaiting_bug, F.text)
async def on_bug_note(
    message: Message, app_user: User, assistant: Assistant, session: AsyncSession,
    state: FSMContext,
) -> None:
    await state.clear()
    await assistant.report_bug(app_user, message.text or "")
    await session.commit()
    await message.answer(_BUG_DONE)


@router.message(MealStates.awaiting_bug, F.photo)
async def on_bug_photo(
    message: Message, app_user: User, assistant: Assistant, session: AsyncSession,
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
    message: Message, bot: Bot, app_user: User, assistant: Assistant, session: AsyncSession,
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
    await _reply_message(message, result, state, raw=raw)


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
    await _reply_message(message, result, state, raw=raw)
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
    await _reply_message(message, result, state, raw=transcript)
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
            await _reply_message(message, final, state, raw="этикетка")
            return
        card = render_label_card(
            item.name, item.kcal_per_100g, item.protein_per_100g,
            item.fat_per_100g, item.carb_per_100g,
        )
        await message.answer_photo(BufferedInputFile(card, "label.png"))  # инфо-карточка КБЖУ/100г
        await state.set_state(MealStates.awaiting_label_grams)
        await state.update_data(draft=result.draft, item_index=result.item_index, raw="этикетка")
        await message.answer(
            format_result(result),
            reply_markup=label_grams_keyboard(item.unit_weight_g, item.serving_g),
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
    await _reply_message(message, result, state, raw="этикетка")


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
    item = draft.items[int(data.get("item_index", 0))]
    item.grams = float((callback.data or "label_g:0").split(":", 1)[1])
    item.unit = Unit.GRAM
    result = await assistant.continue_meal(app_user, draft, raw_input="этикетка")
    await session.commit()
    await state.clear()
    await _render_after_continue(callback.message, result, state, raw="этикетка")


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
    await _reply_message(message, result, state, raw=message.text or "")
    await _maybe_weight_chart(message, app_user, assistant, result)


@router.message(
    StateFilter(
        MealStates.awaiting_portion, MealStates.awaiting_variant, MealStates.awaiting_density,
        MealStates.awaiting_rename, MealStates.awaiting_label_grams, MealStates.awaiting_bug,
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

    item_index = int(data.get("item_index", 0))
    raw = str(data.get("raw", ""))
    hint = (callback.data or "portion:medium").split(":", 1)[1]
    draft.items[item_index].portion_hint = hint

    result = await assistant.continue_meal(app_user, draft, raw_input=raw)
    await session.commit()
    await _render_after_continue(callback.message, result, state, raw=raw)


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

    result = await assistant.resolve_clarification(
        app_user, draft, item_index, options[idx], raw_input=raw
    )
    await session.commit()
    await _render_after_continue(callback.message, result, state, raw=raw)
    await callback.answer()


@router.callback_query(F.data.startswith("meal_undo:"))
async def on_meal_undo(callback: CallbackQuery, app_user: User, assistant: Assistant) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    meal_id = int((callback.data or "meal_undo:0").split(":", 1)[1])
    result = await assistant.delete_meal(app_user, meal_id)
    if result.meal is None:
        await callback.answer("Уже удалено.")
        await _edit_card(callback.message, "🗑 Запись уже удалена.")  # карточка-фото → edit_caption
        return
    kcal = round(result.meal.totals.kcal)
    if (callback.message.text or "").startswith("Что отменить"):  # пикер /undo → перерисовать
        history = await assistant.today_meals(app_user)
        if history.meals:
            await _safe_edit(
                callback.message, "Что отменить? (по позициям)",
                undo_picker_keyboard(history.meals, history.timezone),
            )
        else:
            await _safe_edit(callback.message, "🗒 Больше нечего отменять — старое в 🗒 История.")
    else:  # контекстная отмена под карточкой приёма (она теперь фото → edit_caption)
        await _edit_card(callback.message, f"🗑 Отменил приём ({kcal} ккал).")
    await callback.answer()


@router.callback_query(F.data.startswith("item_del:"))
async def on_item_del(callback: CallbackQuery, app_user: User, assistant: Assistant) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    parts = (callback.data or "item_del:0:0").split(":")
    meal_id, item_id = int(parts[1]), int(parts[2])
    result = await assistant.remove_item(app_user, meal_id, item_id)
    if result is None:
        await _edit_card(callback.message, "🗑 Запись отменена (позиций не осталось).")
    elif callback.message.photo:  # карточка-фото: перерисовать бары + обновить подпись
        media = InputMediaPhoto(
            media=BufferedInputFile(render_dashboard(result.remaining), "meal.png"),
            caption=_cap(meal_photo_caption(result)),
        )
        kb = meal_actions_keyboard(result.meal)
        with suppress(TelegramBadRequest):
            await callback.message.edit_media(media, reply_markup=kb)
    else:  # путь после уточнения — карточка текстом
        await callback.message.edit_text(
            format_result(result), reply_markup=meal_actions_keyboard(result.meal)
        )
    await callback.answer()


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
    callback: CallbackQuery, app_user: User, assistant: Assistant, users: UserRepo,
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

    if data.get("onboarding"):  # первый запуск → передаём к записи еды + справке
        tail = (
            "\n\n🎉 Всё готово! Запиши первый приём — например «куриная грудка 250 г».\n"
            "📍 Пришли геолокацию через /timezone — чтобы «день» статистики шёл по твоему поясу.\n"
            "Полная инструкция — /help"
        )
    else:
        tail = "\n\nПоменять: <code>/goal 1800</code>"
    await _send_goal_card(callback.message, goal, f"✅ Дневная цель\n{format_goal(goal)}{tail}")
    await callback.answer()


@router.callback_query(F.data.startswith(f"{DAY_NAV_PREFIX}:"))
async def on_day_nav(callback: CallbackQuery, app_user: User, assistant: Assistant) -> None:
    """Вар. A: ← / → листание дней в просмотре истории."""
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    days_ago = max(0, int((callback.data or "day_nav:0").split(":", 1)[1]))
    rem, dv = await assistant.day_card(app_user, days_ago)
    photo = BufferedInputFile(render_day_card(rem, dv.day), "day.png")
    cap, kb = _cap(day_photo_caption(dv)), day_nav_keyboard(dv.days_ago)
    if callback.message.photo:
        media = InputMediaPhoto(media=photo, caption=cap)
        with suppress(TelegramBadRequest):
            await callback.message.edit_media(media, reply_markup=kb)
    else:  # старое ТЕКСТОВОЕ сообщение истории (до картинок) — шлём новую карточку
        await callback.message.answer_photo(photo, caption=cap, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "demo_ping")
async def on_demo_ping(callback: CallbackQuery) -> None:
    """ВРЕМЕННО: проверка, что inline-кнопки живут на rich-сообщении."""
    await callback.answer("✅ Кнопки на rich-сообщении работают", show_alert=True)


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
    await message.answer("⚙️ Настрою дневную цель по профилю.\nТвой пол?", reply_markup=sex_keyboard())


async def _show_today(message: Message, app_user: User, assistant: Assistant) -> None:
    stats = await assistant.today(app_user)
    if stats.meals_count == 0:
        await message.answer(format_result(stats))  # пусто → текстовый нудж
        return
    png = render_today(stats.remaining)  # бары + кольцо Б/Ж/У картинкой (ровно на Android)
    caption = today_photo_caption(stats)
    if len(caption) <= 1024:  # лимит подписи Telegram; длинный список — отдельным сообщением
        await message.answer_photo(BufferedInputFile(png, "today.png"), caption=caption)
    else:
        await message.answer_photo(BufferedInputFile(png, "today.png"), caption="📊 Сегодня")
        await message.answer(caption)


async def _show_week(message: Message, app_user: User, assistant: Assistant) -> None:
    days, goal = await assistant.day_calories(app_user, 7)
    if not any(k > 0 for _, k in days):  # неделя пустая → текстовый нудж
        await message.answer(format_result(await assistant.weekly(app_user)))
        return
    logged = sum(1 for _, k in days if k > 0)
    avg = round(sum(k for _, k in days) / logged)
    png = render_weekly_chart(days, goal.daily_kcal)
    await message.answer_photo(
        BufferedInputFile(png, "week.png"),
        caption=f"📈 Неделя · дней с записями {logged}/7 · в среднем {avg} ккал/день",
    )


async def _show_weight(message: Message, app_user: User, assistant: Assistant) -> None:
    trend = await assistant.weight_trend(app_user)
    if len(trend.logs) < 2:  # 0–1 точка — график не построить, отдаём текст
        await message.answer(format_result(trend))
        return
    await message.answer_photo(
        BufferedInputFile(render_weight_chart(_weight_points(trend)), "weight.png"),
        caption="⚖️ Тренд веса",
    )


async def _show_history(message: Message, app_user: User, assistant: Assistant) -> None:
    rem, dv = await assistant.day_card(app_user)  # день карточкой-картинкой, стрелки ← →
    png = render_day_card(rem, dv.day)
    await message.answer_photo(
        BufferedInputFile(png, "day.png"),
        caption=_cap(day_photo_caption(dv)), reply_markup=day_nav_keyboard(dv.days_ago),
    )


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


async def _send_meal_card(message: Message, result: MealLogged) -> None:
    """Фото-карточка приёма: бары картинкой + список в подписи + кнопки отмены (новое сообщение)."""
    photo = BufferedInputFile(render_dashboard(result.remaining), "meal.png")
    caption = meal_photo_caption(result)
    kb = meal_actions_keyboard(result.meal)
    if len(caption) <= 1024:  # лимит подписи Telegram
        await message.answer_photo(photo, caption=caption, reply_markup=kb)
    else:  # длинный приём — фото с краткой подписью + список отдельным сообщением
        await message.answer_photo(photo, caption="✅ Записал")
        await message.answer(caption, reply_markup=kb)


async def _reply_message(
    message: Message, result: Result, state: FSMContext, *, raw: str
) -> None:
    if isinstance(result, ClarificationNeeded):
        await state.set_state(MealStates.awaiting_portion)
        await state.update_data(draft=result.draft, item_index=result.item_index, raw=raw)
        options = result.draft.items[result.item_index].portion_options
        await message.answer(format_result(result), reply_markup=portion_keyboard(options))
    elif isinstance(result, VariantClarificationNeeded):
        await state.set_state(MealStates.awaiting_variant)
        sent = await message.answer(format_result(result), reply_markup=variant_keyboard(result.options))
        await state.update_data(
            draft=result.draft, item_index=result.item_index, raw=raw, q_msg_id=sent.message_id
        )
    elif isinstance(result, MealLogged):
        await _send_meal_card(message, result)
    else:
        await message.answer(format_result(result))


async def _render_after_continue(
    message: Message, result: Result, state: FSMContext, *, raw: str
) -> None:
    """После ответа на уточнение (порция/вариант): следующий вопрос или финал — правит сообщение."""
    if isinstance(result, ClarificationNeeded):
        await state.set_state(MealStates.awaiting_portion)
        await state.update_data(draft=result.draft, item_index=result.item_index, raw=raw)
        options = result.draft.items[result.item_index].portion_options
        await _safe_edit(message, format_result(result), portion_keyboard(options))
    elif isinstance(result, VariantClarificationNeeded):
        await state.set_state(MealStates.awaiting_variant)
        await state.update_data(
            draft=result.draft, item_index=result.item_index, raw=raw, q_msg_id=message.message_id
        )
        await _safe_edit(message, format_result(result), variant_keyboard(result.options))
    else:
        await state.clear()
        if isinstance(result, MealLogged):  # финал и после кнопки — фото-карточкой, как при тексте
            with suppress(TelegramBadRequest):
                await message.edit_reply_markup(reply_markup=None)  # снять кнопки сообщения-вопроса
            await _send_meal_card(message, result)
        else:
            await _safe_edit(message, format_result(result))


def _cap(text: str) -> str:
    """Подпись Telegram ≤1024 символов — длинную режем с многоточием."""
    return text if len(text) <= 1024 else text[:1020] + "…"


def _weight_points(trend: WeightTrend) -> list[tuple[datetime, float]]:
    return [(log.measured_at, log.weight_kg) for log in reversed(trend.logs)]  # старые→новые


async def _send_goal_card(message: Message, goal: Goal, caption: str) -> None:
    png = render_goal_card(goal)
    await message.answer_photo(BufferedInputFile(png, "goal.png"), caption=_cap(caption))


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


async def _edit_card(message: Message, text: str, markup: object = None) -> None:
    """Правка карточки приёма: фото → edit_caption, текст → edit_text (undo/remove-item)."""
    if message.photo:
        with suppress(TelegramBadRequest):
            await message.edit_caption(caption=text, reply_markup=markup)  # type: ignore[arg-type]
    else:
        await _safe_edit(message, text, markup)


async def _safe_edit(message: Message, text: str, markup: object = None) -> None:
    """edit_text, проглатывая 'message is not modified' (идентичная правка — не ошибка)."""
    try:
        await message.edit_text(text, reply_markup=markup)  # type: ignore[arg-type]
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            raise
