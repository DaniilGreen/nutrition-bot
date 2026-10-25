"""Изменение веса — только разности записанных замеров, без сглаживания (FR-8, D-40).

«Как записал, так и показывай»: каждое число на экране веса можно проверить по списку замеров.
Одна функция кормит и текст «⚖️ Вес», и PNG — чтобы числа не разъехались.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

MONTH = timedelta(days=28)  # «за 4 недели»: сравниваем с замером, ближайшим к этой дате
MONTH_MIN = timedelta(days=21)  # самый ранний замер позже — сравнивать не с чем, числа нет


@dataclass(frozen=True)
class WeightChange:
    last_at: datetime
    last_kg: float
    first_at: datetime
    period_kg: float          # последний − первый из показанных
    month_kg: float | None    # последний − ближайший к «последний − 28 дн.»; None — истории мало


def weight_change(points: list[tuple[datetime, float]]) -> WeightChange | None:
    """points — (время, кг), старые → новые. Пусто → None."""
    if not points:
        return None
    (t0, w0), (t1, w1) = points[0], points[-1]
    month = None
    if t1 - t0 >= MONTH_MIN:
        target = t1 - MONTH
        _, wm = min(points[:-1], key=lambda p: abs(p[0] - target))
        month = round(w1 - wm, 1)
    return WeightChange(t1, w1, t0, round(w1 - w0, 1), month)


def kg(v: float, *, sign: bool = False) -> str:
    """«90,0» / «+3,2» / «−1,6»: десятичная запятая и настоящий минус — как в аппке."""
    s = f"{v:+.1f}" if sign else f"{v:.1f}"
    return s.replace(".", ",").replace("-", "−")


def demo() -> None:
    d = datetime(2026, 10, 1)
    pts = [(d, 86.8), (d + timedelta(days=7), 86.0), (d + timedelta(days=28), 85.2)]
    c = weight_change(pts)
    assert c is not None and c.period_kg == -1.6 and c.month_kg == -1.6
    short = weight_change([(d, 86.0), (d + timedelta(days=20), 85.0)])
    assert short is not None and short.month_kg is None  # 20 дней — не месяц
    assert weight_change([]) is None
    assert kg(-1.6, sign=True) == "−1,6" and kg(90) == "90,0" and kg(0.0, sign=True) == "+0,0"


if __name__ == "__main__":
    demo()
