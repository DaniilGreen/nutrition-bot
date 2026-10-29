"""Совместимость вызовов Claude между семействами моделей (AI-PRINCIPLES §3, §9; D-41).

Брат `openai_compat`: всё, что у Claude зависит от модели или провайдера, живёт здесь, а не
условиями по адаптерам. Смена модели в конфиге не должна требовать правок в трёх файлах.

Что держит этот модуль (источник — skill claude-api, 29.10.2026):
- параметров сэмплинга (`temperature`, `top_p`, `top_k`) нет вовсе: с версии API 2026-10-21 —
  400 на любой модели. Префилла нет. JSON — только `output_config.format`;
- рассуждения: у Opus 5.5 не выключаются (глубина — `effort`), у Sonnet 5.5 выключаются
  `between_tools`, у Haiku 5.5 — `disabled`, оба только при effort ≤ high;
- кэш явный: метка `cache_control` на стабильном префиксе, TTL — по маршруту (D-41);
- `usage.input_tokens` — только некэшированный остаток (полный промпт — сумма трёх полей);
- пустой баланс — 402 `billing_error` или 400 «credit balance is too low», а не 429.

Тесты гоняются на фейках и до API не доходят — поэтому правила вынесены в чистые функции,
которые проверяются без сети.
"""
from __future__ import annotations

import base64
import logging
from functools import cache
from typing import Any, TypeVar

from pydantic import BaseModel

# SDK `anthropic` импортируется лениво, внутри функций: путь OpenAI (по умолчанию) не должен
# тянуть второй SDK в память процесса ради функций, которые он не вызывает.

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

EFFORTS = ("low", "medium", "high", "xhigh", "max")

# TTL — по замеру промежутков (D-41): у разбора за 30 дней 562 промежутка до 5 мин, 126 —
# 5–60 мин, 152 — больше часа; часовой выходит ≈ на 10% дешевле, хоть запись и 2× входа против
# 1.25×. Ассистенту — пять минут: его раунды идут секундами.
CACHE_1H = {"type": "ephemeral", "ttl": "1h"}
CACHE_5M = {"type": "ephemeral"}

# Как выключить рассуждения — своя форма у каждого семейства; у кого нет строки — не выключаются
# (Opus 5.5: `disabled` — 400 на любом effort). ponytail: по префиксу имени, список дописывать
# руками с новым семейством — как `_NO_CUSTOM_TEMPERATURE` у OpenAI.
_THINKING_OFF: dict[str, dict[str, str]] = {
    "claude-sonnet-5-5": {"type": "between_tools"},
    "claude-haiku-5-5": {"type": "disabled"},
}
# Выше high выключенные рассуждения — 400 у обоих семейств.
_OFF_EFFORTS = ("low", "medium", "high")
# Быстрый режим (D-42): тот же ответ ~2× быстрее за 2× цены, только Opus и только Claude API.
# Бета — заголовком и полем тела: так он идёт тем же `messages.create`/`stream`, что и всё прочее.
# ponytail: список по префиксу, как `_THINKING_OFF`; новое семейство с fast — дописать сюда.
_FAST = ("claude-opus-5-5", "claude-opus-5", "claude-opus-4-8")
FAST_BETA = "fast-mode-2026-02-01"
# ponytail: `effort` шлём всегда — он есть у всех кандидатов (5.5) и у семейств с 4.6. Модели
# старше (Haiku 4.5, Claude 3) на нём дадут 400 — их в маршруты не ставим.


def _by_prefix(table: dict[str, Any], model: str) -> Any:
    best = max((k for k in table if model.startswith(k)), key=len, default=None)
    return table[best] if best is not None else None


def request_params(
    model: str, effort: str, *, thinking_off: bool = False,
    schema: type[BaseModel] | None = None, fast: bool = False,
) -> dict[str, Any]:
    """Поля запроса, зависящие от семейства: effort, схема ответа, рассуждения, скорость.

    `thinking_off` — просьба, а не приказ: где семейство не позволяет (Opus 5.5, effort выше
    high), рассуждения остаются адаптивными, а глубину держит effort. `fast` — так же: у
    модели без быстрого режима (Sonnet, Haiku) запрос обычный.
    """
    if effort not in EFFORTS:
        raise ValueError(f"effort={effort!r}, ждём одно из {EFFORTS}")
    output: dict[str, Any] = {"effort": effort}
    if schema is not None:
        output["format"] = json_format(schema)
    params: dict[str, Any] = {"output_config": output}
    off = _by_prefix(_THINKING_OFF, model) if thinking_off and effort in _OFF_EFFORTS else None
    if off is not None:
        params["thinking"] = dict(off)
    if fast and any(model.startswith(k) for k in _FAST):
        params["extra_headers"] = {"anthropic-beta": FAST_BETA}
        params["extra_body"] = {"speed": "fast"}
    return params


def billed_model(model: str, usage: object) -> str:
    """Под каким тарифом считать вызов: быстрый режим — 2× цены (`<модель>:fast` в spend).
    Режим берём из ответа (`usage.speed`), а не из настройки: считаем то, что выставят в счёт."""
    return f"{model}:fast" if getattr(usage, "speed", None) == "fast" else model


@cache
def json_format(schema: type[BaseModel]) -> dict[str, Any]:
    """Pydantic-схема → `output_config.format`. SDK сам ставит `additionalProperties: false` и
    выносит неподдерживаемые ограничения; кэш — схема одна на маршрут и не меняется.

    Все поля — обязательные (AC-267): structured outputs выводит обязательные раньше прочих, и
    `est_note` — рассуждение, стоящее в схеме ПЕРВЫМ, — уезжал бы после чисел, а `confidence` —
    вперёд `items`. Поля и так допускают null; схема OpenAI не трогается."""
    from anthropic import transform_schema

    return {"type": "json_schema", "schema": _all_required(transform_schema(schema))}


