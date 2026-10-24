"""Клавиатуры: reply-меню + inline для порций, действий, отмены, правки продукта, профиля."""
from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from datetime import date
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from app.application.entities import Meal, MealItem, Product
from app.domain.plausibility import MAX_PORTION_G, MIN_PORTION_G

PORTION_CALLBACK_PREFIX = "portion"
VARIANT_CALLBACK_PREFIX = "variant"
LABEL_G_PREFIX = "label_g"  # пресет веса (порция/пачка) после фото этикетки (FR-27)
DAY_NAV_PREFIX = "day_nav"  # навигация по дням в просмотре истории (вар. A)
DAY_DEL_PREFIX = "day_del"  # удалить позицию прямо с экрана «День»/«История» (день:meal:item)
DAY_REP_PREFIX = "day_rep"  # с прошлого дня в сегодня: day_rep:d<ГГГГММДД>:<meal>[:<item>] (FR-42)
DAY_REPEAT_MODE = "r"  # day_nav:<дней назад>:r — тот же день в режиме повтора (FR-42)
GOAL_TARGET_PREFIX = "goal_tgt"  # сменить цель (cut/maintain/bulk) с экрана 🎯 Цель, без визарда
MEAL_UNDO_PREFIX = "meal_undo"
UNDO_ITEM_PREFIX = "undo_item"
ITEM_DEL_PREFIX = "item_del"
MEAL_REPEAT_PREFIX = "meal_repeat"
PROD_FIX_PREFIX = "prod_fix"
PROD_RENAME_PREFIX = "prod_rename"  # переименовать продукт в /products (FR-27 #6)
DISH_SAVE_PREFIX = "dish_save"  # запомнить запись как блюдо своей сборки (FR-39)
ONBOARD_PREFIX = "onb"
PHOTO_PREFIX = "ph"  # карточка фото блюда: ph:fat:<id> / ph:dr:<id> / ph:ok (FR-34)
SCAN_PREFIX = "scan"  # карточка бланка InBody: scan:save / scan:confirm / scan:apply (FR-36)
WIZ_CANCEL = "wiz_cancel"  # «✖️ Отмена» в любом визарде (профиль/вариант) — сброс FSM
RESTORE_PREFIX = "rst"  # «⏪ Вернуть» после удаления: rst:<токен>:<c|p|d<ГГГГММДД>> (FR-41)
ITEM_WEIGHT_PREFIX = "iw"  # «✏️ Вес»: iw:<meal>[:<item>[:<граммы>|:own]] · iw:<meal>:back (FR-43)
FREQ_PREFIX = "fq"  # «⚡ Частое»: fq:p:<product_id>:<граммы> / fq:s:<хэш имени набора> (FR-40)
# FR-40: потолки раздельные — наборы не вытесняют продукты. Аппка показывает ВСЕ наборы и топ-10
# частого; бот берёт тот же топ-10 и только закреплённое из него, не глубже.
FREQ_SETS_MAX = 3
FREQ_PRODUCTS_MAX = 8
FREQ_TOP = 10  # тот же топ, что у чипов аппки (`/api/frequent?limit=10`)
FREQ_NAME_MAX = 24  # режем ИМЯ, хвост «· 180 г» / «· 420 ккал» остаётся целым (320 px)

# Тексты кнопок постоянного меню (они же приходят как обычные сообщения).
BTN_TODAY = "📊 Сегодня"
BTN_WEEK = "📈 Неделя"
BTN_HISTORY = "🗒 История"
BTN_WEIGHT = "⚖️ Вес"
BTN_GOAL = "🎯 Цель"
BTN_PROFILE = "⚙️ Профиль"
BTN_UNDO = "↩️ Отменить"
BTN_BUG = "🐛 Баг"
BTN_FREQUENT = "⚡ Частое"

