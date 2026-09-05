"""Отпечаток статики мини-аппа.

Живёт отдельным модулем, потому что нужен обоим входам: `main_api` подставляет
версию в ссылки страницы, `main_bot` — в адрес кнопки, которой аппка открывается.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def asset_version() -> str:
    """Хэш содержимого статики: меняется ровно тогда, когда меняется код фронта.

    Telegram кэширует мини-апп жёстче обычного браузера и на голый `/app.js`
    может не сходить: 05.09.2026 выкатили новый экран и камеру этикеток, а на
    телефоне остался старый — «моргает и фото нет» при полностью верном файле
    на сервере. Одинаковый URL клиенту нечем отличить от вчерашнего, поэтому
    URL меняется вместе с файлом.
    """
    digest = hashlib.sha256()
    for name in ("app.js", "app.css", "index.html"):
        path = WEB_DIR / name
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]
