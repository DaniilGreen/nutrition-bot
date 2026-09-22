"""Настройки оформления: что можно менять и в каких пределах (§6.5).

Белый список, а не «складываем что прислали»: `ui_prefs` — это JSONB, куда
клиент шлёт произвольный объект. Без списка туда уедет что угодно, включая
мегабайт мусора в каждой строке пользователя.

MA-INV-31: настройки без поведения за ней тут нет. Каждый ключ читается фронтом
и что-то меняет: `hero` переключает верхнюю панель, `foot_third` — третью ячейку
подвала, `period` — период, открывающийся в «Итогах», `mic_first` — начинать ли
ввод с микрофона.
"""
from __future__ import annotations

from typing import Any

# ключ → (допустимые значения, значение по умолчанию)
ALLOWED: dict[str, tuple[tuple[Any, ...], Any]] = {
    # Вид верхней панели. Принят вариант Е; кольца оставлены как альтернатива,
    # потому что владелец просил обратимость выбора.
    "hero": (("bars", "rings", "kcal"), "bars"),
    # Что показывать в подвале панели: третья ячейка занята весом или приёмами.
    # По умолчанию вес: так в макете панели Е (§6.2). Число приёмов —
    # справочное, вес участвует в сверке дневника с весами (§4.0).
    "foot_third": (("weight", "meals"), "weight"),
    # Период, открывающийся в «Итогах» по умолчанию.
    "period": ((7, 30, 90), 30),
    # Открывать ввод сразу микрофоном. По умолчанию нет: непрошеный запрос
    # доступа к микрофону на первом же экране — плохой первый опыт.
    "mic_first": ((True, False), False),
    # Порядок ленты дня. По умолчанию новое сверху: только что записанное
    # проверяют сразу, а утренний кофе — почти никогда.
    "feed_order": (("new_first", "old_first"), "new_first"),
    # С какого часа начинаются сутки (§6.5). Перекус в 00:30 — это ещё вчерашний
    # вечер, а не завтрак: у владельца таких 14 за 90 дней (замер 17.09.2026).
    "day_start": ((0, 3, 4, 5, 6), 0),
    # Замеры состава (§6.24): откуда брать базовый обмен и какой множитель
    # активности. Профиль визарда в БД не хранится, поэтому активность живёт тут.
    "bmr_source": (("inbody", "formula"), "inbody"),
    "activity": (("low", "medium", "high"), "medium"),
    # Цель по проценту жира (§6.24). Нужна только для срока в «Итогах»: на
    # калории, белок и расход не влияет. Умолчания нет сознательно — подставить
    # чужому телу «норму» приложение не берётся (MA-INV-28).
    "target_fat_pct": ((None, 10, 12, 15, 18, 20, 25), None),
}


def day_start(prefs: object) -> int:
    """Час начала суток из настроек человека; мусор — полночь."""
    return int(sanitize(prefs)["day_start"])


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
    assert day_start({"day_start": 4}) == 4
    assert day_start({"day_start": 13}) == 0 and day_start(None) == 0
    assert sanitize({"day_start": True})["day_start"] == 0   # True == 1, но 1 не в списке
    print("настройки: ок")


if __name__ == "__main__":
    demo()
