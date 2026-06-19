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
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.assistant import Assistant
from app.application.dto import Unit
from app.application.entities import User
from app.application.ports import UserRepo
from app.application.results import (
    ClarificationNeeded,
    MealLogged,
    Result,
    VariantClarificationNeeded,
)
from app.bot.formatters import format_goal, format_result
from app.bot.keyboards import (
    BTN_GOAL,
    BTN_HISTORY,
    BTN_PROFILE,
    BTN_TODAY,
    BTN_UNDO,
    BTN_WEEK,
    BTN_WEIGHT,
    activity_keyboard,
    goal_type_keyboard,
    location_request_keyboard,
    main_menu_keyboard,
    meal_actions_keyboard,
    portion_keyboard,
    products_fix_keyboard,
    sex_keyboard,
    undo_picker_keyboard,
    variant_keyboard,
)
from app.bot.states import MealStates, ProfileStates
from app.domain.goals import Goal, goal_from_profile
from app.domain.timeframe import tz_from_longitude
from app.domain.value_objects import Density

router = Router()
logger = logging.getLogger(__name__)

START_TEXT = (
    "👋 Привет! Я считаю КБЖУ по твоим сообщениям.\n\n"
    "Просто напиши или наговори, что съел:\n"
    "• «курица 250 грамм»\n"
    "• «два банана»\n"
    "• «фо бо средняя порция»\n\n"
    "Вес тела: «вес 86.8».\n"
    "Кнопки меню — снизу 👇"
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
        with suppress(Exception):
            await target.answer("⚠️ Что-то пошло не так. Попробуй ещё раз через пару секунд.")
    return True


# --- команды ------------------------------------------------------------------


@router.message(CommandStart())
async def on_start(
    message: Message, app_user: User, users: UserRepo, state: FSMContext
) -> None:
    await state.clear()
    text = START_TEXT
    if app_user.id is not None and await users.get_goal(app_user.id) is None:
        text += (
            "\n\n⚙️ Сначала задай цель: жми «⚙️ Профиль» или <code>/goal 1800</code> — "
            "иначе остаток считаю от 2000 ккал по умолчанию."
        )
    await message.answer(text, reply_markup=main_menu_keyboard())


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    await message.answer(START_TEXT, reply_markup=main_menu_keyboard())


@router.message(Command("timezone"))
async def cmd_timezone(message: Message, app_user: User) -> None:
    await message.answer(
        f"🕒 Часовой пояс: <b>{escape(app_user.timezone, quote=False)}</b>\n"
        "Чтобы уточнить «день» для статистики — пришли геолокацию кнопкой ниже 👇",
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
    await message.answer(f"🎯 Цель обновлена: {kcal:g} ккал/день.")


@router.message(Command("today"))
async def cmd_today(message: Message, app_user: User, assistant: Assistant) -> None:
    await _show_today(message, app_user, assistant)


@router.message(Command("week"))
async def cmd_week(message: Message, app_user: User, assistant: Assistant) -> None:
    await _show_week(message, app_user, assistant)


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
        await message.answer("Пока нечего править — нет продуктов с AI-оценкой.")
        return
    await message.answer(
        "Продукты с AI-оценкой КБЖУ. Что поправить?",
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
    await message.answer(format_result(await assistant.weight_trend(app_user)))


@router.message(F.text == BTN_GOAL)
async def btn_goal(
    message: Message, app_user: User, assistant: Assistant, state: FSMContext
) -> None:
    await state.clear()
    goal = await assistant.current_goal(app_user)
    await message.answer(
        "🎯 Цель на день:\n"
        f"{format_goal(goal)}\n\n"
        "Изменить: <code>/goal 1800</code> или ⚙️ Профиль (авто)."
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
    if draft is None:  # черновик потерян — обрабатываем как обычный ввод
        await state.clear()
        result = await assistant.process(app_user, message.text or "", input_type="text")
        await session.commit()
        await _reply_message(message, result, state, raw=message.text or "")
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


@router.message(ProfileStates.body, F.text)
async def on_profile_body(message: Message, state: FSMContext) -> None:
    nums = _parse_floats(message.text or "")
    if len(nums) != 3:
        await message.answer(
            "Нужно 3 числа через пробел: <b>вес рост возраст</b>. Напр.: <code>86 180 30</code>"
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
    await state.clear()
    if message.voice is None:
        return

    await _send_typing(message)
    buffer = await bot.download(message.voice)
    audio = buffer.read() if buffer is not None else b""
    transcript = await assistant.transcribe(audio, filename="voice.ogg")

    if not transcript:
        await message.answer(
            "🎤 Не смог распознать голос. Для голосовых нужен OpenAI-ключ — либо напиши текстом."
        )
        return

    await message.answer(f"🎤 Распознал: «{escape(transcript, quote=False)}»")
    await _send_typing(message)
    result = await assistant.process(app_user, transcript, input_type="voice")
    await session.commit()
    await _reply_message(message, result, state, raw=transcript)


@router.message(F.text)
async def on_text(
    message: Message,
    app_user: User,
    assistant: Assistant,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    await state.clear()
    await _send_typing(message)
    result = await assistant.process(app_user, message.text or "", input_type="text")
    await session.commit()  # фиксируем запись ДО ответа — «✅ записал» не врёт
    await _reply_message(message, result, state, raw=message.text or "")


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
    product_id = int((callback.data or "variant:0").split(":", 1)[1])
    product = await assistant.get_product(product_id)
    if product is None:
        await callback.answer("Вариант не найден — отправь заново.")
        return

    draft.items[item_index].canonical_key = product.canonical_key
    draft.items[item_index].variant_locked = True  # выбор сделан явно → детект не переспрашивает
    result = await assistant.continue_meal(app_user, draft, raw_input=raw)
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
        with suppress(Exception):
            await callback.message.edit_text("🗑 Запись уже удалена.")
        return
    await callback.message.edit_text("🗑 Запись отменена.")
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
        await callback.message.edit_text("🗑 Запись отменена (позиций не осталось).")
    else:
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
        f"Напр.: <code>165 31 3.6 0</code>"
    )
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

    await callback.message.answer(
        "✅ Готово! Дневная цель:\n"
        f"{format_goal(goal)}\n\n"
        "Поменять: <code>/goal 1800</code>"
    )
    await callback.answer()


# --- helpers ------------------------------------------------------------------


async def _start_profile(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ProfileStates.sex)
    await message.answer("⚙️ Настрою дневную цель по профилю.\nТвой пол?", reply_markup=sex_keyboard())


async def _show_today(message: Message, app_user: User, assistant: Assistant) -> None:
    await message.answer(format_result(await assistant.today(app_user)))


async def _show_week(message: Message, app_user: User, assistant: Assistant) -> None:
    await message.answer(format_result(await assistant.weekly(app_user)))


async def _show_history(message: Message, app_user: User, assistant: Assistant) -> None:
    await message.answer(format_result(await assistant.history(app_user)))


async def _show_undo_picker(message: Message, app_user: User, assistant: Assistant) -> None:
    history = await assistant.history(app_user, limit=5)
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


async def _reply_message(
    message: Message, result: Result, state: FSMContext, *, raw: str
) -> None:
    if isinstance(result, ClarificationNeeded):
        await state.set_state(MealStates.awaiting_portion)
        await state.update_data(draft=result.draft, item_index=result.item_index, raw=raw)
        await message.answer(format_result(result), reply_markup=portion_keyboard())
    elif isinstance(result, VariantClarificationNeeded):
        await state.set_state(MealStates.awaiting_variant)
        await state.update_data(draft=result.draft, item_index=result.item_index, raw=raw)
        await message.answer(format_result(result), reply_markup=variant_keyboard(result.options))
    elif isinstance(result, MealLogged):
        await message.answer(format_result(result), reply_markup=meal_actions_keyboard(result.meal))
    else:
        await message.answer(format_result(result))


async def _render_after_continue(
    message: Message, result: Result, state: FSMContext, *, raw: str
) -> None:
    """После ответа на уточнение (порция/вариант): следующий вопрос или финал — правит сообщение."""
    if isinstance(result, ClarificationNeeded):
        await state.set_state(MealStates.awaiting_portion)
        await state.update_data(draft=result.draft, item_index=result.item_index, raw=raw)
        await _safe_edit(message, format_result(result), portion_keyboard())
    elif isinstance(result, VariantClarificationNeeded):
        await state.set_state(MealStates.awaiting_variant)
        await state.update_data(draft=result.draft, item_index=result.item_index, raw=raw)
        await _safe_edit(message, format_result(result), variant_keyboard(result.options))
    else:
        await state.clear()
        markup = meal_actions_keyboard(result.meal) if isinstance(result, MealLogged) else None
        await _safe_edit(message, format_result(result), markup)


async def _safe_edit(message: Message, text: str, markup: object = None) -> None:
    """edit_text, проглатывая 'message is not modified' (идентичная правка — не ошибка)."""
    try:
        await message.edit_text(text, reply_markup=markup)  # type: ignore[arg-type]
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            raise