# FR-40: «Частое» — в третьем ряду, перед «Отменить»: первые два ряда не сдвигаются, привычные
# кнопки остаются под пальцем, а ряд записи (частое · отменить · баг) собирается рядом.
MENU_BUTTONS = (
    BTN_TODAY, BTN_WEEK, BTN_HISTORY, BTN_WEIGHT, BTN_GOAL, BTN_PROFILE,
    BTN_FREQUENT, BTN_UNDO, BTN_BUG,
)


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    for text in MENU_BUTTONS:
        builder.button(text=text)
    builder.adjust(3, 3, 3)
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


def restore_button(token: str, where: str, what: str) -> InlineKeyboardButton:
    """FR-41: «⏪ Вернуть: <что>» — что именно вернётся, видно до тапа (тост с именем уже исчез).
    where — куда перерисовать после возврата: c карточка, p пикер, d<ГГГГММДД> день — датой, а не
    «дней назад»: после полуночи перерисуется тот же день, уже как «вчера»."""
    return InlineKeyboardButton(
        text=f"⏪ Вернуть: {_short(what, 24)}", callback_data=f"{RESTORE_PREFIX}:{token}:{where}"
    )


def day_nav_keyboard(
    days_ago: int, meals: Sequence[Meal] = (), tz_name: str = "UTC",
    restore: InlineKeyboardButton | None = None, repeat: bool = False, day: date | None = None,
) -> InlineKeyboardMarkup:
    """Навигация по дням (← пред всегда, → след если не сегодня) + строки позиций этого дня.

    Кнопка на каждую позицию — чтобы править прошлое рядом с текстом дня (в сам текст кнопку
    Telegram не встроить, поэтому клавиатурой под сообщением). Строки — 🗑 (удалить) или, в режиме
    повтора прошлого дня, 🔁 (записать в сегодня, FR-42): режим разводит две противоположные кнопки
    по разным экранам, промах по соседней строке не пишет в чужой день (D-MA-24). Режим — только с
    датой дня `day`: 🔁 несёт её, а не «дней назад», чтобы после полуночи перерисовать ТОТ день.
    «⏪ Вернуть» (FR-41) — только в режиме 🗑: она про удаление.
    """
    repeat = repeat and days_ago > 0 and day is not None  # сегодня повтор задвоил бы
    mode = f":{DAY_REPEAT_MODE}" if repeat else ""
    builder = InlineKeyboardBuilder()
    restore = None if repeat else restore
    if restore is not None:  # FR-41: только что удалённое — первым рядом, над навигацией
        builder.add(restore)
    builder.button(text="← пред. день", callback_data=f"{DAY_NAV_PREFIX}:{days_ago + 1}{mode}")
    if days_ago > 0:
        builder.button(text="след. день →", callback_data=f"{DAY_NAV_PREFIX}:{days_ago - 1}{mode}")
    tz = ZoneInfo(tz_name)
    real = [m for m in meals if m.id is not None]
    rows: list[int] = []
    if days_ago > 0 and day is not None and real:  # переключатель — только на прошлом дне
        flip = "" if repeat else f":{DAY_REPEAT_MODE}"  # «Готово» — назад к 🗑
        builder.button(
            text="✅ Готово" if repeat else "🔁 Повторить в сегодня…",
            callback_data=f"{DAY_NAV_PREFIX}:{days_ago}{flip}",
        )
        rows.append(1)
    for meal in real:
        when = meal.eaten_at.astimezone(tz).strftime("%H:%M")
        items = [it for it in meal.items if it.id is not None]
        whole = repeat and len(items) > 1
        if whole:  # приём целиком — как «Повторить приём сегодня» аппки; позиции под ним — «↳»
            builder.button(
                text=f"🔁 {when} · весь приём · {round(meal.totals.kcal)} ккал",
                callback_data=f"{DAY_REP_PREFIX}:d{day:%Y%m%d}:{meal.id}",
            )
            rows.append(1)
        for it in items:
            if repeat:
                # Число — то, что запишется: граммы снапшота (без веса — ккал), режется имя.
                tail = f"{round(it.grams)} г" if it.grams else f"{round(it.macros.kcal)} ккал"
                head = "↳ 🔁" if whole else f"🔁 {when} ·"
                text = f"{head} {_short(it.name_snapshot, 24)} · {tail}"
                data = f"{DAY_REP_PREFIX}:d{day:%Y%m%d}:{meal.id}:{it.id}"
            else:
                # 🗑 на КАЖДУЮ позицию (а не на запись): владелец логирует по одному продукту, и
                # даже сдвоенную запись (картофель+фарш из одного голосового) чистят по позиции.
                # Время в подписи — различать одинаковые продукты (молоко 09:11 vs молоко 13:37).
                text = f"🗑 {when} · {_short(it.name_snapshot, 24)}"
                data = f"{DAY_DEL_PREFIX}:{days_ago}:{meal.id}:{it.id}"
            builder.button(text=text, callback_data=data)
            rows.append(1)
    nav = 2 if days_ago > 0 else 1  # ряд навигации, дальше каждая строка на своём ряду
    builder.adjust(*([1] if restore else []), nav, *rows)
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


