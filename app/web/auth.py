"""Проверка подписи Telegram initData — граница доверия аппки (§7.1).

Всё, что приходит из браузера, недостоверно, пока подпись не сошлась. Поэтому
здесь нет ни одного упрощения: полное сравнение по HMAC, проверка срока, разбор
без «ну наверное так». Модуль чистый — ни FastAPI, ни БД, чтобы тестировать
подпись без поднятия чего-либо (MA-INV-8: проверка на границе web, не в application).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

# Сутки. Не час: Telegram не обновляет initData, пока аппка открыта, и часовой
# срок выкидывал бы посреди работы. Не неделя: украденная строка живёт ровно
# столько, сколько её срок годности (§7.1).
MAX_AGE_SECONDS = 24 * 60 * 60


class InitDataError(Exception):
    """Подпись не сошлась, строка испорчена или протухла. Наружу — только 401."""


@dataclass(frozen=True)
class TelegramUser:
    telegram_id: int
    first_name: str
    username: str | None
    language_code: str | None


def verify_init_data(
    init_data: str, bot_token: str, *, now: float | None = None
) -> TelegramUser:
    """Вернуть пользователя из initData или бросить InitDataError.

    Идентичность берётся ТОЛЬКО отсюда (MA-INV-7): вызывающий не вправе взять
    user_id из тела запроса.
    """
    if not init_data:
        raise InitDataError("пустая initData")
    if not bot_token:
        # Пустой токен даёт стабильный secret_key, и подпись начала бы сходиться
        # у любого, кто его подберёт. Падаем громко, а не проверяем «как-нибудь».
        raise InitDataError("не задан BOT_TOKEN")

    # keep_blank_values: пустое поле — часть подписанной строки, выкинув его,
    # мы посчитали бы другой data_check_string и получили бы ложный отказ.
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    if not pairs:
        raise InitDataError("initData не разбирается")

    fields = dict(pairs)
    if len(fields) != len(pairs):
        # Дубль ключа: parse_qsl оставит последний, а Telegram подписывал другой
        # набор. Такая строка не может быть подлинной.
        raise InitDataError("повторяющийся ключ в initData")

    received = fields.pop("hash", None)
    if not received:
        raise InitDataError("нет поля hash")

    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    # compare_digest, а не ==: сравнение строк выходит на первом же различии и
    # по времени ответа подпись можно подобрать побайтно.
    if not hmac.compare_digest(expected, received):
        raise InitDataError("подпись не сошлась")

    raw_auth_date = fields.get("auth_date")
    if not raw_auth_date:
        raise InitDataError("нет auth_date")
    try:
        auth_date = int(raw_auth_date)
    except ValueError as exc:
        raise InitDataError("auth_date не число") from exc

    current = time.time() if now is None else now
    age = current - auth_date
    if age > MAX_AGE_SECONDS:
        raise InitDataError("initData просрочена")
    if age < -300:
        # Из будущего дальше, чем на расхождение часов, — подделка либо сломанное
        # время. И то и другое повод отказать.
        raise InitDataError("auth_date из будущего")

    raw_user = fields.get("user")
    if not raw_user:
        raise InitDataError("нет поля user")
    try:
        user = json.loads(raw_user)
    except json.JSONDecodeError as exc:
        raise InitDataError("user не разбирается") from exc
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise InitDataError("в user нет числового id")

    return TelegramUser(
        telegram_id=user["id"],
        first_name=str(user.get("first_name") or ""),
        username=user.get("username") or None,
        language_code=user.get("language_code") or None,
    )
