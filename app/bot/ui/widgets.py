"""Данные HTML-виджетов (FR-49): `chat_data` и подписи из тех же сервисов `Assistant`, что у
rich-экранов (MA-INV-1). Полный `gather()` виджету не нужен: он считает неделю и месяц, а виджет
дня пересобирается после каждого апдейта человека.

Шаблон виджета статичен (`app/bot/widgets/<имя>.html`), всё конкретное — здесь, JSON ≤ 1024
байт (W-INV-3). Числа посчитаны Python'ом (W-INV-1): JS их только рисует.

Формат `today` (v1), ключи короткие — бюджет 1024 байта UTF-8, а кириллица по два байта:
  d   ISO-дата дня (ключ запроса `t:<d>`)        dl  «вт, 10 ноя»       at  «14:20» — свежесть
  sh  час начала суток человека
  g   цель [ккал, Б, Ж, У]                        e   съедено [ккал, Б, Ж, У]
  l   осталось [ккал, Б] (`Remaining`, со знаком)  pk  белок на кг веса (нет веса — нет ключа)
  es  % ккал оценкой (≈, §3.5 MA)
  s   серия [дней, заморозка недели 0/1, покрыто дней, из скольких] (D-44, D-57) — «N из 30»
  h   [[индекс часа, ккал, записей]] — все часы с записями (никогда не режутся)
  it  [[индекс часа, «мм», имя, ккал, ≈ 0/1, граммы (0 — без веса), Б, Ж, У, id записи, id позиции
      (запись из одной позиции с весом — её вес правится прямо в виджете, D-74; иначе 0)]] — свежие
      первыми (режутся с хвоста);
      записей часа в `h` больше, чем в `it`, — виджет пишет «ещё N — в приложении»
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta
from html import unescape
from typing import Any
from zoneinfo import ZoneInfo

from app.application.entities import Meal
from app.application.results import DailyStats
from app.bot.keyboards import weighable
from app.bot.quick import Frequent, parse_op
from app.bot.ui.context import DayContext, est_share
from app.bot.ui.kit import MON, WD, macros_rows, pct
from app.bot.ui.screens import WeekView, week_hits
from app.domain.goals import Goal, Remaining
from app.domain.nutrition import macros_for_grams
from app.domain.streak import Streak
from app.domain.timeframe import local_date
from app.domain.value_objects import Macros

LIMIT = 1024  # байт UTF-8 на chat_data / ответ запроса (Bot API 10.4)
# Имя целиком, пока влезает в бюджет: по ширине строку режет CSS (`text-overflow`); ступени
# короче — только чтобы не выкидывать записи из ленты.
NAME_STEPS = (60, 22, 16, 12)


def _name(meal: Meal, limit: int) -> str:
    """Имя записи — то, что записано (MA-INV-64): позиции через запятую, больше двух — «+N»."""
    names = [i.name_snapshot.strip() for i in meal.items] or [meal.raw_input.strip()]
    text = ", ".join(names[:2]) + (f" +{len(names) - 2}" if len(names) > 2 else "")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def size(text: str) -> int:
    return len(text.encode())


def today_data(
    stats: DailyStats, goal: Goal, weight_kg: float | None, streak: Streak | None,
    now: datetime, start_h: int,
) -> str:
    """`chat_data` виджета «Сегодня» (FR-49.1). Всегда ≤ LIMIT байт.

    Данные — те же сервисы, что у rich-экрана (`assistant.today`, цель, вес, темп): одна правда
    (MA-INV-1). Полный `gather()` здесь не нужен — он считает неделю и месяц, а виджет
    пересобирается после каждого апдейта человека (D-46)."""
    tz = ZoneInfo(stats.timezone)
    local = now.astimezone(tz)
    day = local_date(now, stats.timezone, start_h)
    c, g = stats.remaining.consumed, goal

    def idx(dt: datetime) -> int:
        return (dt.astimezone(tz).hour - start_h) % 24

    meals = sorted(stats.meals, key=lambda m: m.eaten_at, reverse=True)
    hours: dict[int, list[float]] = {}  # час → [ккал, записей]
    for m in meals:
        hk = hours.setdefault(idx(m.eaten_at), [0.0, 0])
        hk[0] += m.totals.kcal
        hk[1] += 1
    base: dict[str, Any] = {
        "v": 1,
        "d": day.isoformat(),
        "dl": f"{WD[day.weekday()].lower()}, {day.day} {MON[day.month - 1]}",
        "at": local.strftime("%H:%M"),
        "sh": start_h,
        "g": [round(g.daily_kcal), round(g.protein_g), round(g.fat_g), round(g.carb_g)],
        "e": [round(c.kcal), round(c.protein), round(c.fat), round(c.carb)],
        # Остатки — тем же `Remaining`, что у rich-экрана и подписи: JS их не вычитает (W-INV-1).
        "l": [round(stats.remaining.kcal_left), round(stats.remaining.protein_left)],
        "es": est_share(stats),
        "h": [[h, round(k), int(n)] for h, (k, n) in sorted(hours.items())],
    }
    if weight_kg:
        base["pk"] = round(c.protein / weight_kg, 1)
    if streak is not None and streak.of > 0:
        base["s"] = [streak.days, int(streak.frozen), streak.covered, streak.of]

    text = ""
    for limit in NAME_STEPS:
        rows = [
            [idx(m.eaten_at), m.eaten_at.astimezone(tz).strftime("%M"), _name(m, limit),
             round(m.totals.kcal), int(any(i.is_estimate for i in m.items)),
             round(sum(i.grams or 0 for i in m.items)), round(m.totals.protein),
             round(m.totals.fat), round(m.totals.carb), m.id or 0,
             (m.items[0].id or 0) if len(m.items) == 1 and m.items[0].grams else 0]
            for m in meals
        ]
        while True:
            text = _dump({**base, "it": rows})
            if size(text) <= LIMIT or not rows:
                break
            rows = rows[:-1]  # выпадает самая старая запись; её час с суммой остаётся в `h`
        if len(rows) == len(meals):
            break
    if size(text) > LIMIT:  # ponytail: лента целиком выпала — часы с суммами остались
        text = _dump({**base, "it": []})
    return text


def frequent_data(freq: Frequent) -> str:
    """`chat_data` виджета «⚡ Частое» (FR-49.2): ровно плитки клавиатуры FR-40 — тот же топ,
    те же подписи и граммы (`frequent_tiles`, одна выборка `quick.frequent`), плюс ккал порции.

    Формат v1: {"v":1,"t":[[подпись, хвост, операция, ккал, ≈ 0/1], …]}; операция — `p:<id>:<г>`
    или `s:<ключ набора>`, она же уходит в запрос `w:<операция>:<ключ>`. Ккал продукта —
    граммы × запиннутая плотность (INV-2), набора — его записанный итог (INV-3)."""
    tiles = []
    for t in freq.tiles:
        op = parse_op(t.op)
        kcal, est = 0, 0
        if isinstance(op, tuple) and (prod := freq.products.get(op[0])) is not None:
            kcal = round(macros_for_grams(prod.density, op[1]).kcal)
            est = int(prod.is_estimate)
        elif isinstance(op, str) and (m := freq.sets.get(op)) is not None:
            kcal = round(m.totals.kcal)
            est = int(any(i.is_estimate for i in m.items))
        tiles.append([t.label, t.tail, t.op, kcal, est])
    text = _dump({"v": 1, "t": tiles})
    while size(text) > LIMIT and tiles:  # ponytail: 8 плиток × короткие подписи влезают с запасом
        tiles.pop()
        text = _dump({"v": 1, "t": tiles})
    return text


def weight_data(points: list[tuple[datetime, float]], tz: str, target: str) -> str | None:
    """`chat_data` виджета «⚖️ Вес» (FR-49.3): записанные замеры как есть (D-40).

    Формат v1: "p" — [[дни от первого замера, кг, номер дня замера от "a0"], …]: первое — место на
    оси по настоящему времени, третье — местная дата замера, посчитанная здесь, как у списка
    «📋 Замеры» rich-экрана (JS только прибавляет целые дни к "a0" для подписи тапа);
    "a0" — ISO-дата первого замера; "a"/"z" — подписи краёв оси; "l" — последний;
    "dp" — Δ за период; "dm" — Δ за 4 недели или null; "st" — статус Δ (ok/bad/mute, MA-INV-20);
    "dd" — Δ с прошлого замера (последний − предпоследний: то, что писал текст «Вес записан»).
    Все разности — `weight_change`, как в тексте экрана: числа не разъедутся. None — замеров
    меньше двух (график не нужен, FR-8)."""
    from app.domain.weight_change import weight_change

    zone = ZoneInfo(tz)
    pts = list(points)
    while len(pts) >= 2:
        ch = weight_change(pts)
        assert ch is not None
        t0 = pts[0][0]
        d0 = t0.astimezone(zone).date()
        first, last = d0, pts[-1][0].astimezone(zone).date()
        status = (
            "mute" if target == "maintain" or abs(ch.period_kg) < 0.05
            else "ok" if (ch.period_kg < 0) == (target != "bulk") else "bad"
        )
        data = {
            "v": 1,
            "a0": d0.isoformat(),
            "a": f"{first.day} {MON[first.month - 1]}",
            "z": f"{last.day} {MON[last.month - 1]}",
            "p": [[round((t - t0).total_seconds() / 86400, 1), round(w, 1),
                   (t.astimezone(zone).date() - d0).days] for t, w in pts],
            "l": round(ch.last_kg, 1),
            "dp": ch.period_kg,
            "dm": ch.month_kg,
            "dd": round(pts[-1][1] - pts[-2][1], 1),
            "st": status,
        }
        text = _dump(data)
        if size(text) <= LIMIT:
            return text
        pts = pts[1:]  # старейший замер уходит; Δ «за период» — по показанным, как и было
    return None


MEAL_NAME_STEPS = (28, 18, 12)


def meal_data(
    meal: Meal, left: Remaining, tz: str, *, session_kcal: float | None = None,
    undo: int = 0, deleted: bool = False, head: str | None = None, rows: bool = False,
) -> str | None:
    """`chat_data` карточки приёма (FR-49.4). None — не влезла в бюджет: вызывающий шлёт rich.

    Формат v1 (ключи короткие — бюджет 1024 байта, ответ на запрос — те же данные + "ok"):
      m   id записи                     at  «13:05» — время записи      dl  «вт, 10 ноя»
      i   [[id позиции, имя, граммы, ккал, ≈ 0/1, вес правится 0/1]] — порядок записи
      k   записью [ккал, Б, Ж, У]
      dy  день записи [[съедено, цель, %] × Ккал, Б, Ж, У] — те же `macros_rows`/`pct`, что у
          rich-карточки (MA-INV-1), от `Remaining` дня записи (вчерашняя — вчерашний день)
      ss  ккал приёма пищи (FR-47), если записей в нём больше одной
      z   что можно вернуть этим виджетом: 0 — ничего, 1 — позицию, 2 — запись
      x   1 — запись удалена (карточка показывает «удалена» и «⏪ Вернуть»)
      rv  ревизия (мс): `chatDataChanged` старше показанного виджет пропускает
      hd  шапка вместо «Записал» («📌 Пиши «бургер»» у набора, FR-47); нет ключа — «Записал»
      nb  (только у `merged_data`) имя набора, в который вошла запись
      l   1 — блок дня столбиком (`ui_prefs.card_macros`, D-76); нет ключа — сеткой 2×2
    Граммы и ккал позиции — снапшот (INV-3); JS лишь предпросматривает пропорцию (W-INV-1)."""
    zone = ZoneInfo(tz)
    at = meal.eaten_at.astimezone(zone)
    can = {i.id for i in weighable(meal)}
    t = meal.totals
    base: dict[str, Any] = {
        "v": 1, "m": meal.id, "at": at.strftime("%H:%M"),
        "dl": f"{WD[at.weekday()].lower()}, {at.day} {MON[at.month - 1]}",
        "k": [round(t.kcal), round(t.protein), round(t.fat), round(t.carb)],
        "dy": [[round(c), round(g), pct(c, g)]
               for _, c, g in macros_rows(left.consumed, left.goal)],
        "z": undo, "x": int(deleted),
        "rv": time.time_ns() // 1_000_000 % 10**10,  # ревизия: старшая правка не ляжет поверх
    }
    if session_kcal is not None:
        base["ss"] = round(session_kcal)
    if head:
        base["hd"] = head
    if rows:
        base["l"] = 1
    for limit in MEAL_NAME_STEPS:
        items = []
        for i in meal.items:
            name = i.name_snapshot.strip()
            name = name if len(name) <= limit else name[: limit - 1].rstrip() + "…"
            grams = round(i.grams, 1) if i.grams else 0
            items.append([i.id, name, grams, round(i.macros.kcal), int(i.is_estimate),
                          int(i.id in can)])
        text = _dump({**base, "i": items})
        if size(text) <= LIMIT - 8:  # запас под "ok":1 в ответе на запрос
            return text
    return None


def merged_data(name: str) -> str:
    """Карточка, где нажали «📌 Приём пищи — в набор», после слияния (FR-47, FR-49.4): её записей
    больше нет — только след «вошло в набор», без позиций и действий."""
    return _dump({
        "v": 1, "m": 0, "x": 1, "z": 0, "nb": name[:64], "hd": "📌 Набор", "i": [],
        "k": [0, 0, 0, 0], "rv": time.time_ns() // 1_000_000 % 10**10,
    })


def meal_caption(meal: Meal) -> str:
    """«✅ Записал: …» именами — текст, когда карточка-виджет после подтверждения не ушла (FR-49.7).
    У самого виджета подписи нет (D-75)."""
    names = ", ".join(i.name_snapshot.strip() for i in meal.items)
    if len(names) > 120:
        names = names[:119].rstrip() + "…"
    return f"✅ Записал: {names}"


_CELL = {"none": 0, "ok": 1, "low": 2, "over": 3}


def week_data(v: WeekView, macros: dict[date, Macros], today: date, weeks_ago: int = 0) -> str:
    """`chat_data` виджета «📈 Неделя» (FR-49.5): те же числа, что у rich-экрана (`week_view`).

    Формат v1:
      t   «3–9 ноя»          h  герой словами          hs  строка под ним     ov  1 — перебор
      c   [[«пн 3», клетка 0…3 (нет/в цели/меньше половины/перебор), ккал, Б, Ж, У, цель ккал,
           сегодня 0/1, будущее 0/1]] — семь дней
      a   средние [ккал, Б, Ж, У] вошедших дней      g  цели [ккал, Б, Ж, У]
      al  «В среднем за 5 дн.»   k  [в цели, судимых дней]   tl  подвал
      wa  недель назад (0 — текущая; листание ‹ › — запрос `k:<wa>`)
    Клетка — правило баланса (§6.23): сегодня не судим, пока не перебор (D-29)."""
    hit, logged = week_hits(v)
    days = []
    for day, goal, cell in zip(v.days, v.goals, v.cells, strict=True):
        m = macros.get(day, Macros(0, 0, 0, 0))
        days.append([
            f"{WD[day.weekday()].lower()} {day.day}", _CELL[cell], round(m.kcal),
            round(m.protein), round(m.fat), round(m.carb), round(goal),
            int(day == today), int(day > today),
        ])
    a, g = v.avg, v.goal
    return _dump({
        "v": 1, "t": v.title, "h": v.hero, "hs": v.sub, "ov": int(v.over), "c": days,
        "a": [round(a.kcal), round(a.protein), round(a.fat), round(a.carb)],
        "g": [round(g.daily_kcal), round(g.protein_g), round(g.fat_g), round(g.carb_g)],
        "al": v.avg_label, "k": [hit, logged], "tl": v.tail, "wa": weeks_ago,
    })


def _plain(html: str) -> str:
    """Строка rich-блока текстом для виджета: те же слова, что в rich-итоге, без разметки."""
    text = re.sub(r"<br\s*/?>", " ", html)
    return unescape(re.sub(r"<[^>]+>", "", text)).strip()


def evening_data(
    d: DayContext, day: date, streak: Streak | None, *, closed: bool = False
) -> str:
    """`chat_data` вечернего итога (FR-49.6): цифры дня и те же слова, что у rich-итога
    (`screens.evening`: перебор, добор белка, неделя, якорь 7 дней) — текстом.

    Формат v1:
      d   ISO-дата дня (запрос `e:c:<d>`)   dl  «вт, 10 ноя»   c  1 — день закрыт «всё записал»
      e   съедено [ккал, Б, Ж, У]            g   цель [ккал, Б, Ж, У]
      nt  строки итога (режутся с хвоста под бюджет)    s  серия, как у «Сегодня» (D-57)
      lo  1 — записано меньше 40 % цели (похоже, не всё попало в бота)"""
    from app.bot.ui import blocks

    c, g = d.today.remaining.consumed, d.goal
    low = c.kcal < g.daily_kcal * 0.4
    notes = [] if low else [blocks.over_note(d), blocks.advice(d)]
    notes += [blocks.week_balance(d), blocks.anchor(d)]
    base: dict[str, Any] = {
        "v": 1, "d": day.isoformat(),
        "dl": f"{WD[day.weekday()].lower()}, {day.day} {MON[day.month - 1]}",
        "c": int(closed), "lo": int(low),
        "e": [round(c.kcal), round(c.protein), round(c.fat), round(c.carb)],
        "g": [round(g.daily_kcal), round(g.protein_g), round(g.fat_g), round(g.carb_g)],
    }
    if streak is not None and streak.of > 0:
        base["s"] = [streak.days, int(streak.frozen), streak.covered, streak.of]
    lines = [p for p in (_plain(x) for x in notes) if p]
    text = _dump({**base, "nt": lines})
    while size(text) > LIMIT and lines:
        lines.pop()
        text = _dump({**base, "nt": lines})
    return text


def _diary_words(d: Any, since: date | None) -> tuple[str, str, int]:
    """Сверка «ждали — показали» (`energy.diary_check`) — те же слова, что в «Итогах» аппки."""
    from app.domain.weight_change import kg

    s = f"{since.day:02d}.{since.month:02d}" if since else ""
    if d.verdict == "waiting" and d.days < 7:
        return "Сверка с дневником", f"Появится через неделю записей после замера {s}.", 0
    if d.verdict == "waiting":
        text = (
            f"С {s} по дневнику дефицит ≈ {round(d.deficit_kcal)} ккал — около "
            f"{kg(abs(d.expected_kg))} кг, в основном жир. Следующий замер покажет, сколько ушло."
            if d.deficit_kcal >= 0 else
            f"С {s} записано больше расхода на {round(-d.deficit_kcal)} ккал — около "
            f"{kg(d.expected_kg, sign=True)} кг. Следующий замер покажет, так ли это."
        )
        return "Сверка с дневником", text, 0
    if d.verdict == "few_logs":
        return ("Сверка с дневником",
                f"Записано {d.logged_days} дн. из {d.days} — сверять не по чему.", 0)
    exp, got = kg(d.expected_kg, sign=True), kg(d.actual_kg or 0, sign=True)
    if d.verdict == "agree":
        return "Сходится", f"Ждали {exp} кг, весы показали {got}. Дневнику можно верить.", 0
    if d.verdict == "less_lost":
        return ("Ушло меньше, чем ждали", f"{got} кг вместо {exp}. Обычно так, если не всё "
                "записано или расход ниже расчётного.", 1)
    return ("Ушло больше, чем ждали", f"{got} кг вместо {exp}. Расход, похоже, выше расчётного — "
            "или часть потери вода.", 1)


def burn_data(ov: Any, today: date) -> str:
    """`chat_data` виджета «🔥 Реальный расход» (FR-50): та же сводка `BodyScanService.overview`,
    что у «Итогов» аппки (MA-INV-1) — числа и слова Python, JS рисует.

    Формат v1:
      st  none — замеров нет · wait — нет пары замеров 28–120 дней · few — мало полных дней · ok
      t   расход по замерам, ккал/день   er  погрешность ±   in  съедено в среднем (полный день)
      fb  расход по формуле (обмен × активность)   pp  цель по расходу (можно ставить)
      ld/dd/nd  полных дней / дней в окне / нужно   nt  строка состояния
      dt/dx/da  сверка дневника: заголовок, текст, 1 — тревога"""
    from app.domain import energy

    data: dict[str, Any] = {"v": 1}
    if not ov.scans:
        data.update(st="none", nt="Пришли фото бланка InBody — расход посчитаю по составу тела "
                                  "между двумя замерами с зазором от 4 недель.")
        return _dump(data)
    if ov.burn_kcal:
        data["fb"] = round(ov.burn_kcal)
    sp = ov.spend
    if sp is None or sp.verdict == "short_window":
        base = min((s.measured_at for s in ov.scans
                    if (today - s.measured_at.date()).days <= energy.WINDOW_MAX_DAYS),
                   default=ov.scans[-1].measured_at)
        due = base.date() + timedelta(days=energy.WINDOW_MIN_DAYS)
        data.update(st="wait", nt=(
            f"Нужен замер не раньше {due.day} {MON[due.month - 1]} — тогда расход посчитается "
            "по двум замерам." if due > today else
            "Следующий замер InBody даст расход: зазор с прошлым уже больше 4 недель."))
    elif sp.verdict == "few_logs":
        need = -(-sp.days * 8 // 10)  # ceil(0.8 × дней), как `need_days` в «Итогах»
        data.update(st="few", ld=sp.logged_days, dd=sp.days, nd=need,
                    nt=f"Записано {sp.logged_days} полных дн. из {sp.days}, нужно от {need}.")
    else:
        data.update(st="ok", t=round(sp.tdee_kcal), er=round(sp.error_kcal),
                    **{"in": round(sp.intake_kcal)}, ld=sp.logged_days, dd=sp.days)
        p = ov.proposal
        if sp.proposable and p is not None and p.base == "energy" and p.changed:
            data["pp"] = round(p.proposed.daily_kcal)
        elif not sp.proposable:
            data["nt"] = "Разброс пока широкий — ещё один замер через месяц, и можно будет " \
                         "предложить цель по расходу."
    if ov.diary is not None:
        data["dt"], data["dx"], data["da"] = _diary_words(ov.diary, ov.diary_since)
    return _dump(data)


def confirm_data(items: list[Any]) -> str | None:
    """`chat_data` подтверждения разбора (FR-49.7). None — не влезло: вызывающий задаёт вопросы
    прежней цепочкой.

    Формат v1: {"v":1,"i":[[имя, граммы, к, б, ≈ 0/1, [S,M,L] граммы или 0, режим], …]}
      режим 0 — граммы правятся, к/б — ккал и белок на грамм (та плотность, что запишется)
            1 — вес назван в имени: граммы не правятся, к/б на грамм
            2 — свои КБЖУ (FR-22): к/б — на всю позицию
      граммы 0 — размер не выбран (есть [S,M,L]): «Записать» не пишет, пока не выбран (FR-21)."""
    rows = []
    for p in items:
        mode = 2 if not p.per_gram else 1 if p.fixed else 0
        sizes = [p.sizes["small"], p.sizes["medium"], p.sizes["large"]] if p.sizes else 0
        k, b = (round(p.kcal, 4), round(p.protein, 4)) if p.per_gram else (round(p.kcal),
                                                                           round(p.protein))
        rows.append([p.name if len(p.name) <= 30 else p.name[:29].rstrip() + "…", p.grams, k, b,
                     int(p.estimate), sizes, mode])
    text = _dump({"v": 1, "i": rows})
    return text if size(text) <= LIMIT else None


DAY_NAME_STEPS = (60, 26, 18, 12)  # полное имя, пока влезает: ширину режет CSS


def day_data(dv: Any, rem: Remaining, *, undo: int = 0) -> str:
    """`chat_data` «🗒 Истории» (FR-49.8): тот же `day_card`, что у rich-экрана дня (MA-INV-1),
    приёмы пищи — группами `MealSession` (FR-47). Всегда ≤ LIMIT: имена режутся, хвост дня
    уходит в «ещё N — в приложении».

    Формат v1:
      da  дней назад (0 — сегодня)     t  «Сегодня, 10 ноя»     z  1 — есть что «⏪ Вернуть»
      dy  день [[съедено, цель, %] × Ккал, Б, Ж, У] (`macros_rows`/`pct`, как карточка приёма)
      g   приёмы пищи из нескольких записей: [[«18:48–18:56», ккал, ≈ 0/1]]
      e   позиции: [[id записи, id позиции, «чч:мм», имя, граммы, ккал, Б, Ж, У, ≈ 0/1,
           индекс в g или −1, у записи больше одной позиции 0/1]]
      mo  позиций не влезло"""
    from app.application.results import MealSession
    from app.bot.ui.screens import _day_title

    zone = ZoneInfo(dv.timezone)
    hm = lambda t: t.astimezone(zone).strftime("%H:%M")  # noqa: E731
    groups: list[list[Any]] = []
    raw: list[tuple[Any, Any, int]] = []
    for s in dv.sessions or MealSession.group(dv.meals):
        gi = -1
        if len(s.meals) > 1:
            gi = len(groups)
            groups.append([f"{hm(s.start)}–{hm(s.end)}",
                           round(sum(m.totals.kcal for m in s.meals)),
                           int(any(i.is_estimate for m in s.meals for i in m.items))])
        raw += [(m, it, gi) for m in s.meals for it in m.items]
    base: dict[str, Any] = {
        "v": 1, "da": dv.days_ago, "t": _day_title(dv), "z": undo, "g": groups,
        "dy": [[round(c), round(g), pct(c, g)] for _, c, g in macros_rows(rem.consumed, rem.goal)],
    }
    text = ""
    for limit in DAY_NAME_STEPS:
        rows = []
        for m, it, gi in raw:
            name = it.name_snapshot.strip()
            name = name if len(name) <= limit else name[: limit - 1].rstrip() + "…"
            k = it.macros
            rows.append([m.id or 0, it.id or 0, hm(m.eaten_at), name, round(it.grams or 0),
                         round(k.kcal), round(k.protein), round(k.fat), round(k.carb),
                         int(it.is_estimate), gi, int(len(m.items) > 1)])
        keep = rows
        while True:
            text = _dump({**base, "e": keep, "mo": len(rows) - len(keep)})
            if size(text) <= LIMIT or not keep:
                break
            keep = keep[:-1]
        if len(keep) == len(rows):
            break
    return text


def goal_data(goal: Goal, weight_kg: float | None, head: str = "") -> str:
    """`chat_data` «🎯 Цели» (FR-49.9): те же числа и г/кг, что у rich-экрана (`goal_screen`).

    Формат v1: hd шапка · tg цель (cut/maintain/bulk) · k ккал · m [[г, «1.9 г/кг» или "", доля ккал
    %] × Б, Ж, У] · wk 1 — вес есть (без веса цель кнопкой не сменить: раскладка — от веса)."""
    p, f, c = goal.protein_g, goal.fat_g, goal.carb_g
    kc = [p * 4, f * 9, c * 4]
    tot = sum(kc) or 1
    per = [f" ({round(p / weight_kg, 1)}\u00a0г/кг)" if weight_kg else "",
           f" ({round(f / weight_kg, 2)}\u00a0г/кг)" if weight_kg else "", ""]
    return _dump({
        "v": 1, "hd": head, "tg": goal.target, "k": round(goal.daily_kcal),
        "wk": int(bool(weight_kg)),
        "m": [[round(g), per[i], round(100 * kc[i] / tot)] for i, g in enumerate((p, f, c))],
    })
