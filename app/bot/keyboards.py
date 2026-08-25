"""Клавиатуры: reply-меню + inline для порций, действий, отмены, правки продукта, профиля."""
from __future__ import annotations

from collections.abc import Sequence
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from app.application.entities import Meal, Product

PORTION_CALLBACK_PREFIX = "portion"
VARIANT_CALLBACK_PREFIX = "variant"
LABEL_G_PREFIX = "label_g"  # пресет веса (порция/пачка) после фото этикетки (FR-27)
DAY_NAV_PREFIX = "day_nav"  # навигация по дням в просмотре истории (вар. A)
DAY_DEL_PREFIX = "day_del"  # удалить позицию прямо с экрана «День»/«История» (день:meal:item)
GOAL_TARGET_PREFIX = "goal_tgt"  # сменить цель (cut/maintain/bulk) с экрана 🎯 Цель, без визарда
MEAL_UNDO_PREFIX = "meal_undo"
UNDO_ITEM_PREFIX = "undo_item"
ITEM_DEL_PREFIX = "item_del"
MEAL_REPEAT_PREFIX = "meal_repeat"
PROD_FIX_PREFIX = "prod_fix"
PROD_RENAME_PREFIX = "prod_rename"  # переименовать продукт в /products (FR-27 #6)
ONBOARD_PREFIX = "onb"
WIZ_CANCEL = "wiz_cancel"  # «✖️ Отмена» в любом визарде (профиль/вариант) — сброс FSM

# Тексты кнопок постоянного меню (они же приходят как обычные сообщения).
BTN_TODAY = "📊 Сегодня"
BTN_WEEK = "📈 Неделя"
BTN_HISTORY = "🗒 История"
BTN_WEIGHT = "⚖️ Вес"
BTN_GOAL = "🎯 Цель"
BTN_PROFILE = "⚙️ Профиль"
BTN_UNDO = "↩️ Отменить"
BTN_BUG = "🐛 Баг"

MENU_BUTTONS = (
    BTN_TODAY, BTN_WEEK, BTN_HISTORY, BTN_WEIGHT, BTN_GOAL, BTN_PROFILE, BTN_UNDO, BTN_BUG,
)


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    for text in MENU_BUTTONS:
        builder.button(text=text)
    builder.adjust(3, 3, 2)
    return builder.as_markup(resize_keyboard=True, is_persistent=True)


def onboarding_intro_keyboard() -> InlineKeyboardMarkup:
    """Первый запуск (FR-24): настроить профиль сейчас или отложить."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⚙️ Настроить (30 сек)", callback_data=f"{ONBOARD_PREFIX}:setup")
    builder.button(text="⏭ Позже", callback_data=f"{ONBOARD_PREFIX}:skip")
    builder.adjust(1)
    return builder.as_markup()


def location_request_keyboard() -> ReplyKeyboardMarkup:
    """Одноразовая клавиатура: запросить геолокацию для часового пояса (FR-23)."""
    builder = ReplyKeyboardBuilder()
    builder.button(text="📍 Отправить геолокацию", request_location=True)
    return builder.as_markup(resize_keyboard=True, one_time_keyboard=True)


def portion_keyboard(options: dict[str, float] | None = None) -> InlineKeyboardMarkup:
    """Кнопки размера. Если LLM дал реальные граммы пер-блюдо — показываем их (FR-21)."""
    builder = InlineKeyboardBuilder()
    for key, label in (("small", "Маленькая"), ("medium", "Средняя"), ("large", "Большая")):
        grams = options.get(key) if options else None
        text = f"{label} · {round(grams)} г" if grams else label
        builder.button(text=text, callback_data=f"{PORTION_CALLBACK_PREFIX}:{key}")
    builder.button(text="✖️ Отмена", callback_data=WIZ_CANCEL)
    builder.adjust(3, 1)
    return builder.as_markup()


def variant_keyboard(options: Sequence[str]) -> InlineKeyboardMarkup:
    """D-19: кнопки-варианты от LLM (callback = индекс в options). Свой вариант — впиши текстом."""
    builder = InlineKeyboardBuilder()
    # Кнопка на всю ширину и одна в ряд — влезает заметно больше дефолтных 22 символов,
    # которыми мы сами резали «ржаные сухарики без в…».
    for i, opt in enumerate(_distinct(options)):
        builder.button(text=_short(opt, 34), callback_data=f"{VARIANT_CALLBACK_PREFIX}:{i}")
    builder.button(text="✖️ Отмена", callback_data=WIZ_CANCEL)
    builder.adjust(1)
    return builder.as_markup()


def prod_edit_keyboard(product_id: int) -> InlineKeyboardMarkup:
    """Под карточкой правки продукта: переименовать (КБЖУ правится текстом-числами). FR-27 #6."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Переименовать", callback_data=f"{PROD_RENAME_PREFIX}:{product_id}")
    builder.button(text="✖️ Отмена", callback_data=WIZ_CANCEL)
    builder.adjust(1)
    return builder.as_markup()


