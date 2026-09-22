"""Замер состава тела с бланка InBody (MINIAPP-SPEC §6.24, FR-36).

Здесь: что такое замер, как проверить, что бланк прочитан верно, и что из него
следует для целей. Числа на бланке избыточны — вес = жир + БЖМ, % = жир / вес,
БЖМ = вода + белок + минералы, обмен = 370 + 21.6 × БЖМ. Любая одиночная ошибка
чтения ломает хотя бы одну связь (MA-INV-50).
"""
from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, fields, replace
from datetime import datetime

from app.domain.energy import SCAN_FAT_ERROR_KG, WINDOW_MIN_DAYS

# Белок на кг безжировой массы. В дефиците 2.3–3.1 (Helms 2014, для худых атлетов);
# при 20%+ жира уместна нижняя граница, 2.4 — с запасом. Пол — 1.6 г/кг ВСЕГО веса
# (Morton 2018, плато синтеза мышечного белка).
PROTEIN_PER_KG_FFM = {"cut": 2.4, "maintain": 2.0, "bulk": 2.2}
PROTEIN_FLOOR_PER_KG = 1.6

# Допуски сверки. InBody округляет каждую строку до десятых, поэтому сумма
# трёх округлённых чисел может разойтись на пару десятых сама по себе. Ближе к
# этому пределу и держим: 0.3 кг жира — это 2 800 ккал в расчёте расхода, а
# лишний флажок стоит человеку одного нажатия раз в месяц.
TOL_MASS = 0.2
TOL_PCT = 0.3
TOL_FFM_PARTS = 0.6
TOL_BMR = 15.0

RANGES: dict[str, tuple[float, float]] = {
    "weight_kg": (30, 300),
    "skeletal_muscle_kg": (5, 90),
    "body_fat_kg": (1, 200),
    "body_fat_pct": (3, 70),
    "fat_free_kg": (20, 150),
    "water_l": (15, 110),
    "protein_kg": (3, 30),
    "minerals_kg": (1, 10),
    "visceral_level": (1, 20),
    "bmr_kcal": (800, 4000),
    "height_cm": (120, 230),
    "age": (10, 100),
}


@dataclass(frozen=True)
class BodyScan:
    measured_at: datetime
    weight_kg: float
    body_fat_kg: float
    fat_free_kg: float
    body_fat_pct: float | None = None
    skeletal_muscle_kg: float | None = None
    water_l: float | None = None
    protein_kg: float | None = None
    minerals_kg: float | None = None
    visceral_level: float | None = None
    bmr_kcal: float | None = None
    height_cm: float | None = None
    age: int | None = None
    sex: str | None = None          # male | female
    device: str | None = None
    smi: float | None = None              # кг/м², мышцы конечностей на рост²
    inbody_score: float | None = None     # балл прибора, 0–100
    lean_arm_l_kg: float | None = None
    lean_arm_r_kg: float | None = None
    lean_trunk_kg: float | None = None
    lean_leg_l_kg: float | None = None
    lean_leg_r_kg: float | None = None
    fat_arm_l_kg: float | None = None
    fat_arm_r_kg: float | None = None
    fat_trunk_kg: float | None = None
    fat_leg_l_kg: float | None = None
    fat_leg_r_kg: float | None = None
    id: int | None = None
    user_id: int | None = None


# Сегменты бланка: безжировая масса и жир по пяти участкам тела. Держим их
# плоскими полями, а не словарём: `BodyScan` заморожен, и поле-словарь сломало
# бы hash, а плоские бесплатно проходят через правку, json и `corrected_fields`.
SEGMENTS = ("arm_l", "arm_r", "trunk", "leg_l", "leg_r")
LIMBS = ("arm_l", "arm_r", "leg_l", "leg_r")
SEGMENT_FIELDS = tuple(f"{kind}_{seg}_kg" for kind in ("lean", "fat") for seg in SEGMENTS)
# Индекс и балл правятся человеком, сегменты — нет (MINIAPP-SPEC §6.24).
EXTRA_FIELDS = ("smi", "inbody_score")

