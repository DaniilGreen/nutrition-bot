"""Имя файла для распознавания по типу содержимого.

Whisper выбирает парсер по расширению имени, а не по заголовку. Если браузер
записал mp4 (так делают Safari и Telegram на iOS), а мы назвали файл `voice.ogg`,
распознавание падает или возвращает мусор — при том что запись прошла успешно.

Домен: чистое сопоставление строк, ни ввода-вывода, ни зависимостей.
"""
from __future__ import annotations

# Что принимает Whisper. Ключ — то, что реально шлют браузеры в Content-Type.
_BY_TYPE = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/oga": "ogg",
    "audio/mp4": "mp4",
    "audio/x-m4a": "m4a",
    "audio/m4a": "m4a",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
}

DEFAULT_EXT = "webm"


def filename_for(content_type: str | None) -> str:
    """Имя файла под тип. Параметры вроде `;codecs=opus` отбрасываем."""
    base = (content_type or "").split(";")[0].strip().lower()
    return f"voice.{_BY_TYPE.get(base, DEFAULT_EXT)}"


def demo() -> None:
    """Runnable-чек: браузерные типы и мусор на входе."""
    assert filename_for("audio/webm;codecs=opus") == "voice.webm"
    assert filename_for("audio/ogg; codecs=opus") == "voice.ogg"
    # Safari и Telegram на iOS пишут именно так — раньше это уезжало как .ogg.
    assert filename_for("audio/mp4") == "voice.mp4"
    assert filename_for("AUDIO/MP4") == "voice.mp4"
    assert filename_for("audio/x-m4a") == "voice.m4a"
    # Неизвестное и пустое: webm — то, что шлёт большинство клиентов.
    assert filename_for("application/octet-stream") == "voice.webm"
    assert filename_for(None) == "voice.webm"
    assert filename_for("") == "voice.webm"
    print("имя файла по типу аудио: ок")


if __name__ == "__main__":
    demo()
