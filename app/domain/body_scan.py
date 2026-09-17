"""Замер состава тела с бланка InBody (MINIAPP-SPEC §6.24, FR-36).

Здесь: что такое замер, как проверить, что бланк прочитан верно, и что из него
следует для целей. Числа на бланке избыточны — вес = жир + БЖМ, % = жир / вес,
БЖМ = вода + белок + минералы, обмен = 370 + 21.6 × БЖМ. Любая одиночная ошибка
чтения ломает хотя бы одну связь (MA-INV-50).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, fields, replace
from datetime import datetime

# Белок на кг безжировой массы. В дефиците 2.3–3.1 (Helms 2014, для худых атлетов);
# при 20%+ жира уместна нижняя граница, 2.4 — с запасом. Пол — 1.6 г/кг ВСЕГО веса
# (Morton 2018, плато синтеза мышечного белка).
PROTEIN_PER_KG_FFM = {"cut": 2.4, "maintain": 2.0, "bulk": 2.2}
PROTEIN_FLOOR_PER_KG = 1.6

# Допуски сверки. InBody округляет каждую строку до десятых, поэтому сумма
# трёх округлённых чисел может разойтись на пару десятых сама по себе.
TOL_MASS = 0.3
TOL_PCT = 0.5
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
    id: int | None = None
    user_id: int | None = None


NUMERIC = [f.name for f in fields(BodyScan) if f.name in RANGES]


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


def parse_blank_date(text: str, language: str) -> tuple[tuple[int, int, int] | None, bool]:
    """Дата с бланка → ((год, месяц, день), неоднозначна ли).

    Год первым — Г.М.Д. Английский бланк при дне и месяце ≤ 12 — неоднозначен
    (американский порядок М.Д). Остальные языки — Д.М.Г, как на вьетнамском и
    русском бланках."""
    nums = [int(x) for x in re.findall(r"\d+", text or "")]
    if len(nums) < 3:
        return None, True
    a, b, c = nums[:3]
    if a > 31:
        y, m, d = a, b, c
        ambiguous = False
    else:
        y = c if c > 99 else 2000 + c
        d, m = a, b
        ambiguous = False
        if language.lower().startswith("en"):
            m, d = a, b
            ambiguous = a <= 12 and b <= 12 and a != b
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

    # Даты: вьетнамский Д.М.Г, год первым, английский неоднозначен (AC-MA-62).
    assert parse_blank_date("03.05.2026 15:44", "vi") == ((2026, 5, 3), False)
    assert parse_blank_date("2026.05.03", "en") == ((2026, 5, 3), False)
    assert parse_blank_date("05.03.2026", "en") == ((2026, 5, 3), True)
    assert parse_blank_date("05.23.2026", "en") == ((2026, 5, 23), False)
    assert parse_blank_date("31.12.26", "ru") == ((2026, 12, 31), False)
    assert parse_blank_date("", "vi") == (None, True)
    assert parse_blank_date("13.13.2026", "vi") == (None, True)
    print("замер состава: ок")


if __name__ == "__main__":
    demo()