# Мягкие границы: грубый фильтр мусора («92» вместо «9.2»). В отличие от RANGES
# флага не дают — число просто не сохраняется (MA-INV-57).
SOFT_RANGES: dict[str, tuple[float, float]] = {
    "smi": (3.0, 16.0),
    "inbody_score": (0.0, 100.0),
    "lean_arm_l_kg": (0.5, 12), "lean_arm_r_kg": (0.5, 12),
    "lean_trunk_kg": (8, 60),
    "lean_leg_l_kg": (2, 30), "lean_leg_r_kg": (2, 30),
    "fat_arm_l_kg": (0.05, 10), "fat_arm_r_kg": (0.05, 10),
    "fat_trunk_kg": (0.3, 40),
    "fat_leg_l_kg": (0.1, 20), "fat_leg_r_kg": (0.1, 20),
}

# Допуск связи «индекс × рост² = мышцы конечностей». Бюджет округления бланка:
# четыре сегмента по ±0.05 плюс индекс ±0.05 × рост² — до 0.4; 0.6 покрывает и
# рост, напечатанный целыми сантиметрами. Перепутанная цифра двигает сумму на
# килограмм и больше, так что ловить это не мешает.
TOL_SMI = 0.6
# Сумма сегментов против целого: голова в сегменты не входит. Коридоры
# откалиброваны по бланку владельца (безжировая 88.5%, жир 93.8%).
LEAN_SUM = (0.85, 1.005)
FAT_SUM = (0.80, 1.02)

NUMERIC = [f.name for f in fields(BodyScan) if f.name in RANGES or f.name in EXTRA_FIELDS]


def mismatches(s: BodyScan) -> set[str]:
    """Поля, которые не сходятся с остальными или вне разумных границ.

    Возвращаем все участники несошедшейся связи: какое из чисел прочитано
    неверно, по одной связи не понять — человек посмотрит на бланк.
    """
    bad: set[str] = set()
    for name, (lo, hi) in RANGES.items():
        v = getattr(s, name)
        if v is not None and not lo <= v <= hi:
            bad.add(name)
    if abs(s.weight_kg - (s.body_fat_kg + s.fat_free_kg)) > TOL_MASS:
        bad |= {"weight_kg", "body_fat_kg", "fat_free_kg"}
    if s.body_fat_pct is not None and s.weight_kg > 0:
        if abs(s.body_fat_pct - s.body_fat_kg / s.weight_kg * 100) > TOL_PCT:
            bad |= {"body_fat_pct", "body_fat_kg"}
    parts = (s.water_l, s.protein_kg, s.minerals_kg)
    if all(p is not None for p in parts):
        if abs(s.fat_free_kg - sum(parts)) > TOL_FFM_PARTS:  # type: ignore[arg-type]
            bad |= {"fat_free_kg", "water_l", "protein_kg", "minerals_kg"}
    if s.bmr_kcal is not None and abs(s.bmr_kcal - cunningham(s.fat_free_kg)) > TOL_BMR:
        bad |= {"bmr_kcal", "fat_free_kg"}
    if s.skeletal_muscle_kg is not None and s.fat_free_kg > 0:
        if not 0.45 <= s.skeletal_muscle_kg / s.fat_free_kg <= 0.65:
            bad |= {"skeletal_muscle_kg", "fat_free_kg"}
    return bad


def segments_ok(s: BodyScan) -> bool:
    """Сегментный блок прочитан связно (MA-INV-58, MA-INV-59).

    Меньше пяти сегментов — то же, что ни одного: четыре из пяти всегда провалят
    сумму и подсветили бы исправный бланк. Суммы сверяются с целым: голова в
    сегменты не входит, поэтому не равенство, а коридор.
    """
    vals = [getattr(s, f) for f in SEGMENT_FIELDS]
    if any(v is None for v in vals):
        return False
    for name in SEGMENT_FIELDS:
        lo, hi = SOFT_RANGES[name]
        if not lo <= getattr(s, name) <= hi:
            return False
    lean = sum(getattr(s, f"lean_{seg}_kg") for seg in SEGMENTS)
    fat = sum(getattr(s, f"fat_{seg}_kg") for seg in SEGMENTS)
    if s.fat_free_kg > 0 and not LEAN_SUM[0] <= lean / s.fat_free_kg <= LEAN_SUM[1]:
        return False
    return not (s.body_fat_kg > 0 and not FAT_SUM[0] <= fat / s.body_fat_kg <= FAT_SUM[1])


