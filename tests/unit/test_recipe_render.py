"""Рецепт (FR-39.1) обязан ДОЕХАТЬ до человека в боте и в аппке.

Регрессия 15.10.2026: продукт заводился, а оба рендера проваливались в «не понял» —
ни у бота, ни у API не было ветки под этот результат. Человек решил бы, что не вышло,
и повторил бы ввод поверх уже сохранённого.
"""
from __future__ import annotations

from app.application.entities import Product, User
from app.application.results import DishSaved
from app.bot.formatters import format_result
from app.domain.value_objects import Density
from app.web.routes import _result_json


def _recipe(unit_weight: float | None = None) -> DishSaved:
    return DishSaved(
        product=Product(
            canonical_key="начинка", display_name="начинка",
            density=Density(256, 25.3, 16.2, 0.8), unit_weight_g=unit_weight,
            source="manual", is_estimate=False, id=7,
        )
    )


def test_bot_answers_a_recipe_instead_of_failing() -> None:
    text = format_result(_recipe())

    assert "Не смог обработать" not in text
    assert "Рецепт" in text and "начинка" in text
    assert "256" in text  # плотность видна сразу — её человек и пришёл проверить


def test_bot_recipe_with_portion_says_how_to_log_it() -> None:
    assert "Порция 219 г" in format_result(_recipe(unit_weight=219))


def test_api_returns_recipe_kind_not_unknown() -> None:
    user = User(telegram_id=1, name="t", locale="ru", timezone="UTC", id=1)

    out = _result_json(_recipe(), user, None, raw_input="рецепт начинка: …")  # type: ignore[arg-type]

    assert out["kind"] == "recipe"
    assert out["name"] == "начинка"
    assert out["kcal_per_100g"] == 256
