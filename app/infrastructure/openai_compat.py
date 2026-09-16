"""Совместимость вызовов OpenAI между поколениями моделей.

Модели gpt-5.x принимают ТОЛЬКО temperature по умолчанию (=1) и отвергают явное значение
ошибкой 400 «Only the default (1) value is supported». Наш код передаёт temperature в двух
местах — извлечение (NLU) и чтение этикеток (Vision), — поэтому смена модели в конфиге без
этой развилки кладёт бота на КАЖДОМ сообщении.

Отдельно опасно то, что тесты этого не поймают: они гоняются на in-memory фейках и до реального
API не доходят. Поэтому решение вынесено в чистую функцию — её проверяют без сети.
"""
from __future__ import annotations

from openai import Omit, omit

# ponytail: разделяем по префиксу имени модели. Потолок — список дописывать руками, когда
# появится новое семейство. Точнее было бы один раз попробовать вызов и запомнить отказ, но это
# лишний сетевой поход на старте ради случая, который меняется раз в год.
_NO_CUSTOM_TEMPERATURE = ("gpt-5",)


def supports_temperature(model: str) -> bool:
    """Принимает ли модель явное значение temperature."""
    return not model.startswith(_NO_CUSTOM_TEMPERATURE)


def temperature_for(model: str, temperature: float) -> float | Omit:
    """Значение temperature для вызова API — либо само число, либо `omit` (параметр не шлётся).

    `omit` — штатный сентинел SDK: он выкидывает поле из запроса, а не шлёт null.
    """
    return temperature if supports_temperature(model) else omit


def token_counts(usage: object) -> tuple[int, int, int]:
    """(вход, из них из кэша, выход) из объекта usage любого вызова.

    Одно место на все адаптеры: кэшированные токены лежат во вложенном
    `prompt_tokens_details`, которого у части ответов нет вовсе. Пропустить его
    значит считать кэш по полной цене — так счётчик и завышал разбор до 16.09.2026.
    """
    def num(obj: object, name: str) -> int:
        try:
            return int(getattr(obj, name, 0) or 0)
        except (TypeError, ValueError):
            return 0

    details = getattr(usage, "prompt_tokens_details", None)
    return (
        num(usage, "prompt_tokens"),
        num(details, "cached_tokens") if details is not None else 0,
        num(usage, "completion_tokens"),
    )
