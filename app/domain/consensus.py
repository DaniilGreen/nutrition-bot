"""Self-consistency оценки (D-15/INV-12) — механизм против «правдоподобного-но-неверного».

Настоящее знание модели стабильно между сэмплами, галлюцинация — нет. Разброс (CV) даёт
КАЛИБРОВАННУЮ уверенность, в отличие от self-reported `confidence` от LLM (некалиброван).
Чистые функции, без I/O.
"""
from __future__ import annotations

import statistics

# Температура сэмплов оценки (не извлечения): нужен разброс, чтобы померить согласие.
SAMPLE_TEMPERATURE = 0.6
# Пороги CV → уровень уверенности.
CV_HIGH = 0.15  # ≤ → консенсус: пиннить медиану
CV_ABSTAIN = 0.35  # > → воздержание: не пиннить, пометить грубой оценкой


def median_cv(values: list[float]) -> tuple[float, float]:
    """Медиана и коэффициент вариации (σ/μ). Пусто → (0,0); один сэмпл → (v, 0)."""
    xs = [v for v in values if v is not None]
    if not xs:
        return (0.0, 0.0)
    med = statistics.median(xs)
    mean = statistics.mean(xs)
    if len(xs) == 1 or mean == 0:
        return (med, 0.0)
    return (med, statistics.pstdev(xs) / abs(mean))


def confidence_from_cv(cv: float) -> str:
    """CV → уровень: high (консенсус) / med (пиннить, но пометить) / low (воздержание)."""
    if cv <= CV_HIGH:
        return "high"
    if cv <= CV_ABSTAIN:
        return "med"
    return "low"


def worst(*levels: str | None) -> str | None:
    """Худший (наименее уверенный) из уровней; None игнорируется (один сэмпл/легаси)."""
    order = {"high": 0, "med": 1, "low": 2}
    present = [x for x in levels if x in order]
    return max(present, key=lambda x: order[x]) if present else None