def smi_ok(s: BodyScan) -> bool:
    """Индекс × рост² ≈ безжировая масса рук и ног. Одна связь накрывает пять
    самых трудных для чтения чисел — напечатанных внутри силуэта. Нет участника —
    связь не проверяется, и оба числа остаются непроверенными."""
    limbs = [getattr(s, f"lean_{seg}_kg") for seg in LIMBS]
    if s.smi is None or not s.height_cm or any(v is None for v in limbs):
        return True
    return abs(s.smi * (s.height_cm / 100) ** 2 - sum(limbs)) <= TOL_SMI


def drop_suspect(s: BodyScan) -> BodyScan:
    """Не сошедшееся среди новых чисел — обнулить, а не подсвечивать (MA-INV-57).

    Эти числа ни на что не влияют: цена ошибки — кривая картинка раз в месяц, а
    цена лишнего подтверждения выше — человек натренируется жать «Всё верно» не
    глядя, и сверка перестанет работать на весе и жире.
    """
    patch: dict[str, None] = {}
    for name in EXTRA_FIELDS:
        v = getattr(s, name)
        lo, hi = SOFT_RANGES[name]
        if v is not None and not lo <= v <= hi:
            patch[name] = None
    if not segments_ok(s):
        # Индекс проверяется ТОЛЬКО через сегменты конечностей. Нет связного
        # блока — проверить его нечем, а живые прогоны показали, что модель
        # читает это число неверно чаще, чем верно (7.9 и 12.2 вместо 9.2).
        # Неверное число хуже пустого: по нему человек будет следить за
        # выдуманной динамикой (MA-INV-56).
        patch.update(dict.fromkeys(SEGMENT_FIELDS))
        patch["smi"] = None
    elif not smi_ok(s):
        # Сегменты сошлись сразу с двумя независимыми суммами — безжировой массой
        # и массой жира; индекс не подтверждён ничем. Значит вероятнее ошибка в
        # нём, и уносим только его.
        patch["smi"] = None
    return replace(s, **patch) if patch else s


def foreign(s: BodyScan, prev: BodyScan | None, last_weight: tuple[datetime, float] | None
            ) -> bool:
    """Похоже, бланк чужой: рост, пол или возраст не сходятся с прошлым замером,
    либо вес далёк от последнего в ленте. Спрашиваем, а не отказываем."""
    if prev is not None:
        if s.height_cm and prev.height_cm and abs(s.height_cm - prev.height_cm) > 3:
            return True
        if s.sex and prev.sex and s.sex != prev.sex:
            return True
        if s.age is not None and prev.age is not None:
            years = (s.measured_at - prev.measured_at).days / 365.25
            if abs((s.age - prev.age) - years) > 1.5:
                return True
    if last_weight is not None:
        at, kg = last_weight
        months = max(1.0, abs((s.measured_at - at).days) / 30)
        if abs(s.weight_kg - kg) > 5 * months:
            return True
    return False


def _date_order(nums: list[int]) -> str | None:
    """Порядок чисел в дате по самим числам: `ymd` | `dmy` | `mdy`, None — не решить."""
    if len(nums) < 3:
        return None
    a, b = nums[0], nums[1]
    if a > 31:
        return "ymd"
    if a > 12 and b <= 12:
        return "dmy"
    if b > 12 and a <= 12:
        return "mdy"
    if a == b <= 12:
        return "mdy"          # 05.05 — порядок ничего не меняет
    return None


def parse_blank_date(
    text: str, language: str, others: Sequence[str] = ()
) -> tuple[tuple[int, int, int] | None, bool]:
    """Дата с бланка → ((год, месяц, день), неоднозначна ли). MA-INV-55.

    Порядок дня и месяца задаёт прибор, а не язык бланка: вьетнамский InBody270
    печатает `09.22.2026`. Поэтому решают числа — а когда обе позиции ≤ 12,
    прочие даты той же распечатки (история замеров внизу бланка).
    """
    nums = [int(x) for x in re.findall(r"\d+", text or "")]
    if len(nums) < 3:
        return None, True
    order = _date_order(nums)
    if order is None:
        for other in others:
            hint = _date_order([int(x) for x in re.findall(r"\d+", other or "")])
            if hint in ("dmy", "mdy"):
                order = hint
                break
    ambiguous = order is None
    if order is None:
        # Ни бланк, ни история не решили. Русские приборы печатают Д.М, прочие — М.Д.
        order = "dmy" if language.lower().startswith("ru") else "mdy"
    a, b, c = nums[:3]
    if order == "ymd":
        y, m, d = a, b, c
    else:
        y = c if c > 99 else 2000 + c
        d, m = (a, b) if order == "dmy" else (b, a)
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return None, True
    return (y, m, d), ambiguous


