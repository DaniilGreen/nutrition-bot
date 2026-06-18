from __future__ import annotations

from typing import Any

from app.domain.portions import (
    DEFAULT_PIECE_WEIGHT_G,
    DEFAULT_PORTION_G,
    resolve_grams,
)


def _call(**kw: Any):
    base: dict[str, Any] = dict(
        unit="unknown",
        quantity=None,
        grams_estimate=None,
        portion_hint=None,
        parsed_unit_weight_g=None,
        product_unit_weight_g=None,
        product_default_portion_g=None,
        product_portion_sizes=None,
    )
    base.update(kw)
    return resolve_grams(**base)


def test_explicit_grams() -> None:
    r = _call(unit="g", grams_estimate=250)
    assert r.grams == 250
    assert r.assumed is False


def test_pieces_with_product_weight() -> None:
    r = _call(unit="piece", quantity=2, product_unit_weight_g=120)
    assert r.grams == 240
    assert r.assumed is False


def test_pieces_default_weight_assumed() -> None:
    r = _call(unit="piece", quantity=3)
    assert r.grams == 3 * DEFAULT_PIECE_WEIGHT_G
    assert r.assumed is True


def test_portion_preset() -> None:
    r = _call(
        unit="portion",
        portion_hint="medium",
        product_portion_sizes={"small": 300, "medium": 450, "large": 600},
    )
    assert r.grams == 450
    assert r.assumed is False


def test_portion_fallback_assumed() -> None:
    r = _call(unit="portion", portion_hint="large", product_default_portion_g=400)
    assert r.grams == 400 * 1.4
    assert r.assumed is True


def test_final_default() -> None:
    r = _call()
    assert r.grams == DEFAULT_PORTION_G
    assert r.assumed is True


def test_explicit_grams_beats_portion_fallback() -> None:
    # «30 грамм одна порция», у продукта нет курируемых размеров → 30 г, не дефолт 300
    r = _call(unit="portion", portion_hint="medium", grams_estimate=30)
    assert r.grams == 30
    assert r.assumed is False


def test_curated_portion_beats_explicit_grams() -> None:
    # У блюда есть курируемый размер «большая» → он точнее оценки граммов LLM
    r = _call(
        unit="portion",
        portion_hint="large",
        grams_estimate=700,
        product_portion_sizes={"large": 650},
    )
    assert r.grams == 650
