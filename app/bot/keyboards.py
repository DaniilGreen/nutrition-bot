"""Клавиатуры: reply-меню + inline для порций, действий, отмены, правки продукта, профиля."""
from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from datetime import date
from functools import cache
from zoneinfo import ZoneInfo

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from app.application.entities import Meal, MealItem, Product
from app.application.results import MealSession
from app.assets import asset_version
from app.config import get_settings
from app.domain.inedible import edible_ratio, peeled_name
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
DISH_SESSION = "s"  # хвост `dish_save:<id>:s` — набором весь приём пищи записи (FR-47)
ONBOARD_PREFIX = "onb"
PHOTO_PREFIX = "ph"  # карточка фото: ph:off:<i> / ph:peel:<i> / ph:pick:<i|x> / ph:ok (FR-34)
SCAN_PREFIX = "scan"  # карточка бланка InBody: scan:save / scan:confirm / scan:apply (FR-36)
WIZ_CANCEL = "wiz_cancel"  # «✖️ Отмена» в любом визарде (профиль/вариант) — сброс FSM
RESTORE_PREFIX = "rst"  # «⏪ Вернуть» после удаления: rst:<токен>:<c|p|d<ГГГГММДД>> (FR-41)
ITEM_WEIGHT_PREFIX = "iw"  # «✏️ Вес»: iw:<meal>[:<item>[:<граммы>|:own]] · iw:<meal>:back (FR-43)
FIX_PICK_PREFIX = "rf"  # FR-44: что заменить ответом на карточку: rf:<meal>:<item> · rf:<meal>:x
FREQ_PREFIX = "fq"  # «⚡ Частое»: fq:p:<product_id>:<граммы> / fq:s:<хэш имени набора> (FR-40)
# FR-40: потолки раздельные — наборы не вытесняют продукты. Аппка показывает ВСЕ наборы и топ-10
# частого; бот берёт тот же топ-10 и только закреплённое из него, не глубже.
FREQ_SETS_MAX = 3
FREQ_MAX = 8  # кнопок всего: наборы и продукты в одном рейтинге
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


OPEN_APP_TEXT = "📱 Открыть в приложении"
# FR-46: экраны, на которые бот открывает аппку, — тот же белый список, что у `landing` в
# web/app.js (MA-INV-78). Значение вне него аппка молча открыла бы на «Дне»; ловим до отправки.
# `[0-9]`, а не `\d`: тот в Python принимает и «٣».
_APP_SCREEN = re.compile(r"day|cat|dupes|tot|tot-week|body|set|(?:cat|recipe)-[1-9][0-9]{0,9}")


@cache
def _app_version() -> str:
    """Отпечаток статики — раз на процесс: `app` и `web` входят в образ, и любой деплой с их
    правкой пересоздаёт контейнер бота. Кнопка меню (menu.py) берёт его так же, на старте."""
    return asset_version()


