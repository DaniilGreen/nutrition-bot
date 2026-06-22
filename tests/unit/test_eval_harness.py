"""Юнит-тест чистого ядра eval-харнесса (без LLM): метрики + парсинг golden-set."""
from __future__ import annotations

from tests.eval.harness import (
    ItemEval,
    aggregate_runs,
    flaky_rows,
    gate_check,
    load_golden,
    metrics,
    within,
)


def test_within_tolerance() -> None:
    assert within(110, 100, 0.15) is True
    assert within(86, 100, 0.15) is True
    assert within(120, 100, 0.15) is False
    assert within(0, 0, 0.0) is True  # вода: истина 0 → точное совпадение
    assert within(150, 0, 0.2) is False  # «кола 0» → 150 ккал: провал


def test_metrics_basic() -> None:
    evals = [
        ItemEval("g1", "piece_weight", 100, 110, clarified=False, expected_clarify=False),
        ItemEval("g2", "stated_totals", 200, 400, clarified=False, expected_clarify=False),
        ItemEval("g3", "portion_clarify", 0, 0, clarified=True, expected_clarify=True),
    ]
    m = metrics(evals, {"g1": 0.2, "g2": 0.2})
    assert m["logged"] == 2  # g3 воздержался — не logged
    assert m["pct_within_item"] == 0.5  # g1 within, g2 — нет (+100%)
    assert m["abstention_recall"] == 1.0  # ждали clarify у g3 — получили
    assert m["false_abstain_rate"] == 0.0  # лишних переспросов нет
    assert m["by_mechanism"]["piece_weight"]["within"] == 1.0
    assert m["by_mechanism"]["stated_totals"]["within"] == 0.0


def test_false_abstain_counted() -> None:
    # бот переспросил там, где ждали ответ → штраф false_abstain_rate
    evals = [
        ItemEval("g1", "density_estimate", 100, 0, clarified=True, expected_clarify=False),
        ItemEval("g2", "density_estimate", 100, 100, clarified=False, expected_clarify=False),
    ]
    m = metrics(evals, {})
    assert m["false_abstain_rate"] == 0.5


def _run(within_frac: float) -> dict:
    """Синтетический metrics()-прогон с заданной долей попаданий (для baseline/gate-тестов)."""
    n = 10
    hits = round(within_frac * n)
    evals = [
        ItemEval(f"g{i}", "density_estimate", 100, 100 if i < hits else 200,
                 clarified=False, expected_clarify=False)
        for i in range(n)
    ]
    return metrics(evals, {})


def test_aggregate_runs_mean_std() -> None:
    agg = aggregate_runs([_run(0.9), _run(0.9), _run(0.9)])
    assert agg["runs"] == 3
    assert agg["metrics"]["pct_within_item"]["mean"] == 0.9
    assert agg["metrics"]["pct_within_item"]["std"] == 0.0  # стабильно → нулевой разброс


def test_gate_passes_within_2sigma_and_fails_beyond() -> None:
    baseline = aggregate_runs([_run(0.9), _run(0.8), _run(0.9)])  # mean 0.867, есть разброс
    assert gate_check(_run(0.9), baseline)[0] is True  # лучше baseline — PASS
    ok, fails = gate_check(_run(0.4), baseline)  # резкое падение — FAIL
    assert ok is False and any("pct_within_item" in f for f in fails)


def test_gate_no_false_alarm_on_stable_baseline() -> None:
    # стабильный baseline (std=0) → floor 0.03 защищает от ложной тревоги на мелком шуме
    baseline = aggregate_runs([_run(1.0), _run(1.0)])
    assert gate_check(_run(1.0), baseline)[0] is True


def test_flaky_detects_changed_verdict() -> None:
    r1 = metrics([ItemEval("g1", "m", 100, 100, False, False)], {})  # within
    r2 = metrics([ItemEval("g1", "m", 100, 200, False, False)], {})  # not within
    assert flaky_rows([r1, r2]) == ["g1"]
    assert flaky_rows([r1, r1]) == []  # стабильный — не flaky


def test_golden_set_parses_and_well_formed() -> None:
    rows = load_golden()
    assert len(rows) >= 20  # regression-core набран
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))  # id уникальны
    for r in rows:
        assert r["input"] and "mechanism" in r and "expected" in r
        assert "kcal" in r["expected"]
        if r.get("expect_clarify") and r["expected"]["kcal"] == 0:
            assert r["items"] == []  # чистое воздержание (kcal 0) → нечего сверять
        # гибрид (expect_clarify + items): ждём вопрос, но если бот залогирует — сверим по ккал
