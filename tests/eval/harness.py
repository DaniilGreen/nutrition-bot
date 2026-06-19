"""Офлайн eval-харнесс точности (NFR-1, D-15).

Чистые функции метрик отделены от LLM-прогона и юнит-тестируются без сети
(`tests/unit/test_eval_harness.py`). Живой прогон по реальному OpenAI — в
`run_eval.py` (`make eval`); он гоняет golden-set через настоящий `Assistant`,
а НЕ через `nlu.parse` напрямую — баги рождаются в КОМПОЗИЦИИ parse × resolve_grams
× gate × pin, а не только в извлечении.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path

GOLDEN = Path(__file__).resolve().parent.parent / "golden" / "meals.jsonl"
DEFAULT_ITEM_TOL = 0.20  # допуск per-item ккал по умолчанию (NFR-1: дневная сумма — отдельно)


@dataclass(frozen=True)
class ItemEval:
    """Один прогнанный кейс golden-set: истина vs выдача пайплайна."""

    gid: str
    mechanism: str
    true_kcal: float
    est_kcal: float
    clarified: bool  # бот воздержался/переспросил (порция/вариант) — ккал не сравниваем
    expected_clarify: bool


def load_golden(path: Path = GOLDEN) -> list[dict]:
    """Прочитать golden-set (JSONL). Пустые строки и `//`-комментарии пропускаются."""
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("//"):
            rows.append(json.loads(s))
    return rows


def within(est: float, true: float, tol: float) -> bool:
    """Попал ли est в ±tol от истины. Истина 0 (вода) — точное совпадение."""
    if true == 0:
        return est == 0
    return abs(est - true) / true <= tol


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(q * len(s)))], 3)


def _mape(pairs: list[tuple[float, float]]) -> dict | None:
    """MAPE по ккал: медиана (типичная ошибка) + p90 (хвост катастроф)."""
    xs = [abs(e - t) / t for e, t in pairs if t]
    if not xs:
        return None
    return {"median": round(statistics.median(xs), 3), "p90": _pct(xs, 0.9), "n": len(xs)}


def metrics(evals: list[ItemEval], tol_by_id: dict[str, float]) -> dict:
    """Сводка метрик NFR-1 со срезами по механизму (средние без срезов врут)."""
    logged = [e for e in evals if not e.clarified]

    def tol(gid: str) -> float:
        return tol_by_id.get(gid, DEFAULT_ITEM_TOL)

    abst = [e for e in evals if e.expected_clarify]
    by_mech: dict[str, dict] = {}
    for mech in sorted({e.mechanism for e in evals}):
        ms = [e for e in evals if e.mechanism == mech and not e.clarified]
        if ms:
            by_mech[mech] = {
                "n": len(ms),
                "within": round(
                    sum(within(e.est_kcal, e.true_kcal, tol(e.gid)) for e in ms) / len(ms), 3
                ),
                "kcal_mape": _mape([(e.est_kcal, e.true_kcal) for e in ms]),
            }
    return {
        "n": len(evals),
        "logged": len(logged),
        "pct_within_item": (
            round(sum(within(e.est_kcal, e.true_kcal, tol(e.gid)) for e in logged) / len(logged), 3)
            if logged
            else None
        ),
        "kcal_mape": _mape([(e.est_kcal, e.true_kcal) for e in logged]),
        "abstention_recall": (
            round(sum(e.clarified for e in abst) / len(abst), 3) if abst else None
        ),
        "false_abstain_rate": round(
            sum(e.clarified and not e.expected_clarify for e in evals)
            / max(sum(not e.expected_clarify for e in evals), 1),
            3,
        ),
        "by_mechanism": by_mech,
    }