def cunningham(fat_free_kg: float) -> float:
    """Базовый обмен по безжировой массе — так считает сам InBody."""
    return round(370 + 21.6 * fat_free_kg)


def mifflin(weight_kg: float, height_cm: float, age: int, sex: str) -> float:
    return round(10 * weight_kg + 6.25 * height_cm - 5 * age + (5 if sex == "male" else -161))


def protein_target(fat_free_kg: float, target: str, weight_kg: float = 0.0) -> float:
    by_ffm = PROTEIN_PER_KG_FFM.get(target, 2.0) * fat_free_kg
    return float(round(max(by_ffm, PROTEIN_FLOOR_PER_KG * weight_kg)))


def with_fields(s: BodyScan, patch: dict[str, object]) -> BodyScan:
    """Правка полей человеком: только известные числовые, числом."""
    clean: dict[str, object] = {}
    for name in NUMERIC:
        if name in patch and patch[name] is not None:
            v = float(str(patch[name]).replace(",", "."))
            if v != v or v in (float("inf"), float("-inf")):   # NaN/inf
                raise ValueError(name)
            clean[name] = int(v) if name == "age" else v
    return replace(s, **clean)


@dataclass(frozen=True)
class Change:
    days: int
    weight_kg: float
    body_fat_kg: float
    fat_free_kg: float
    skeletal_muscle_kg: float | None
    body_fat_pct: float | None
    visceral_level: float | None

    @property
    def lean_share(self) -> float | None:
        """Доля безжировой массы в потере веса. None — потеря меньше 3 кг или окно
        короче 28 дней: тонет в шуме замера (±1.4 кг на разнице двух), а в начале
        похудения вода и гликоген уходят как «безжировая масса»."""
        if self.weight_kg > -3.0 or self.days < 28:
            return None
        return self.fat_free_kg / self.weight_kg


def change(a: BodyScan, b: BodyScan) -> Change:
    def diff(x: float | None, y: float | None) -> float | None:
        return None if x is None or y is None else round(y - x, 1)

    return Change(
        days=max(1, (b.measured_at - a.measured_at).days),
        weight_kg=round(b.weight_kg - a.weight_kg, 1),
        body_fat_kg=round(b.body_fat_kg - a.body_fat_kg, 1),
        fat_free_kg=round(b.fat_free_kg - a.fat_free_kg, 1),
        skeletal_muscle_kg=diff(a.skeletal_muscle_kg, b.skeletal_muscle_kg),
        body_fat_pct=diff(a.body_fat_pct, b.body_fat_pct),
        visceral_level=diff(a.visceral_level, b.visceral_level),
    )


# На сушке доля безжировой массы в потере больше этой — повод сказать.
LEAN_SHARE_WARN = 0.35

DAYS_PER_MONTH = 30.44
# Дальше этого срок не считаем: на два года вперёд конкретное число врёт сильнее,
# чем помогает.
MAX_FORECAST_MONTHS = 24.0


@dataclass(frozen=True)
class Pace:
    """Темп изменения состава между двумя замерами, кг в месяц (MA-INV-60).

    Приводим к месяцу, иначе замеры с неравными промежутками несравнимы.
    """

    days: int
    fat_kg_per_month: float
    lean_kg_per_month: float
    noise_kg_per_month: float     # ±, шум двух замеров на том же окне

    @property
    def meaningful(self) -> bool:
        """Темп жира крупнее собственной погрешности — только тогда это движение."""
        return abs(self.fat_kg_per_month) > self.noise_kg_per_month


def pace(a: BodyScan, b: BodyScan) -> Pace | None:
    """None — окно короче 28 дней: разница тонет в погрешности прибора.

    Порог общий с окном расхода (`energy`): две константы про одно разойдутся.
    """
    days = (b.measured_at - a.measured_at).days
    if days < WINDOW_MIN_DAYS:
        return None
    per_month = DAYS_PER_MONTH / days
    return Pace(
        days=days,
        fat_kg_per_month=round((b.body_fat_kg - a.body_fat_kg) * per_month, 1),
        lean_kg_per_month=round((b.fat_free_kg - a.fat_free_kg) * per_month, 1),
        noise_kg_per_month=round(math.sqrt(2) * SCAN_FAT_ERROR_KG * per_month, 1),
    )