def set_key(name: str) -> str:
    """Короткий id набора для callback (FR-40). Имя целиком в 64 байта не влезает (кириллица —
    два байта на букву), а id приёма меняется с каждым повтором. 10 hex-знаков: коллизия среди
    своих десятка наборов невероятна, а и случись — сверка идёт по наборам самого человека."""
    return hashlib.blake2s(name.encode(), digest_size=5).hexdigest()


def _freq_labels(names: Sequence[str]) -> list[str]:
    """Имена под кнопки «⚡ Частое»: ≤ FREQ_NAME_MAX, с «…».

    Два длинных имени с общим началом после обрезки совпали бы («куриное филе гриль на у…»
    дважды). Такой паре отдаём то, чем они отличаются (`_distinct`): первое слово + хвост.
    """
    out = [_short(n, FREQ_NAME_MAX) for n in names]
    for label in {x for x in out if out.count(x) > 1}:
        idx = [i for i, x in enumerate(out) if x == label]
        group = [names[i] for i in idx]
        if len({g.casefold() for g in group}) == 1:
            continue  # имена и правда одинаковые — различать нечем
        for i, tail in zip(idx, _distinct(group), strict=True):
            out[i] = _short(f"{names[i].split()[0]} … {tail}", FREQ_NAME_MAX)
    return out


def frequent_keyboard(
    sets: Sequence[Meal], frequent: Sequence[tuple[int | None, str, float, int]]
) -> InlineKeyboardMarkup | None:
    """FR-40: наборы первыми (≤ FREQ_SETS_MAX), дальше частые продукты (≤ FREQ_PRODUCTS_MAX).

    По одной кнопке в ряд: на 320 px две в ряд резали бы как раз число. Продукт без product_id
    не показываем: записать его можно только моделью, а это уже не «один тап». Граммы — в
    callback и после округления: записано будет ровно то, что на кнопке, и «0 г» не бывает.
    None — показывать нечего; пустую клавиатуру не шлём.
    """
    seen: set[str] = set()
    named: list[tuple[str, float]] = []
    for m in sets:  # свежие первыми (Assistant.sets) — из двух «Курица»/«курица» берём свежий
        name = m.template_name or ""
        if name and name.casefold() not in seen and len(named) < FREQ_SETS_MAX:
            seen.add(name.casefold())
            named.append((name, m.totals.kcal))
    products = [
        (pid, name, g) for pid, name, g in
        ((pid, name, round(grams, 1)) for pid, name, grams, _ in frequent)
        if pid and 0 < g <= 5000
    ][:FREQ_PRODUCTS_MAX]
    if not named and not products:
        return None
    builder = InlineKeyboardBuilder()
    for (name, kcal), label in zip(named, _freq_labels([n for n, _ in named]), strict=True):
        builder.button(
            text=f"📌 {label} · {round(kcal)} ккал",
            callback_data=f"{FREQ_PREFIX}:s:{set_key(name)}",
        )
    for (pid, _, grams), label in zip(
        products, _freq_labels([n for _, n, _ in products]), strict=True
    ):
        builder.button(
            text=f"{label} · {grams:g} г", callback_data=f"{FREQ_PREFIX}:p:{pid}:{grams:g}"
        )
    builder.adjust(1)
    return builder.as_markup()


