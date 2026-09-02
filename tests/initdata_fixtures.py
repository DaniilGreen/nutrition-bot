"""Сборка подлинной initData для тестов — так же, как это делает Telegram.

Живёт отдельным модулем, а не в тест-файле: на него опираются e2e, и
переименование теста не должно ронять половину набора.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

TOKEN = "123456:AA-test-token"
USER = {"id": 777, "first_name": "Даниил", "username": "dg", "language_code": "ru"}


def sign(fields: dict[str, str], token: str = TOKEN) -> str:
    """Собрать подлинную initData так же, как это делает Telegram."""
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


def valid(**over) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAE",
        "user": json.dumps(USER, ensure_ascii=False, separators=(",", ":")),
    }
    fields.update(over)
    return sign(fields)