def open_in_app(
    screen: str, markup: InlineKeyboardMarkup | None = None
) -> InlineKeyboardMarkup | None:
    """FR-46: `📱 Открыть в приложении` последним рядом под `markup` — сразу на экран `screen`.

    web_app-кнопка, а не ссылка `startapp` (D-MA-28): адрес тот же, что у кнопки меню, с
    отпечатком статики (MA-INV-35), плюс `&s=`. Id в экране — только выбор экрана: каталог общий,
    данные те же, что видны в списке.
    Без MINIAPP_URL кнопки нет — `markup` возвращается как был (в том числе None).
    """
    if not _APP_SCREEN.fullmatch(screen):
        raise ValueError(f"экрана аппки «{screen}» нет в белом списке (MA-INV-78)")
    base = get_settings().miniapp_url.rstrip("/")
    if not base:
        return markup
    url = f"{base}/?v={_app_version()}&s={screen}"
    button = InlineKeyboardButton(text=OPEN_APP_TEXT, web_app=WebAppInfo(url=url))
    rows = markup.inline_keyboard if markup else []
    return InlineKeyboardMarkup(inline_keyboard=[*rows, [button]])


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
        if whole:  # запись целиком — как «Повторить приём сегодня» аппки; позиции под ней — «↳»
            builder.button(
                text=f"🔁 {when} · вся запись · {round(meal.totals.kcal)} ккал",
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
    sets: Sequence[tuple[Meal, int]], frequent: Sequence[tuple[int | None, str, float, int]]
) -> InlineKeyboardMarkup | None:
    """FR-40: один рейтинг на всех — сколько раз ели за 30 дней; ≤ FREQ_MAX кнопок, наборов ≤
    FREQ_SETS_MAX. Набор не стоит первым только потому, что он набор: «Частое» — про то, что ешь
    сейчас (поправка владельца 24.10.2026). При ничьей выше продукт — его порядок уже по свежести.

    По одной кнопке в ряд: на 320 px две в ряд резали бы как раз число. Продукт без product_id
    не показываем: записать его можно только моделью, а это уже не «один тап». Граммы — в
    callback и после округления: записано будет ровно то, что на кнопке, и «0 г» не бывает.
    None — показывать нечего; пустую клавиатуру не шлём.
    """
    seen: set[str] = set()
    entries: list[tuple[int, str, str, str]] = []  # (раз, имя, хвост кнопки, callback)
    for pid, name, grams, times in frequent:
        g = round(grams, 1)
        if pid and 0 < g <= 5000:
            entries.append((times, name, f"{g:g} г", f"{FREQ_PREFIX}:p:{pid}:{g:g}"))
    for m, times in sets:  # свежие первыми (Assistant.sets) — из «Курица»/«курица» берём свежий
        name = m.template_name or ""
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            entries.append((times, "📌 " + name, f"{round(m.totals.kcal)} ккал",
                            f"{FREQ_PREFIX}:s:{set_key(name)}"))
    entries.sort(key=lambda e: -e[0])  # устойчиво: при ничьей продукт выше набора
    picked: list[tuple[int, str, str, str]] = []
    for e in entries:
        if e[1].startswith("📌 ") and sum(p[1].startswith("📌 ") for p in picked) == FREQ_SETS_MAX:
            continue
        picked.append(e)
        if len(picked) == FREQ_MAX:
            break
    if not picked:
        return None
    builder = InlineKeyboardBuilder()
    names = [n.removeprefix("📌 ") for _, n, _, _ in picked]
    for (_, name, tail, data), label in zip(picked, _freq_labels(names), strict=True):
        pin = "📌 " if name.startswith("📌 ") else ""
        builder.button(text=f"{pin}{label} · {tail}", callback_data=data)
    builder.adjust(1)
    return builder.as_markup()


def meal_actions_keyboard(
    meal: Meal, restore: InlineKeyboardButton | None = None, session: MealSession | None = None,
    *, trash: bool = True,
) -> InlineKeyboardMarkup:
    """Под свежей записью: удалить отдельную позицию (если их >1) и отменить всё.

    Это контекстная отмена «без вопроса» — кнопки знают конкретную запись. restore — «⏪ Вернуть»
    только что убранной позиции (FR-41), первой: сразу после удаления нужна именно она. session —
    приём пищи записи (FR-47): из нескольких записей «📌» сохраняет набором его целиком.
    trash=False — без «🗑 позиция» на каждую позицию: карточка только что слитого набора, где их
    пять подряд (FR-47); убрать позицию — ответом на карточку (FR-44) или в 🗒 История.
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
    # FR-47: запись — часть приёма пищи из нескольких записей → набором весь приём пищи (бургер
    # пятью сообщениями). Хвост `:s` в callback — кнопка делает то, что на ней написано: к
    # старой «Сохранить набор» мог прилипнуть кофе, и тап по ней не должен слить чужое (D-MA-29).
    # Весь приём пищи набором не сохранить (у позиции нет веса) — сама запись, если годится,
    # получает свой «📌 Сохранить набор» (AC-210). Запись уже набор — «Переименовать» (AC-209).
    whole = session is not None and len(session.meals) > 1
    if whole and all(i.grams for m in session.meals for i in m.items):
        builder.button(
            text="📌 Приём пищи — в набор",
            callback_data=f"{DISH_SAVE_PREFIX}:{meal.id}:{DISH_SESSION}",
        )
    elif len(meal.items) > 1 and all(item.grams for item in meal.items):
        builder.button(
            text="✏️ Переименовать набор" if meal.template_name else "📌 Сохранить набор",
            callback_data=f"{DISH_SAVE_PREFIX}:{meal.id}",
        )
    if len(meal.items) > 1 and trash:
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


def fix_pick_keyboard(meal: Meal, items: Sequence[MealItem]) -> InlineKeyboardMarkup:
    """FR-44: какую позицию заменить фразой из ответа (или чей это вес) — по строке на позицию и
    `✖️ Отмена`."""
    builder = InlineKeyboardBuilder()
    for it in items:
        if it.id is None:
            continue
        size = f"{half_up(it.grams)} г" if it.grams else f"{half_up(it.macros.kcal)} ккал"
        builder.button(
            text=f"{_short(it.name_snapshot, 24)} · {size}",
            callback_data=f"{FIX_PICK_PREFIX}:{meal.id}:{it.id}",
        )
    builder.button(text="✖️ Отмена", callback_data=f"{FIX_PICK_PREFIX}:{meal.id}:x")
    builder.adjust(1)
    return builder.as_markup()


def card_meal_id(markup: object) -> int | None:
    """FR-44: id приёма, если это клавиатура карточки приёма: `meal_repeat:<id>` или навигация
    `✏️ Вес` (`iw:<id>`, `iw:<id>:back`). Пикер, день, вопрос «✍️ Своё» (`iw:<m>:<i>:x`) — не
    карточка: там id нет, их несколько или это вопрос. Id ровно один — иначе None (D-MA-26)."""
    if not isinstance(markup, InlineKeyboardMarkup):
        return None
    ids: set[str] = set()
    for row in markup.inline_keyboard:
        for b in row:
            head, *rest = (b.callback_data or "").split(":")
            if (head == MEAL_REPEAT_PREFIX and len(rest) == 1) or (
                head == ITEM_WEIGHT_PREFIX and rest[1:] in ([], ["back"]) and rest
            ):
                ids.add(rest[0])
    if len(ids) != 1:
        return None
    try:  # int(), а не isdigit(): тот пропустил бы «²»
        return int(ids.pop())
    except ValueError:
        return None


def undo_picker_keyboard(
    meals: Sequence[Meal], tz_name: str, restore: InlineKeyboardButton | None = None
) -> InlineKeyboardMarkup:
    """Меню «Отменить» (FR-12/D-13): для каждой записи — она целиком + позиции (если их >1).

    Единый смысл с контекстной отменой под свежей записью: можно снять и запись целиком, и позицию.
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


