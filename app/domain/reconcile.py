"""Сверка дневника с весами: сходятся ли цифры (§4.0).

Главное открытие проекта: дыра не в плотностях, а в пропущенных записях.
Дневник показывал 1429 ккал в день при весе, выросшем с 86,8 до 91,5 кг —
такое расхождение не объясняется ошибкой в плотности борща.

Панель не обвиняет и не считает «сколько ты недозаписал». Она кладёт рядом два
факта — что записано и что показали весы — и говорит, сходятся ли они. Вывод
человек делает сам (MA-INV-28: ни одного оценочного слова).

Физика: 1 кг жировой ткани ≈ 7700 ккал. Число грубое (ткань не чистый жир,
вода и гликоген гуляют), поэтому мы никогда не называем «недостающие калории»
точной цифрой — только диапазон и вердикт.
"""
from __future__ import annotations

from dataclasses import dataclass

KCAL_PER_KG = 7700.0

# Порог, ниже которого расхождение не считаем значимым. Вес шумит на воде и
# гликогене; при коротком окне ±0,5 кг это шум, а не еда.
NOISE_KG = 0.5

# Минимум дней, при котором вывод вообще имеет смысл. На трёх днях вес скажет
# больше про соль и стул, чем про питание.
MIN_DAYS = 14


@dataclass(frozen=True)
class Reconciliation:
    days: int
    logged_kcal_per_day: float
    weight_delta_kg: float
    # Сколько калорий в день объясняет изменение веса сверх записанного.
    gap_kcal_per_day: float
    verdict: str  # enough_data_agree | enough_data_gap | not_enough_data | no_weight

    @property
    def agrees(self) -> bool:
        return self.verdict == "enough_data_agree"


def reconcile(
    *,
    days: int,
    logged_kcal_total: float,
    days_with_records: int,
    weight_start_kg: float | None,
    weight_end_kg: float | None,
    maintenance_kcal: float,
) -> Reconciliation:
    """Сопоставить записанное с изменением веса.

    `maintenance_kcal` — сколько человек тратит в день по своей цели. Точного
    расхода мы не знаем и не пытаемся вычислять: цель — это то, от чего человек
    сам отталкивается, и сравнение с ней честнее выдуманной формулы.
    """
    per_day = logged_kcal_total / days_with_records if days_with_records else 0.0

    if weight_start_kg is None or weight_end_kg is None:
        return Reconciliation(days, per_day, 0.0, 0.0, "no_weight")
    if days < MIN_DAYS or days_with_records < MIN_DAYS // 2:
        return Reconciliation(days, per_day, weight_end_kg - weight_start_kg, 0.0,
                              "not_enough_data")

    delta = weight_end_kg - weight_start_kg
    if abs(delta) < NOISE_KG:
        # Вес практически стоит: ожидаем, что съедено примерно на поддержание.
        expected = maintenance_kcal
    else:
        expected = maintenance_kcal + (delta * KCAL_PER_KG) / days

    gap = expected - per_day
    # Порог значимости — десятая часть поддержания: меньше тонет в неточности
    # и цели, и коэффициента 7700.
    significant = abs(gap) > maintenance_kcal * 0.1
    return Reconciliation(
        days=days,
        logged_kcal_per_day=round(per_day, 1),
        weight_delta_kg=round(delta, 1),
        gap_kcal_per_day=round(gap, 1),
        verdict="enough_data_gap" if significant else "enough_data_agree",
    )


def demo() -> None:
    """Runnable-чек: согласие, расхождение, нехватка данных."""
    # Реальный случай владельца: дневник 1429, вес +4,7 кг за 30 дней.
    r = reconcile(
        days=30, logged_kcal_total=1429 * 28, days_with_records=28,
        weight_start_kg=86.8, weight_end_kg=91.5, maintenance_kcal=2200,
    )
    assert r.verdict == "enough_data_gap"
    assert r.gap_kcal_per_day > 1000, r.gap_kcal_per_day   # дыра больше тысячи в день
    assert r.weight_delta_kg == 4.7

    # Дневник сходится: ел на поддержание, вес стоит.
    ok = reconcile(
        days=30, logged_kcal_total=2200 * 30, days_with_records=30,
        weight_start_kg=90.0, weight_end_kg=90.1, maintenance_kcal=2200,
    )
    assert ok.agrees, ok

    # Короткое окно вывода не даёт: вес там про воду, а не про еду.
    short = reconcile(
        days=5, logged_kcal_total=1000 * 5, days_with_records=5,
        weight_start_kg=90.0, weight_end_kg=91.0, maintenance_kcal=2200,
    )
    assert short.verdict == "not_enough_data"

    # Нет замеров — нет сверки, и мы не притворяемся.
    none = reconcile(
        days=30, logged_kcal_total=1000, days_with_records=10,
        weight_start_kg=None, weight_end_kg=None, maintenance_kcal=2200,
    )
    assert none.verdict == "no_weight"
    print("сверка дневника с весами: ок")


if __name__ == "__main__":
    demo()
