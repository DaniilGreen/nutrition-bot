"""CLI eval-прогона (NFR-1). Гоняет golden-set через реальный Assistant и печатает метрики.

Использование (нужен OPENAI_API_KEY):
    python -m tests.eval.run_eval          # печать метрик
    python -m tests.eval.run_eval --gate   # + примитивный порог (exit 1 при провале)

Примитивный гейт (pct_within_item ≥ 0.6) — старт. Полноценный baseline±2σ из 5 прогонов
(`tests/eval/baselines/`) — следующий шаг (D-15, см. docs/REVIEW.md §D.3).
"""
from __future__ import annotations

import asyncio
import json
import sys

from app.application.results import (
    ClarificationNeeded,
    MealLogged,
    VariantClarificationNeeded,
)
from tests.eval.catalog import build_eval_assistant
from tests.eval.harness import ItemEval, load_golden, metrics

GATE_MIN_WITHIN = 0.6


async def _process_with_retry(assistant, user, row, attempts: int = 6):
    """Прогон одной строки с backoff на 429 (TPM-лимит OpenAI) — иначе серия из ~30 больших
    промптов подряд упирается в лимит токенов/мин. ponytail: линейный backoff, без джиттера."""
    from openai import RateLimitError

    text, itype = row["input"], row.get("input_type", "text")
    for i in range(attempts):
        try:
            return await assistant.process(user, text, input_type=itype)
        except RateLimitError:
            if i == attempts - 1:
                raise
            await asyncio.sleep(6 * (i + 1))


async def _run() -> dict:
    assistant, user = await build_eval_assistant()
    rows = load_golden()
    evals: list[ItemEval] = []
    tol_by_id: dict[str, float] = {}
    for row in rows:
        tol_by_id[row["id"]] = row.get("tol_item_pct", 0.20)
        expect_clarify = bool(row.get("expect_clarify") or row.get("expect_abstain"))
        res = await _process_with_retry(assistant, user, row)
        await asyncio.sleep(1.0)  # пейсинг под TPM (большой системный промпт × ~26 строк)
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


def main() -> int:
    m = asyncio.run(_run())
    print(json.dumps(m, ensure_ascii=False, indent=2))
    if "--gate" in sys.argv:
        ok = (m["pct_within_item"] or 0.0) >= GATE_MIN_WITHIN
        print(f"\nGATE ({GATE_MIN_WITHIN:.0%} within-item): {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
