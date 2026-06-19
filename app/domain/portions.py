"""Разрешение веса порции в граммы.

Самый большой рычаг калорийности и самый неопределённый вход. Приоритет:
явный вес → счёт штук → курируемый размер порции → порция × множитель размера
(база — типичная порция от LLM) → явные граммы → дефолт. `assumed` = вес угадан.

При unit=portion размер (S/M/L) масштабирует базу: «овсянка» без количества →
вопрос размера → типичная_порция × множитель (FR-21/D-7).
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PIECE_WEIGHT_G = 100.0
DEFAULT_PORTION_G = 300.0
# Множители к дефолтной порции, когда у продукта нет своих пресетов S/M/L.
PORTION_FALLBACK_FACTOR = {"small": 0.7, "medium": 1.0, "large": 1.4}


@dataclass(frozen=True, slots=True)
class GramsResolution:
    grams: float
    assumed: bool


def resolve_grams(
    *,
    unit: str,
    quantity: float | None,
    grams_estimate: float | None,
    portion_hint: str | None,
    parsed_unit_weight_g: float | None,
    product_unit_weight_g: float | None,
    product_default_portion_g: float | None,
    product_portion_sizes: dict[str, float] | None,
    parsed_portion_sizes: dict[str, float] | None = None,
) -> GramsResolution:
    # 1. Явный вес/объём ("250 грамм", "200 мл" ~ 200 г для MVP).
    if unit in ("g", "ml") and grams_estimate is not None:
        return GramsResolution(grams=grams_estimate, assumed=False)

    # 2. Счёт штук ("два банана").
    if unit == "piece" and quantity is not None:
        piece = product_unit_weight_g or parsed_unit_weight_g
        if piece is not None:
            return GramsResolution(grams=quantity * piece, assumed=False)
        if grams_estimate is not None:
            # нет веса штуки, но LLM оценил суммарные граммы — доверяем оценке, не дефолту 100
            return GramsResolution(grams=grams_estimate, assumed=True)
        return GramsResolution(grams=quantity * DEFAULT_PIECE_WEIGHT_G, assumed=True)

    # 3. Порция с курируемым размером (для блюд — самый точный путь).
    if (
        unit == "portion"
        and portion_hint is not None
        and product_portion_sizes
        and portion_hint in product_portion_sizes
    ):
        return GramsResolution(grams=product_portion_sizes[portion_hint], assumed=False)

    # 3.5 Порция с LLM-размерами под конкретное блюдо (реальные граммы S/M/L, не множитель).
    #     Нейронка предлагает размеры пер-блюдо (круассан 80/120/160 ≠ суп) — точнее множителя.
    if (
        unit == "portion"
        and portion_hint is not None
        and parsed_portion_sizes
        and portion_hint in parsed_portion_sizes
    ):
        return GramsResolution(grams=parsed_portion_sizes[portion_hint], assumed=True)

    # 4. Порция с размером, без пресетов (курируемых и LLM): масштабируем базу множителем.
    #    Запасной путь — база (дефолт продукта/grams) × фактор. ponytail: грубо, если LLM не дал размеры.
    if unit == "portion" and portion_hint is not None:
        base = product_default_portion_g or grams_estimate or DEFAULT_PORTION_G
        factor = PORTION_FALLBACK_FACTOR.get(portion_hint, 1.0)
        return GramsResolution(grams=base * factor, assumed=True)

    # 5. Явные граммы (не порция) — страховка для unit=unknown с числом.
    if grams_estimate is not None:
        return GramsResolution(grams=grams_estimate, assumed=False)

    # 6. Последний дефолт.
    base = product_default_portion_g or DEFAULT_PORTION_G
    return GramsResolution(grams=base, assumed=True)
