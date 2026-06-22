"""Офлайн eval-харнесс точности (NFR-1, D-15).

Чистые функции метрик отделены от LLM-прогона и юнит-тестируются без сети
(`tests/unit/test_eval_harness.py`). Живой прогон по реальному OpenAI — в
`run_eval.py` (`make eval`); он гоняет golden-set через настоящий `Assistant`,
а НЕ через `nlu.parse` напрямую — баги рождаются в КОМПОЗИЦИИ parse × resolve_grams
× gate × pin, а не только в извлечении.
"""
from __future__ import annotations

import json
import random
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


def daily_sum_within(
    logged: list[ItemEval], *, tol: float = 0.15, days: int = 300, day_size: int = 5, seed: int = 0
) -> float | None:
    """НАСТОЯЩИЙ NFR-1: доля синтетических «дней» (сумма day_size позиций), где сумма ккал в ±tol.

    Бутстрап со seed → детерминирован. Per-item ошибки в дневной сумме частично гасятся
    (разнознаковые) → дневная точность обычно ВЫШЕ per-item. None, если логов меньше day_size.
    """
    pool = [(e.est_kcal, e.true_kcal) for e in logged if e.true_kcal]
    if len(pool) < day_size:
        return None
    rng = random.Random(seed)
    hits = 0
    for _ in range(days):
        sample = rng.sample(pool, day_size)
        est, true = sum(e for e, _ in sample), sum(t for _, t in sample)
        if true and abs(est - true) / true <= tol:
            hits += 1
    return round(hits / days, 3)


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
        "daily_sum_within_15": daily_sum_within(logged),  # настоящий NFR-1 (дневная сумма ±15%)
        "abstention_recall": (
            round(sum(e.clarified for e in abst) / len(abst), 3) if abst else None
        ),
        "false_abstain_rate": round(
            sum(e.clarified and not e.expected_clarify for e in evals)
            / max(sum(not e.expected_clarify for e in evals), 1),
            3,
        ),
        "by_mechanism": by_mech,
        "rows": [
            {
                "id": e.gid,
                "mechanism": e.mechanism,
                "clarified": e.clarified,
                "within": None if e.clarified else within(e.est_kcal, e.true_kcal, tol(e.gid)),
                "est": e.est_kcal,
                "true": e.true_kcal,
                "expected_clarify": e.expected_clarify,
            }
            for e in evals
        ],
    }


# --- baseline как распределение + регрессионный гейт (NFR-1, D-15) ---

# Плоские числовые метрики для baseline/gate; True = «больше — лучше».
_GATED = {
    "pct_within_item": True,
    "daily_sum_within_15": True,
    "abstention_recall": True,
    "false_abstain_rate": False,
    "kcal_mape_median": False,
    "kcal_mape_p90": False,
}


def scalars(m: dict) -> dict[str, float]:
    """Плоский срез метрик прогона для сравнения с baseline."""
    km = m.get("kcal_mape") or {}
    return {
        "pct_within_item": m.get("pct_within_item") or 0.0,
        "daily_sum_within_15": m.get("daily_sum_within_15") or 0.0,
        "abstention_recall": m.get("abstention_recall") or 0.0,
        "false_abstain_rate": m.get("false_abstain_rate") or 0.0,
        "kcal_mape_median": km.get("median") or 0.0,
        "kcal_mape_p90": km.get("p90") or 0.0,
    }


def aggregate_runs(run_metrics: list[dict]) -> dict:
    """Baseline = распределение по N прогонам: mean±std каждой метрики (отделяем шум LLM)."""
    cols = [scalars(m) for m in run_metrics]
    agg = {}
    for k in _GATED:
        xs = [c[k] for c in cols]
        agg[k] = {
            "mean": round(statistics.mean(xs), 4),
            "std": round(statistics.pstdev(xs), 4) if len(xs) > 1 else 0.0,
        }
    return {"runs": len(run_metrics), "metrics": agg}


def gate_check(current: dict, baseline: dict, abs_floor: float = 0.03) -> tuple[bool, list[str]]:
    """Регрессия засчитывается, только если метрика хуже baseline сверх max(abs_floor, 2σ) —
    т.е. перекрывает естественный разброс модели (а не шумит). Возвращает (ok, список провалов).
    """
    cur = scalars(current)
    base = baseline.get("metrics", {})
    fails: list[str] = []
    for k, higher_better in _GATED.items():
        b = base.get(k)
        if not b:
            continue
        margin = max(abs_floor, 2 * b["std"])
        if higher_better and cur[k] < b["mean"] - margin:
            fails.append(f"{k}: {cur[k]:.3f} < {b['mean']:.3f}−{margin:.3f}")
        elif not higher_better and cur[k] > b["mean"] + margin:
            fails.append(f"{k}: {cur[k]:.3f} > {b['mean']:.3f}+{margin:.3f}")
    return (not fails, fails)


def flaky_rows(run_metrics: list[dict]) -> list[str]:
    """id строк, чей вердикт within менялся между прогонами — кандидаты на воздержание (D-15)."""
    by_id: dict[str, set] = {}
    for m in run_metrics:
        for r in m.get("rows", []):
            by_id.setdefault(r["id"], set()).add(r["within"])
    return sorted(i for i, verdicts in by_id.items() if len(verdicts) > 1)
