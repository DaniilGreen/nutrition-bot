"""CLI eval-прогона точности (NFR-1, D-15). Гоняет golden-set через реальный Assistant.

Бьёт реальную модель — деньги. Провайдер и модель — флагами (NFR-6, D-41), потолок трат —
жёсткий: при `--max-usd` новый вызов не начинается. Свежий Assistant на КАЖДЫЙ прогон —
иначе пины из прогона 1 превратят прогоны 2..N в cache-hit и убьют замер дисперсии LLM.

    python -m tests.eval.run_eval                       # один прогон + метрики + диагностика
    python -m tests.eval.run_eval --provider anthropic --model claude-sonnet-5-5 --max-usd 1
    python -m tests.eval.run_eval --runs 5 --save-baseline   # заморозить baseline
    python -m tests.eval.run_eval --gate      # сверить с baseline (2σ); exit 1 при провале

Кроме точности печатает задержку одного вызова модели (p50/p90) и $ на строку golden-set.
Пейсинг между строками — под TPM-лимит (большой системный промпт × ~68 строк); 429/529 → backoff.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from app.application.results import (
    ClarificationNeeded,
    MealLogged,
    Unknown,
    VariantClarificationNeeded,
)
from app.infrastructure.llm_errors import overloaded
from tests.eval.catalog import build_eval_assistant, eval_settings
from tests.eval.harness import (
    BudgetExceeded,
    EvalMeter,
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
    """Прогон одной строки с backoff на 429/529 (TPM, перегрузка). ponytail: линейный backoff."""
    text, itype = row["input"], row.get("input_type", "text")
    for i in range(attempts):
        try:
            return await assistant.process(user, text, input_type=itype)
        except Exception as exc:
            if not overloaded(exc) or i == attempts - 1:
                raise
            await asyncio.sleep(6 * (i + 1))


async def _run_once(settings, meter: EvalMeter) -> dict:
    """Один полный прогон golden-set через свежий Assistant → metrics() + деньги и время."""
    assistant, user = await build_eval_assistant(settings, meter)
    rows = load_golden()
    evals: list[ItemEval] = []
    tol_by_id: dict[str, float] = {}
    for row in rows:
        meter.check()  # потолок — до строки, а не после прогона
        tol_by_id[row["id"]] = row.get("tol_item_pct", 0.20)
        expect_clarify = bool(row.get("expect_clarify") or row.get("expect_abstain"))
        res = await _process_with_retry(assistant, user, row)
        await asyncio.sleep(ROW_PACING_S)
        # Воздержание — это ЛЮБОЙ отказ записать число, а не только вопрос. Отказ «это не еда»
        # (Unknown) на мусорном вводе — лучший ответ, чем вопрос: спрашивать про «Пизденку»
        # нечего. Пока Unknown сюда не входил, фикс не-еды выглядел падением abstention_recall.
        clarified = isinstance(res, (ClarificationNeeded, VariantClarificationNeeded, Unknown))
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
    m = metrics(evals, tol_by_id)
    m["cost"] = meter.summary(len(evals))
    return m


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
    # quality_score и coverage — первыми: только у них знаменатель не зависит от поведения
    # модели, остальные метрики между моделями сравнивать нельзя (см. harness.metrics).
    return {k: m[k] for k in ("n", "logged", "quality_score", "coverage",
                              "pct_within_item", "daily_sum_within_15", "kcal_mape",
                              "abstention_recall", "false_abstain_rate", "by_mechanism",
                              "cost", "provider", "model")}


def _args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="eval точности разбора (NFR-1)")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--provider", choices=("openai", "anthropic"), default=None,
                    help="по умолчанию — LLM_PROVIDER из окружения")
    ap.add_argument("--model", default="", help="модель разбора; пусто — умолчание провайдера")
    ap.add_argument("--effort", default="", help="Claude: low|medium|high|xhigh|max")
    ap.add_argument("--thinking-off", action="store_true",
                    help="Claude: выключить рассуждения, где семейство позволяет")
    ap.add_argument("--fast", action="store_true", help="Claude: быстрый режим Opus (D-42)")
    ap.add_argument("--max-usd", type=float, default=2.0,
                    help="жёсткий потолок трат на ВСЕ прогоны вместе (по прод-ценам)")
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    ap.add_argument("--save-baseline", action="store_true")
    ap.add_argument("--gate", action="store_true")
    return ap.parse_args()


def main() -> int:
    from app.config import get_settings

    args = _args()
    settings = eval_settings(
        args.provider or get_settings().llm_provider, nlu_model=args.model,
        effort=args.effort, thinking_off=args.thinking_off, fast=args.fast,
    )
    per_run, spent = [], 0.0
    for _ in range(args.runs):
        # Потолок — на весь вызов команды: каждому прогону — остаток.
        meter = EvalMeter(args.max_usd - spent)
        try:
            m = asyncio.run(_run_once(settings, meter))
        except BudgetExceeded as exc:
            print(f"\n⛔ {exc} — прогон остановлен, неполные метрики не печатаем")
            return 2
        spent += meter.spent_usd
        m["provider"], m["model"] = settings.llm_provider, settings.model_for("nlu")
        per_run.append(m)
    last = per_run[-1]

    print(json.dumps(_summary(last), ensure_ascii=False, indent=2))
    _diagnostics(last)
    if args.runs > 1:
        agg = aggregate_runs(per_run)
        print("\nAGGREGATE (mean±std по", args.runs, "прогонам):")
        print(json.dumps(agg, ensure_ascii=False, indent=2))
        print("FLAKY (вердикт менялся между прогонами):", flaky_rows(per_run) or "—")

    if args.save_baseline:
        agg = aggregate_runs(per_run)
        agg["created"] = datetime.now(UTC).isoformat()
        agg["provider"], agg["model"] = last["provider"], last["model"]
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✅ baseline сохранён: {args.baseline}")

    if args.gate:
        if not args.baseline.exists():
            print("\n⚠️ baseline отсутствует — сперва `--runs 5 --save-baseline`. Гейт пропущен.")
            return 0
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        ok, fails = gate_check(last, baseline)
        print(f"\nGATE (baseline ±2σ): {'PASS' if ok else 'FAIL'}")
        for f in fails:
            print("  ✗", f)
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