def day_nav_keyboard(
    days_ago: int, meals: Sequence[Meal] = (), tz_name: str = "UTC"
) -> InlineKeyboardMarkup:
    """Навигация по дням (← пред всегда, → след если не сегодня) + удаление приёмов этого дня.

    Кнопка 🗑 на каждый приём — чтобы удалять любой прошлый приём рядом с текстом дня (в сам текст
    кнопку Telegram не встроить, поэтому клавиатурой под сообщением). days_ago в callback — чтобы
    после удаления перерисовать ТОТ ЖЕ день.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="← пред. день", callback_data=f"{DAY_NAV_PREFIX}:{days_ago + 1}")
    if days_ago > 0:
        builder.button(text="след. день →", callback_data=f"{DAY_NAV_PREFIX}:{days_ago - 1}")
    tz = ZoneInfo(tz_name)
    # 🗑 на КАЖДУЮ позицию (а не на запись): владелец логирует по одному продукту, и даже сдвоенную
    # запись (картофель+фарш из одного голосового) надо чистить по позиции. Время в подписи —
    # чтобы различать одинаковые продукты в разное время (молоко 09:11 vs молоко 13:37).
    items = [(m, it) for m in meals if m.id is not None for it in m.items if it.id is not None]
    for meal, it in items:
        when = meal.eaten_at.astimezone(tz).strftime("%H:%M")
        builder.button(
            text=f"🗑 {when} · {_short(it.name_snapshot, 24)}",
            callback_data=f"{DAY_DEL_PREFIX}:{days_ago}:{meal.id}:{it.id}",
        )
    nav = 2 if days_ago > 0 else 1  # ряд навигации, дальше каждое удаление на своей строке
    builder.adjust(nav, *([1] * len(items)))
    return builder.as_markup()


def label_grams_keyboard(
    package_g: float | None, serving_g: float | None = None
) -> InlineKeyboardMarkup:
    """FR-27: пресеты веса с этикетки — «1 порция» и/или «вся пачка», плюс отмена.

    Отмена тут обязательна (баг #31): это был единственный визард без неё, а без пресетов
    клавиатура вообще не приходила — вопрос висел, выйти можно было только текстом, и новое
    фото при этом не принималось (`_busy_with_flow` отбивает его, пока висит вопрос).
    """
    builder = InlineKeyboardBuilder()
    shown: set[int] = set()
    if serving_g:
        g = round(serving_g)
        builder.button(text=f"1 порция · {g} г", callback_data=f"{LABEL_G_PREFIX}:{g}")
        shown.add(g)
    if package_g and round(package_g) not in shown:  # дедуп: порция==пачка → одна кнопка
        g = round(package_g)
        builder.button(text=f"Вся пачка · {g} г", callback_data=f"{LABEL_G_PREFIX}:{g}")
        shown.add(g)
    builder.button(text="✖️ Отмена", callback_data=WIZ_CANCEL)
    builder.adjust(1)
    return builder.as_markup()


def meal_actions_keyboard(meal: Meal) -> InlineKeyboardMarkup:
    """Под свежей записью: удалить отдельную позицию (если их >1) и отменить всё.

    Это контекстная отмена «без вопроса» — кнопки знают конкретную запись.
    """
    builder = InlineKeyboardBuilder()
    # Первой — созидательная кнопка: выпил вторую бутылку, съел вторую порцию. Копирует вес и
    # КБЖУ один-в-один, поэтому в ИИ не идёт и повтор не «переоценивается» заново.
    builder.button(text="🔁 Повторить", callback_data=f"{MEAL_REPEAT_PREFIX}:{meal.id}")
    if len(meal.items) > 1:
        for item in meal.items:
            if item.id is not None:
                builder.button(
                    text=f"🗑 {_short(item.name_snapshot)}",
                    callback_data=f"{ITEM_DEL_PREFIX}:{meal.id}:{item.id}",
                )
    builder.button(text="↩️ Отменить всё", callback_data=f"{MEAL_UNDO_PREFIX}:{meal.id}")
    builder.adjust(1)
    return builder.as_markup()


def undo_picker_keyboard(meals: Sequence[Meal], tz_name: str) -> InlineKeyboardMarkup:
    """Меню «Отменить» (FR-12/D-13): для каждого приёма — «весь приём» + позиции (если их >1).

    Единый смысл с контекстной отменой под свежей записью: можно снять и приём целиком, и позицию.
    """
    tz = ZoneInfo(tz_name)
    builder = InlineKeyboardBuilder()
    for meal in meals:
        when = meal.eaten_at.astimezone(tz).strftime("%H:%M")
        names = _short(", ".join(i.name_snapshot for i in meal.items), 24)
        builder.button(
            text=f"↩️ {when} · {names} · {meal.totals.kcal:g}",
            callback_data=f"{MEAL_UNDO_PREFIX}:{meal.id}",
        )
        if len(meal.items) > 1:  # детализация по позициям только когда их несколько
            for item in meal.items:
                if item.id is not None:
                    builder.button(
                        text=f"   🗑 {_short(item.name_snapshot, 20)} · {item.macros.kcal:g}",
                        callback_data=f"{UNDO_ITEM_PREFIX}:{meal.id}:{item.id}",
                    )
    builder.adjust(1)
    return builder.as_markup()


def products_fix_keyboard(products: Sequence[Product]) -> InlineKeyboardMarkup:
    """Список продуктов с AI-оценкой — выбрать, какому исправить КБЖУ."""
    builder = InlineKeyboardBuilder()
    for product in products:
        if product.id is not None:
            builder.button(
                text=f"✏️ {_short(product.display_name)} ({product.density.kcal_per_100g:g} ккал)",
                callback_data=f"{PROD_FIX_PREFIX}:{product.id}",
            )
    builder.adjust(1)
    return builder.as_markup()


def sex_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="♂️ Мужской", callback_data="prof_sex:male")
    builder.button(text="♀️ Женский", callback_data="prof_sex:female")
    builder.button(text="✖️ Отмена", callback_data=WIZ_CANCEL)
    builder.adjust(2, 1)
    return builder.as_markup()


def activity_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🛋 Низкая", callback_data="prof_act:low")
    builder.button(text="🚶 Средняя", callback_data="prof_act:medium")
    builder.button(text="🏃 Высокая", callback_data="prof_act:high")
    builder.button(text="✖️ Отмена", callback_data=WIZ_CANCEL)
    builder.adjust(3, 1)
    return builder.as_markup()


def goal_target_keyboard(current: str) -> InlineKeyboardMarkup:
    """Смена цели прямо с экрана 🎯 Цель — без прогона всего визарда профиля.

    Менять надо ОДНО поле, а визард заново спрашивает пол/вес/рост/возраст. Текущая цель помечена
    ✓ — иначе непонятно, на чём стоишь (в заголовке экрана она есть, но кнопки читают отдельно).
    """
    builder = InlineKeyboardBuilder()
    options = (("cut", "🔻 Похудение"), ("maintain", "⚖️ Поддержание"), ("bulk", "🔺 Набор"))
    for key, label in options:
        mark = "✓ " if key == current else ""
        builder.button(text=f"{mark}{label}", callback_data=f"{GOAL_TARGET_PREFIX}:{key}")
    builder.adjust(3)
    return builder.as_markup()


def goal_type_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔻 Похудение", callback_data="prof_goal:cut")
    builder.button(text="⚖️ Поддержание", callback_data="prof_goal:maintain")
    builder.button(text="🔺 Набор", callback_data="prof_goal:bulk")
    builder.button(text="✖️ Отмена", callback_data=WIZ_CANCEL)
    builder.adjust(1)
    return builder.as_markup()


def _short(text: str, limit: int = 22) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _distinct(options: Sequence[str]) -> list[str]:
    """Срезать общее начало вариантов: «ржаные сухарики со вкусом бекона» → «со вкусом бекона».

    По D-19 варианты приходят полными именами (кнопка целиком заменяет имя продукта), поэтому
    отличаются они ХВОСТОМ — ровно тем, что и обрезается. Продукт уже назван в вопросе над
    кнопками, второй раз в каждой кнопке он не нужен.

    Режем по словам, а не по символам: «ржаные сухарики с сы…» читается хуже, чем «с сыром».
    Хотя бы одно слово у варианта остаётся всегда — иначе кнопка станет пустой.
    """
    words = [o.split() for o in options]
    if len(words) < 2:
        return [" ".join(w) for w in words]
    common = 0
    while (
        all(len(w) > common + 1 for w in words)
        and len({w[common].casefold() for w in words}) == 1
    ):
        common += 1
    return [" ".join(w[common:]) for w in words]
