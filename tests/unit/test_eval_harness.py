"""Юнит-тест чистого ядра eval-харнесса (без LLM): метрики + парсинг golden-set."""
from __future__ import annotations

from tests.eval.harness import ItemEval, load_golden, metrics, within


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


def test_golden_set_parses_and_well_formed() -> None:
    rows = load_golden()
    assert len(rows) >= 20  # regression-core набран
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))  # id уникальны
    for r in rows:
        assert r["input"] and "mechanism" in r and "expected" in r
        assert "kcal" in r["expected"]
        if r.get("expect_clarify"):
            assert r["items"] == []  # воздержание → нечего сверять по ккал