def photo_keyboard(
    components: Sequence[object], off: Sequence[int], peeled: Sequence[int] = (),
) -> InlineKeyboardMarkup:
    """FR-34, D-MA-20: на каждую позицию «✖ убрать» (убранная — «↩ вернуть»), затем
    «Записать»/«Отмена». Вопросов про масло нет: его оценивает модель, оно — строкой в списке.

    FR-34.2: позиции с несъедобным («в панцире») — «Без вычета — уже чищено»; нажата —
    «↩ Вычитать несъедобное». Имена в кнопках — как на карточке (чищеное — без панциря)."""
    builder = InlineKeyboardBuilder()
    names = [getattr(c, "name", "") for c in components]
    for i, name in enumerate(names):
        mark = "↩ " if i in off else "✖ "
        shown = peeled_name(name) if i in peeled else name
        builder.button(text=mark + _short(shown), callback_data=f"{PHOTO_PREFIX}:off:{i}")
    peel = [i for i, n in enumerate(names) if i not in off and edible_ratio(n) is not None]
    for i in peel:
        if len(peel) == 1:
            text = "↩ Вычитать несъедобное" if i in peeled else "Без вычета — уже чищено"
        else:
            short = _short(names[i], 18)
            text = f"↩ Вычет: {short}" if i in peeled else f"Без вычета: {short}"
        builder.button(text=text, callback_data=f"{PHOTO_PREFIX}:peel:{i}")
    builder.button(text="✅ Записать", callback_data=f"{PHOTO_PREFIX}:ok")
    builder.button(text="✖️ Отмена", callback_data=WIZ_CANCEL)
    builder.adjust(*([2] * ((len(components) + 1) // 2)), *([1] * len(peel)), 2)
    return builder.as_markup()


def photo_weight_keyboard(grams: float) -> InlineKeyboardMarkup:
    """FR-34.1: «вес 72,4» при открытой карточке — тарелка или вес тела?"""
    n = f"{grams:g}".replace(".", ",")
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🍽 Тарелка {n} г", callback_data=f"{PHOTO_PREFIX}:wq:plate")
    builder.button(text=f"⚖️ Мой вес {n} кг", callback_data=f"{PHOTO_PREFIX}:wq:body")
    builder.adjust(1)
    return builder.as_markup()


def photo_pick_keyboard(
    names: Sequence[str], cands: Sequence[int], *, separate: bool = False
) -> InlineKeyboardMarkup:
    """FR-34.1: «Чей вес «150 г»?» — позиции и «✖️ Отмена»; имя не нашлось на карточке —
    ещё «➕ Записать отдельно» (обычный путь, карточка остаётся)."""
    builder = InlineKeyboardBuilder()
    for i in cands:
        builder.button(text=_short(names[i], 30), callback_data=f"{PHOTO_PREFIX}:pick:{i}")
    if separate:
        builder.button(text="➕ Записать отдельно", callback_data=f"{PHOTO_PREFIX}:pick:new")
    builder.button(text="✖️ Отмена", callback_data=f"{PHOTO_PREFIX}:pick:x")
    builder.adjust(1)
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
