"""Настройки оформления: что можно менять и в каких пределах (§6.5).

Белый список, а не «складываем что прислали»: `ui_prefs` — это JSONB, куда
клиент шлёт произвольный объект. Без списка туда уедет что угодно, включая
мегабайт мусора в каждой строке пользователя.

MA-INV-31: настройки без поведения за ними тут нет. Каждый ключ читается
фронтом и что-то меняет.
"""
from __future__ import annotations

from typing import Any

# ключ → (допустимые значения, значение по умолчанию)
ALLOWED: dict[str, tuple[tuple[Any, ...], Any]] = {
    # Вид верхней панели. Принят вариант Е; кольца оставлены как альтернатива,
    # потому что владелец просил обратимость выбора.
    "hero": (("bars", "rings", "kcal"), "bars"),
    # Что показывать в подвале панели: третья ячейка занята весом или приёмами.
    "foot_third": (("weight", "meals"), "meals"),
    # Период, открывающийся в «Итогах» по умолчанию.
    "period": ((7, 30, 90), 30),
    # Открывать ввод сразу микрофоном. По умолчанию нет: непрошеный запрос
    # доступа к микрофону на первом же экране — плохой первый опыт.
    "mic_first": ((True, False), False),
}


def defaults() -> dict[str, Any]:
    return {k: v[1] for k, v in ALLOWED.items()}


def sanitize(raw: object) -> dict[str, Any]:
    """Оставить только известные ключи с допустимыми значениями.

    Молча отбрасываем неизвестное, а не падаем: клиент может быть новее
    сервера, и старая версия не обязана понимать новый ключ.
    """
    out = defaults()
    if not isinstance(raw, dict):
        return out
    for key, (allowed, _) in ALLOWED.items():
        if key in raw and raw[key] in allowed:
            out[key] = raw[key]
    return out


def merge(current: object, patch: object) -> dict[str, Any]:
    """Применить частичную правку поверх текущих настроек."""
    merged = dict(sanitize(current))
    if isinstance(patch, dict):
        for key, (allowed, _) in ALLOWED.items():
            if key in patch and patch[key] in allowed:
                merged[key] = patch[key]
    return merged


def demo() -> None:
    """Runnable-чек: мусор не проходит, частичная правка работает."""
    assert defaults()["hero"] == "bars"
    assert sanitize(None) == defaults()
    assert sanitize({"hero": "rings"})["hero"] == "rings"
    # Неизвестный ключ и негодное значение отбрасываются.
    assert "лишнее" not in sanitize({"лишнее": 1})
    assert sanitize({"hero": "что-то"})["hero"] == "bars"
    assert sanitize({"period": 999})["period"] == 30
    # Частичная правка не сбрасывает остальное.
    cur = {"hero": "rings", "period": 7}
    assert merge(cur, {"period": 90}) == {**defaults(), "hero": "rings", "period": 90}
    # True/False различаются, хотя True == 1 в питоне.
    assert merge(cur, {"mic_first": True})["mic_first"] is True
    print("настройки: ок")


if __name__ == "__main__":
    demo()