def meal_actions_keyboard(
    meal: Meal, restore: InlineKeyboardButton | None = None
) -> InlineKeyboardMarkup:
    """Под свежей записью: удалить отдельную позицию (если их >1) и отменить всё.

    Это контекстная отмена «без вопроса» — кнопки знают конкретную запись. restore — «⏪ Вернуть»
    только что убранной позиции (FR-41), первой: сразу после удаления нужна именно она.
    """
    builder = InlineKeyboardBuilder()
    if restore is not None:
        builder.add(restore)
    # Первой — созидательная кнопка: выпил вторую бутылку, съел вторую порцию. Копирует вес и
    # КБЖУ один-в-один, поэтому в ИИ не идёт и повтор не «переоценивается» заново.
    builder.button(text="🔁 Повторить", callback_data=f"{MEAL_REPEAT_PREFIX}:{meal.id}")
    # FR-43: вес тапом — как чипы шторки аппки. Без позиций с весом кнопки нет вовсе: у «своих
    # цифр» граммы не выбираются (в аппке у такой записи чипов тоже нет).
    if weighable(meal):
        builder.button(text="✏️ Вес", callback_data=f"{ITEM_WEIGHT_PREFIX}:{meal.id}")
    # FR-39.2: несколько взвешенных позиций → набор, чтобы завтра не
    # вводить состав заново. Одна позиция — уже продукт, делать из неё блюдо нечего; позиция
    # без веса не даёт плотности, поэтому кнопку не показываем вовсе, а не ругаемся после тапа.
    if len(meal.items) > 1 and all(item.grams for item in meal.items):
        builder.button(
            text="📌 Сохранить набор", callback_data=f"{DISH_SAVE_PREFIX}:{meal.id}"
        )
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


def weighable(meal: Meal) -> list[MealItem]:
    """Позиции, вес которых можно поправить (FR-43): сохранённые и с граммами. Стейтед без веса
    масштабировать не от чего — `adjust_item` его и не возьмёт."""
    return [i for i in meal.items if i.id is not None and i.grams and i.grams > 0]


def half_up(x: float) -> int:
    """Округление как `Math.round` аппки: половина вверх. `round(66.5)` в Python даёт 66."""
    return math.floor(x + 0.5)


def weight_presets(grams: float) -> list[tuple[str, int]]:
    """FR-43: ½ · ×1,5 · ×2 — те же числа, что у чипов шторки аппки (§6.27): от веса, округлённого
    до грамма. Совпавшее с текущим и вне 1…2000 г (INV-11.2, use-case всё равно зажмёт) — прочь."""
    base = half_up(grams)
    out: list[tuple[str, int]] = []
    for label, x in (("½", half_up(base / 2)), ("×1,5", half_up(base * 1.5)), ("×2", base * 2)):
        if MIN_PORTION_G <= x <= MAX_PORTION_G and x != base and all(x != v for _, v in out):
            out.append((label, x))
    return out


def item_pick_keyboard(meal: Meal) -> InlineKeyboardMarkup:
    """FR-43: какой позиции править вес — по строке на позицию с весом, «← Назад» к карточке."""
    builder = InlineKeyboardBuilder()
    for it in weighable(meal):
        builder.button(
            text=f"✏️ {_short(it.name_snapshot, 24)} · {half_up(it.grams)} г",
            callback_data=f"{ITEM_WEIGHT_PREFIX}:{meal.id}:{it.id}",
        )
    builder.button(text="← Назад", callback_data=f"{ITEM_WEIGHT_PREFIX}:{meal.id}:back")
    builder.adjust(1)
    return builder.as_markup()


