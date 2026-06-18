from __future__ import annotations

from app.infrastructure.products.seed import CURATED, validate_curated


def test_curated_products_are_sane() -> None:
    validate_curated()  # assert-based: дубли ключей, отрицательные/несходящиеся макросы
    assert len(CURATED) >= 20