def _all_required(node: Any) -> Any:
    """Рекурсивно: у каждого объекта `required` = все свойства в порядке схемы."""
    if isinstance(node, dict):
        out = {k: _all_required(v) for k, v in node.items()}
        if isinstance(out.get("properties"), dict):
            out["required"] = list(out["properties"])
        return out
    if isinstance(node, list):
        return [_all_required(v) for v in node]
    return node


def cached_text(text: str, cache_control: dict[str, str] = CACHE_1H) -> dict[str, Any]:
    """Текстовый блок с меткой кэша: всё ДО него (инструменты, система) кэшируется."""
    return {"type": "text", "text": text, "cache_control": dict(cache_control)}


def image_block(image: bytes) -> dict[str, Any]:
    """Картинка base64-блоком. Тип — по сигнатуре: Claude сверяет его с данными, а аппка
    присылает и PNG (у OpenAI проходило «всё jpeg»)."""
    if image.startswith(b"\x89PNG"):
        media = "image/png"
    elif image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        media = "image/webp"
    elif image.startswith(b"GIF8"):
        media = "image/gif"
    else:
        media = "image/jpeg"
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media,
                   "data": base64.standard_b64encode(image).decode()},
    }


def answer_text(message: Any) -> str:
    """Текст ответа по типу блоков, а не по позиции: ответ может начинаться с рассуждений."""
    return "".join(
        getattr(b, "text", "") for b in (getattr(message, "content", None) or [])
        if getattr(b, "type", None) == "text"
    )


def stopped_early(message: Any) -> bool:
    """Отказ модели или обрезка по max_tokens: ответ читать нельзя (AI-PRINCIPLES §9).

    Отказ — это 200, а не исключение: недописанный JSON иначе ушёл бы в pydantic и повторы."""
    reason = getattr(message, "stop_reason", None)
    if reason == "refusal":
        category = getattr(getattr(message, "stop_details", None), "category", None)
        # `stop_early` в записи лога — для eval: он считает отказы отдельно от «не понял».
        log.warning("Claude отказал: %s", category,
                    extra={"stop_early": f"refusal:{category}"})
        return True
    if reason == "max_tokens":
        log.warning("Claude упёрся в max_tokens — ответ недописан",
                    extra={"stop_early": "max_tokens"})
        return True
    return False


def read_json(message: Any, schema: type[T]) -> T | None:
    """Ответ structured outputs → объект схемы. None — отказ или обрезка: «не понял» (§4).

    Сломанный по схеме ответ — исключение, как у OpenAI: его переповторит вызывающий."""
    if stopped_early(message):
        return None
    return schema.model_validate_json(answer_text(message))


def meter_fields(usage: object) -> dict[str, int]:
    """Поля учёта (`SpendMeter.note`, `Usage`) из usage вызова Claude.

    У Claude `input_tokens` — только НЕкэшированный остаток: весь вход — сумма трёх полей.
    Взять его за весь вход значит занизить счёт в разы — ровно та дыра, что была у OpenAI до
    16.09.2026 (AI-PRINCIPLES §2). Запись делится по TTL (`cache_creation`): часовая — 2× входа,
    пятиминутная — 1.25×; разбивки нет — вся запись по часовой (ошибаемся в дорогую сторону)."""
    def num(obj: object, name: str) -> int:
        try:
            return int(getattr(obj, name, 0) or 0)
        except (TypeError, ValueError):
            return 0

    read, write = num(usage, "cache_read_input_tokens"), num(usage, "cache_creation_input_tokens")
    split = getattr(usage, "cache_creation", None)
    write_5m = min(num(split, "ephemeral_5m_input_tokens"), write) if split is not None else 0
    return {
        "input_tokens": num(usage, "input_tokens") + read + write,
        "output_tokens": num(usage, "output_tokens"),
        "cached_tokens": read,
        "cache_write_tokens": write - write_5m,
        "cache_write_5m_tokens": write_5m,
    }


def _is_anthropic(exc: BaseException) -> bool:
    """Ошибка ли это SDK Anthropic — без импорта SDK на пути OpenAI."""
    return type(exc).__module__.startswith("anthropic")


def out_of_credits(exc: BaseException) -> bool:
    """Пустой баланс Anthropic. Повтор не поможет (NFR-4): 402 `billing_error`, а на части
    аккаунтов — 400 `invalid_request_error` с «credit balance is too low»."""
    if not _is_anthropic(exc):
        return False
    import anthropic

    if not isinstance(exc, anthropic.APIStatusError):
        return False
    if exc.status_code == 402 or exc.type == "billing_error":
        return True
    return exc.status_code == 400 and "credit balance" in str(exc).lower()


def overloaded(exc: BaseException) -> bool:
    """429 (лимит) и 529 (перегрузка Anthropic) — временное, человеку «подожди». Ошибка в
    середине потока (SSE `event: error`) приходит со статусом 200 — узнаём её по `type`."""
    if not _is_anthropic(exc):
        return False
    import anthropic

    if isinstance(exc, (anthropic.RateLimitError, anthropic.OverloadedError)):
        return True
    return isinstance(exc, anthropic.APIStatusError) and exc.type in (
        "rate_limit_error", "overloaded_error",
    )


def misconfigured(exc: BaseException) -> bool:
    """Неверная настройка: ключ не принят или отозван (401/403), модели нет у аккаунта (404).
    Повтор не поможет — нужен человек с ключом (NFR-6)."""
    if not _is_anthropic(exc):
        return False
    import anthropic

    return isinstance(exc, anthropic.APIStatusError) and (
        exc.status_code in (401, 403, 404)
        or exc.type in ("authentication_error", "permission_error", "not_found_error")
    )