def item_weight_keyboard(meal: Meal, item: MealItem) -> InlineKeyboardMarkup:
    """FR-43: пресеты веса одной позиции. В callback — граммы, которые запишутся (абсолютные, не
    множитель): двойной тап пишет то же число, а не половину половины. По две в ряд: три на
    320 px режут «×1,5 · 1125 г». Назад называет позицию — экран про неё и без всплывашки — и
    ведёт к выбору позиции, если их с весом несколько, иначе к карточке."""
    builder = InlineKeyboardBuilder()
    head = f"{ITEM_WEIGHT_PREFIX}:{meal.id}:{item.id}"
    presets = weight_presets(item.grams)
    for label, grams in presets:
        # @основа: вес поменяли — кнопка отказывает, даже если число совпало с новым пресетом.
        builder.button(
            text=f"{label} · {grams} г", callback_data=f"{head}:{grams}@{item.grams:g}"
        )
    builder.button(text="✍️ Своё", callback_data=f"{head}:own")
    back = "" if len(weighable(meal)) > 1 else ":back"
    builder.button(
        text=f"← {_short(item.name_snapshot, 24)} · {half_up(item.grams)} г",
        callback_data=f"{ITEM_WEIGHT_PREFIX}:{meal.id}{back}",
    )
    builder.adjust(*([2] * ((len(presets) + 2) // 2)), 1)
    return builder.as_markup()


def undo_picker_keyboard(
    meals: Sequence[Meal], tz_name: str, restore: InlineKeyboardButton | None = None
) -> InlineKeyboardMarkup:
    """Меню «Отменить» (FR-12/D-13): для каждого приёма — «весь приём» + позиции (если их >1).

    Единый смысл с контекстной отменой под свежей записью: можно снять и приём целиком, и позицию.
    restore — «⏪ Вернуть» только что отменённого (FR-41), первой строкой.
    """
    tz = ZoneInfo(tz_name)
    builder = InlineKeyboardBuilder()
    if restore is not None:
        builder.add(restore)
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


def photo_keyboard(components: Sequence[object], off: Sequence[int]) -> InlineKeyboardMarkup:
    """FR-34, D-MA-20: на каждую позицию «✖ убрать» (убранная — «↩ вернуть»), затем
    «Записать»/«Отмена». Вопросов про масло нет: его оценивает модель, оно — строкой в списке."""
    builder = InlineKeyboardBuilder()
    for i, c in enumerate(components):
        mark = "↩ " if i in off else "✖ "
        builder.button(text=mark + _short(getattr(c, "name", "")),
                       callback_data=f"{PHOTO_PREFIX}:off:{i}")
    builder.button(text="✅ Записать", callback_data=f"{PHOTO_PREFIX}:ok")
    builder.button(text="✖️ Отмена", callback_data=WIZ_CANCEL)
    builder.adjust(*([2] * ((len(components) + 1) // 2)), 2)
    return builder.as_markup()


def scan_keyboard(check: object) -> InlineKeyboardMarkup:
    """FR-36: несошедшийся бланк сохраняется только явным «Всё верно» (MA-INV-50)."""
    builder = InlineKeyboardBuilder()
    if getattr(check, "date_problem", None) == "future":
        builder.button(text="✖️ Отмена", callback_data=WIZ_CANCEL)
        return builder.as_markup()
    if getattr(check, "needs_confirm", False):
        builder.button(text="✅ Всё верно — сохранить", callback_data=f"{SCAN_PREFIX}:confirm")
    else:
        label = "💾 Обновить замер" if getattr(check, "existing", None) else "💾 Сохранить"
        builder.button(text=label, callback_data=f"{SCAN_PREFIX}:save")
    builder.button(text="✖️ Отмена", callback_data=WIZ_CANCEL)
    builder.adjust(1)
    return builder.as_markup()


def scan_apply_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Применить цели", callback_data=f"{SCAN_PREFIX}:apply")
    return builder.as_markup()
