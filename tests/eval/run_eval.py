"""CLI eval-прогона точности (NFR-1, D-15). Гоняет golden-set через реальный Assistant.

Нужен OPENAI_API_KEY (бьёт реальный OpenAI). Свежий Assistant на КАЖДЫЙ прогон —
иначе пины из прогона 1 превратят прогоны 2..N в cache-hit и убьют замер дисперсии LLM.

    python -m tests.eval.run_eval                       # один прогон + метрики + диагностика
    python -m tests.eval.run_eval --runs 5 --save-baseline   # заморозить baseline
    python -m tests.eval.run_eval --gate      # сверить с baseline (2σ); exit 1 при провале

Пейсинг между строками — под TPM-лимит (большой системный промпт × ~26 строк); 429 → backoff.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.application.results import (
    ClarificationNeeded,
    MealLogged,
    VariantClarificationNeeded,
)
from tests.eval.catalog import build_eval_assistant
from tests.eval.harness import (
    ItemEval,
    aggregate_runs,
    flaky_rows,
    gate_check,
    load_golden,
    metrics,
)

BASELINE = Path(__file__).resolve().parent / "baselines" / "baseline.json"
ROW_PACING_S = 4.0  # под TPM gpt-4.1 (~2.5k токенов/вызов); 429 добирает backoff


async def _process_with_retry(assistant, user, row, attempts: int = 6):
    """Прогон одной строки с backoff на 429 (TPM-лимит). ponytail: линейный backoff."""
    from openai import RateLimitError

    text, itype = row["input"], row.get("input_type", "text")
    for i in range(attempts):
        try:
            return await assistant.process(user, text, input_type=itype)
        except RateLimitError:
            if i == attempts - 1:
                raise
            await asyncio.sleep(6 * (i + 1))


async def _run_once() -> dict:
    """Один полный прогон golden-set через свежий Assistant → metrics()."""
    assistant, user = await build_eval_assistant()
    rows = load_golden()
    evals: list[ItemEval] = []
    tol_by_id: dict[str, float] = {}
    for row in rows:
        tol_by_id[row["id"]] = row.get("tol_item_pct", 0.20)
        expect_clarify = bool(row.get("expect_clarify") or row.get("expect_abstain"))
        res = await _process_with_retry(assistant, user, row)
        await asyncio.sleep(ROW_PACING_S)
        clarified = isinstance(res, (ClarificationNeeded, VariantClarificationNeeded))
        est = res.meal.totals.kcal if isinstance(res, MealLogged) else 0.0
        evals.append(
            ItemEval(
                gid=row["id"],
                mechanism=row.get("mechanism", "other"),
                true_kcal=float(row["expected"]["kcal"]),
                est_kcal=float(est),
                clarified=clarified,
                expected_clarify=expect_clarify,
            )
        )
    return metrics(evals, tol_by_id)


def _diagnostics(m: dict) -> None:
    misses = [r for r in m["rows"] if r["within"] is False]
    false_ab = [r for r in m["rows"] if r["clarified"] and not r["expected_clarify"]]
    if misses:
        print("\nMISSES (вне допуска):")
        for r in misses:
            print(f"  {r['id']:5} {r['mechanism']:16} est={r['est']:.0f} true={r['true']:.0f}")
    if false_ab:
        print("\nFALSE_ABSTAIN (спросил зря):", ", ".join(r["id"] for r in false_ab))


def _summary(m: dict) -> dict:
    return {k: m[k] for k in ("n", "logged", "pct_within_item", "kcal_mape",
                              "abstention_recall", "false_abstain_rate", "by_mechanism")}


def _arg(name: str, default: str | None = None) -> str | None:
    a = sys.argv[1:]
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default


def main() -> int:
    runs = int(_arg("--runs", "1"))
    per_run = [asyncio.run(_run_once()) for _ in range(runs)]
    last = per_run[-1]

    print(json.dumps(_summary(last), ensure_ascii=False, indent=2))
    _diagnostics(last)
    if runs > 1:
        agg = aggregate_runs(per_run)
        print("\nAGGREGATE (mean±std по", runs, "прогонам):")
        print(json.dumps(agg, ensure_ascii=False, indent=2))
        print("FLAKY (вердикт менялся между прогонами):", flaky_rows(per_run) or "—")

    if "--save-baseline" in sys.argv:
        agg = aggregate_runs(per_run)
        agg["created"] = datetime.now(UTC).isoformat()
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✅ baseline сохранён: {BASELINE}")

    if "--gate" in sys.argv:
        if not BASELINE.exists():
            print("\n⚠️ baseline отсутствует — сперва `--runs 5 --save-baseline`. Гейт пропущен.")
            return 0
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        ok, fails = gate_check(last, baseline)
        print(f"\nGATE (baseline ±2σ): {'PASS' if ok else 'FAIL'}")
        for f in fails:
            print("  ✗", f)
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
