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
    # Воздержался: переспросил (порция/вариант) ИЛИ отказал («это не еда»). Обе формы значат
    # «числа не назвал», поэтому ккал не сравниваем. На мусорном вводе отказ ЛУЧШЕ вопроса, но
    # для метрики они равны: важно, что бот не записал выдуманное.
    clarified: bool
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


# Чего стоит лишний вопрос против верного ответа. Не 0: спросить про тофу лучше, чем уверенно
# занизить его вдвое — неверная плотность пиннится НАВСЕГДА (INV-2). Но и не 1: лишний вопрос —
# трение, из-за которого приём не запишут, а это единственный разрушительный провал (D-28).
FALSE_ABSTAIN_CREDIT = 0.5


def row_score(e: ItemEval, tol: float) -> float:
    """Оценка ОДНОЙ строки. Знаменатель фиксирован: по одной строке на каждую запись golden."""
    if e.clarified:
        return 1.0 if e.expected_clarify else FALSE_ABSTAIN_CREDIT
    hit = float(within(e.est_kcal, e.true_kcal, tol))
    # Ответил там, где ждали вопрос: рискнул. Верное число — половина, неверное — ноль.
    return hit * FALSE_ABSTAIN_CREDIT if e.expected_clarify else hit


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
        # Знаменатель ФИКСИРОВАН (= n): только по нему можно сравнивать модели с разной
        # склонностью переспрашивать. Остальные метрики ниже делятся на logged и потому
        # завышаются у модели, которая чаще воздерживается — сравнивать их между моделями нельзя.
        "quality_score": (
            round(sum(row_score(e, tol(e.gid)) for e in evals) / len(evals), 3) if evals else None
        ),
        "coverage": round(len(logged) / len(evals), 3) if evals else None,
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
    "quality_score": True,  # главная: единственная с фиксированным знаменателем
    "coverage": True,  # доля записанного; без неё рост точности за счёт отказов не видно
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
        "quality_score": m.get("quality_score") or 0.0,
        "coverage": m.get("coverage") or 0.0,
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
            # Раньше здесь стоял молчаливый continue: метрика, которой нет в замороженном
            # baseline, не гейтилась ВООБЩЕ и никак себя не проявляла. Теперь это видно.
            fails.append(f"{k}: нет в baseline — перезаморозь (--runs 5 --save-baseline)")
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


# --- деньги и время прогона (NFR-6: сравнение провайдеров) ---------------------------------


class BudgetExceeded(BaseException):
    """Потолок трат прогона достигнут. BaseException, а не Exception: цикл повторов разбора
    ретраит любое Exception — потолок он ретраить не должен, иначе «жёсткий» потолок
    превратится в три лишних вызова."""


class EvalMeter:
    """Счётчик трат прогона по прод-ценам (`app.domain.spend`) с жёстким потолком.

    Тот же интерфейс `note`, что у `SpendMeter`, — адаптеры не знают, что они в eval. Потолок
    проверяется ДО вызова (`check`): начатый вызов доплачивается, новый не начинается."""

    def __init__(self, max_usd: float) -> None:
        import logging
        from collections import Counter

        self.max_usd = max_usd
        self.spent_usd = 0.0
        self.calls = 0
        self.latencies: list[float] = []
        # Отказы (с категорией) и обрезки Claude адаптер превращает в «не понял» — для метрик
        # точности это верно, но сравнивать модели надо и по ним отдельно. Ловим по метке в
        # записи лога `claude_compat.stopped_early`, прод-код ради eval не меняется.
        self.stopped_early: Counter[str] = Counter()
        meter = self

        class _Stops(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                tag = getattr(record, "stop_early", None)
                if tag:
                    meter.stopped_early[tag] += 1

        self._handler = _Stops()
        logging.getLogger("app.infrastructure.claude_compat").addHandler(self._handler)

    async def note(
        self, *, model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0,
        cache_write_tokens: int = 0, cache_write_5m_tokens: int = 0,
    ) -> None:
        from app.domain.spend import Usage

        self.calls += 1
        self.spent_usd += Usage(
            model, input_tokens, output_tokens, cached_tokens, cache_write_tokens,
            cache_write_5m_tokens,
        ).cost_usd

    def check(self) -> None:
        if self.spent_usd >= self.max_usd:
            raise BudgetExceeded(f"потрачено ${self.spent_usd:.2f} ≥ потолка ${self.max_usd:.2f}")

    def timed(self, obj: object, method: str) -> None:
        """Обернуть один вызов модели адаптера: потолок до, задержка после.

        Имена общие у провайдеров (`_parse_once` разбора, `_parse` фото) — адаптеры Claude
        наследуют транспортный метод OpenAI и подменяют только его (D-41)."""
        import time

        orig = getattr(obj, method)

        async def wrapper(*args, **kwargs):
            self.check()
            t0 = time.perf_counter()
            try:
                return await orig(*args, **kwargs)
            finally:
                self.latencies.append(time.perf_counter() - t0)

        setattr(obj, method, wrapper)

    def summary(self, records: int) -> dict:
        import logging

        logging.getLogger("app.infrastructure.claude_compat").removeHandler(self._handler)
        lat = sorted(self.latencies)
        return {
            "stopped_early": dict(self.stopped_early),   # refusal:<категория> | max_tokens
            "usd_total": round(self.spent_usd, 4),
            "usd_per_record": round(self.spent_usd / records, 5) if records else None,
            "calls": self.calls,
            "latency_s": {"n": len(lat), "p50": _pct(lat, 0.5), "p90": _pct(lat, 0.9)},
        }