@dataclass(frozen=True)
class Forecast:
    """Срок до цели по проценту жира. `months` есть только при `ok` (MA-INV-61)."""

    verdict: str        # ok | one_scan | too_soon | no_target | reached | noisy | wrong_way | too_far
    target_pct: float | None
    months: float | None = None


def forecast(
    last: BodyScan, p: Pace | None, target_pct: float | None, *, pair: bool = True
) -> Forecast:
    """Продолжаем ОБЕ массы, а не только жир: уходящая безжировая сама поднимает
    процент, и прогноз по одному жиру врал бы в оптимистичную сторону.

    `t = (p·W − F) / (f − p·(f + l))`, где W — вес, F — жир, f и l — темпы.
    Порядок проверок важен: шум проверяется РАНЬШЕ знака — при темпе 0.2 кг/мес
    само направление является шумом, и говорить «идёшь не туда» нечестно.
    """
    if not pair:
        return Forecast("one_scan", target_pct)
    if p is None:
        return Forecast("too_soon", target_pct)
    if target_pct is None:
        return Forecast("no_target", target_pct)
    if last.weight_kg <= 0:
        return Forecast("no_target", target_pct)
    if last.body_fat_kg / last.weight_kg * 100 <= target_pct:
        return Forecast("reached", target_pct)
    if not p.meaningful:
        return Forecast("noisy", target_pct)
    share = target_pct / 100
    denom = p.fat_kg_per_month - share * (p.fat_kg_per_month + p.lean_kg_per_month)
    if denom == 0:
        return Forecast("wrong_way", target_pct)
    months = (share * last.weight_kg - last.body_fat_kg) / denom
    if months <= 0:
        return Forecast("wrong_way", target_pct)
    if months > MAX_FORECAST_MONTHS:
        return Forecast("too_far", target_pct)
    return Forecast("ok", target_pct, round(months, 1))


def demo() -> None:
    at = datetime(2026, 5, 3, 15, 44)
    # Бланк владельца 03.05.2026 — всё сходится.
    owner = BodyScan(
        measured_at=at, weight_kg=91.8, body_fat_kg=19.8, fat_free_kg=72.0,
        body_fat_pct=21.6, skeletal_muscle_kg=41.4, water_l=52.8, protein_kg=14.3,
        minerals_kg=4.86, visceral_level=9, bmr_kcal=1925,
        height_cm=180, age=30, sex="male", device="InBody270",
    )
    assert mismatches(owner) == set(), mismatches(owner)
    assert cunningham(72.0) == 1925
    assert mifflin(91.8, 180, 30, "male") == 1898
    assert protein_target(72.0, "cut", 91.8) == 173
    # Очень худой: пол 1.6 г/кг веса не даёт опуститься ниже.
    assert protein_target(40.0, "maintain", 60.0) == 96

    # AC-MA-55: жир прочитан как вес.
    misread = replace(owner, body_fat_kg=91.8)
    assert {"body_fat_kg", "weight_kg"} <= mismatches(misread)
    # Перепутана одна цифра в обмене.
    assert "bmr_kcal" in mismatches(replace(owner, bmr_kcal=1952))
    # Вода мимо — ломается сумма безжировой массы.
    assert "water_l" in mismatches(replace(owner, water_l=58.2))
    # Висцеральный вне шкалы.
    assert "visceral_level" in mismatches(replace(owner, visceral_level=90))

    # Правка человеком.
    fixed = with_fields(misread, {"body_fat_kg": "19,8", "лишнее": 1})
    assert mismatches(fixed) == set()
    try:
        with_fields(owner, {"weight_kg": "nan"})
        raise AssertionError("NaN прошёл")
    except ValueError:
        pass

    later = replace(owner, measured_at=datetime(2026, 7, 2, 8, 0), weight_kg=88.8,
                    body_fat_kg=17.4, fat_free_kg=71.4, body_fat_pct=19.6,
                    skeletal_muscle_kg=41.0)
    c = change(owner, later)
    assert (c.days, c.weight_kg, c.body_fat_kg, c.fat_free_kg) == (59, -3.0, -2.4, -0.6)
    assert c.lean_share is not None and abs(c.lean_share - 0.2) < 1e-9
    assert change(owner, replace(later, weight_kg=91.0)).lean_share is None
    assert change(owner, replace(later, measured_at=datetime(2026, 5, 20))).lean_share is None
    # Мышцы вне доли безжировой массы — прочитано не то.
    assert "skeletal_muscle_kg" in mismatches(replace(owner, skeletal_muscle_kg=14.3))

    # AC-MA-61: чужой бланк.
    assert not foreign(later, owner, (datetime(2026, 6, 30), 89.5))
    assert foreign(replace(later, height_cm=165), owner, None)
    assert foreign(replace(later, sex="female"), owner, None)
    assert foreign(replace(later, age=45), owner, None)
    assert foreign(replace(later, weight_kg=60), None, (datetime(2026, 6, 30), 89.5))
    assert not foreign(owner, None, None)

    # Даты. AC-MA-63: вьетнамский бланк печатает М.Д.Г — решает число 22, не язык.
    assert parse_blank_date("09.22.2026 15:26", "vi") == ((2026, 9, 22), False)
    assert parse_blank_date("2026.05.03", "en") == ((2026, 5, 3), False)
    assert parse_blank_date("22.09.2026", "vi") == ((2026, 9, 22), False)
    assert parse_blank_date("31.12.26", "ru") == ((2026, 12, 31), False)
    # AC-MA-62: обе позиции ≤ 12 и подсказок нет — неоднозначно.
    assert parse_blank_date("05.03.2026", "en") == ((2026, 5, 3), True)
    assert parse_blank_date("05.03.2026", "ru") == ((2026, 3, 5), True)
    # AC-MA-64: история того же бланка решает порядок для всей распечатки.
    assert parse_blank_date("05.03.2026", "vi", ["01.13.26 13:15"]) == ((2026, 5, 3), False)
    assert parse_blank_date("05.03.2026", "vi", ["13.01.26 13:15"]) == ((2026, 3, 5), False)
    assert parse_blank_date("05.03.2026", "vi", ["2026.01.13"]) == ((2026, 5, 3), True)
    # Одинаковые день и месяц — порядок не важен.
    assert parse_blank_date("05.05.2026", "vi") == ((2026, 5, 5), False)
    assert parse_blank_date("", "vi") == (None, True)
    assert parse_blank_date("13.13.2026", "vi") == (None, True)

    # --- новые числа бланка (D-MA-15) ------------------------------------
    # Реальный бланк владельца 22.09.2026 с сегментами.
    seg = BodyScan(
        measured_at=datetime(2026, 9, 22, 15, 26), weight_kg=91.6, body_fat_kg=20.9,
        fat_free_kg=70.7, body_fat_pct=22.8, skeletal_muscle_kg=40.7, water_l=51.8,
        protein_kg=14.2, minerals_kg=4.75, visceral_level=9, bmr_kcal=1898,
        height_cm=180, age=30, sex="male", device="InBody270",
        smi=9.2, inbody_score=82,
        lean_arm_l_kg=4.47, lean_arm_r_kg=4.29, lean_trunk_kg=32.6,
        lean_leg_l_kg=10.62, lean_leg_r_kg=10.57,
        fat_arm_l_kg=1.1, fat_arm_r_kg=1.1, fat_trunk_kg=12.0,
        fat_leg_l_kg=2.7, fat_leg_r_kg=2.7,
    )
    assert mismatches(seg) == set(), mismatches(seg)
    # AC-MA-66: 9.2 × 1.80² = 29.81 против 29.95 — сходится.
    assert segments_ok(seg) and smi_ok(seg)
    assert drop_suspect(seg) == seg

    # AC-MA-67: сдвиг запятой в ноге — весь блок отброшен, но НЕ подсвечен.
    bad_leg = replace(seg, lean_leg_r_kg=1.5)
    assert not segments_ok(bad_leg)
    dropped = drop_suspect(bad_leg)
    assert all(getattr(dropped, f) is None for f in SEGMENT_FIELDS)
    # Индекс уходит вместе с блоком: проверить его больше нечем.
    assert dropped.smi is None and dropped.weight_kg == 91.6
    assert mismatches(dropped) == set()          # сохранение не блокируется

    # Ошибка в последней цифре допуск переживает.
    assert segments_ok(replace(seg, lean_leg_r_kg=10.47))
    # AC-MA-68: прочитаны не все сегменты — блока нет, расхождения нет.
    assert not segments_ok(replace(seg, fat_trunk_kg=None))
    # AC-MA-69: нет роста — связь не проверяется, но сегменты связны сами по
    # себе, поэтому и они, и индекс остаются.
    no_h = replace(seg, height_cm=None)
    assert smi_ok(no_h) and drop_suspect(no_h) == no_h
    # Бланк без сегментов вовсе: индекс не на чем проверить — не сохраняем.
    lonely = replace(seg, **dict.fromkeys(SEGMENT_FIELDS))
    assert drop_suspect(lonely).smi is None
    # Индекс не сходится с конечностями: сегменты подтверждены двумя суммами, а
    # индекс — ничем, значит уносим его, а сегменты остаются.
    conflict = drop_suspect(replace(seg, smi=11.4))
    assert conflict.smi is None and conflict.lean_trunk_kg == 32.6
    # Суммы вне коридоров.
    assert not segments_ok(replace(seg, lean_trunk_kg=20.0, smi=None))
    assert not segments_ok(replace(seg, fat_trunk_kg=4.0, smi=None))
    # Балл «820» — мусор, но только он и обнуляется.
    score = drop_suspect(replace(seg, inbody_score=820))
    assert score.inbody_score is None and score.lean_trunk_kg == 32.6
    # Индекс правится человеком, сегмент — нет.
    edited = with_fields(seg, {"smi": "9,4", "lean_trunk_kg": 1})
    assert edited.smi == 9.4 and edited.lean_trunk_kg == 32.6

    # --- темп и срок ------------------------------------------------------
    prev = replace(seg, measured_at=datetime(2026, 8, 19, 8, 0), weight_kg=93.4,
                   body_fat_kg=22.8, fat_free_kg=70.6, body_fat_pct=24.4)
    p = pace(prev, seg)
    # AC-MA-72: за 34 дня жир −1.9 кг → −1.7 в месяц при шуме ±1.3.
    assert p is not None and (p.days, p.fat_kg_per_month) == (34, -1.7)
    assert p.lean_kg_per_month == 0.1 and p.noise_kg_per_month == 1.3 and p.meaningful
    # AC-MA-71: окно короче 28 дней — темпа нет.
    assert pace(replace(prev, measured_at=datetime(2026, 9, 13)), seg) is None

    # AC-MA-74: до 15% жира при этом темпе — около пяти месяцев.
    f = forecast(seg, p, 15.0)
    assert f.verdict == "ok" and 4.5 < f.months < 5.5, f
    # AC-MA-73: темп внутри погрешности — срока нет, и это проверяется до знака.
    quiet = pace(replace(prev, body_fat_kg=21.4), seg)
    assert quiet is not None and not quiet.meaningful
    assert forecast(seg, quiet, 15.0).verdict == "noisy"
    assert forecast(seg, quiet, 15.0).months is None
    # AC-MA-75: жир растёт, а цель ниже.
    up = pace(replace(prev, body_fat_kg=17.0, fat_free_kg=70.7, weight_kg=87.7), seg)
    assert forecast(seg, up, 15.0).verdict == "wrong_way"
    # AC-MA-76 и остальные вырожденные: числа нет ни в одном.
    for got, verdict in (
        (forecast(seg, p, 15.0, pair=False), "one_scan"),
        (forecast(seg, None, 15.0), "too_soon"),
        (forecast(seg, p, None), "no_target"),
        (forecast(seg, p, 25.0), "reached"),
    ):
        assert got.verdict == verdict and got.months is None, got
    # Срок дальше двух лет не показываем: на четырёх месяцах окна шум мал, темп
    # различим, но до 10% при нём больше двух лет.
    slow = pace(replace(prev, measured_at=datetime(2026, 5, 25), body_fat_kg=22.9,
                        fat_free_kg=70.7), seg)
    assert slow is not None and slow.meaningful, slow
    assert forecast(seg, slow, 10.0).verdict == "too_far"
    print("замер состава: ок")


if __name__ == "__main__":
    demo()
