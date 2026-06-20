"""Gate правдоподобия (INV-11) — единый предохранитель точности.

Каждая записываемая позиция проходит ОДНУ проверку, независимо от пути (оценка
плотности / FR-22 готовые цифры / порция). Закрывает класс «кривое число в сумме
дня» в одной точке, а не патчами на каждый вход. Чистые функции, без I/O.
"""
from __future__ import annotations

from app.domain.nutrition import reconcile_kcal
from app.domain.value_objects import Density, Macros

# Физические/санитарные границы.
MAX_KCAL_PER_100G = 900.0  # чистый жир ≈ 900 ккал/100 г — физический потолок плотности
MAX_MACRO_PER_100G = 100.0  # больше 100 г макроса на 100 г продукта невозможно
MIN_PORTION_G = 1.0
MAX_PORTION_G = 2000.0  # 2 кг — потолок одной порции
MAX_ITEM_KCAL = 5000.0  # абсолютный потолок ккал одной позиции
MAX_ITEM_MACRO_G = 600.0

# Консервативный фолбэк для пустой оценки (INV-11.3): «готовое блюдо», Атвотер ≈ 146.
# Лучше грубая оценка, чем молчаливый 0 ккал реальной еды. В каталог НЕ пиннится.
FALLBACK_DENSITY = Density(150.0, 8.0, 6.0, 15.0)

# Алкогольные напитки: ккал от ЭТАНОЛА (7 ккал/г), не Б/Ж/У → мимо Атвотера (INV-14/D-18).
_ALCOHOL = (
    "пиво", "вино", "водк", "виски", "коньяк", "ром", "джин", "текил", "ликёр", "ликер",
    "сидр", "шампанск", "просекко", "глинтвейн", "вермут", "бренди", "абсент", "наливк",
    "настойк", "мартини", "пунш", "сангри", "beer", "wine", "vodka", "whisk", "rum",
    "gin", "tequila", "cider", "liqueur", "cognac", "brandy",
)


def is_alcoholic(name: str) -> bool:
    """Напиток с алкоголем → ккал от этанола, мимо Атвотер-сверки (INV-14). Узкий список,
    как `_looks_zero_calorie`; ноль курирования плотностей."""
    n = name.strip().lower()
    if "виноград" in n or "ромашк" in n:  # «вино»⊂«виноград», «ром»⊂«ромашка» — НЕ алкоголь
        return False
    return any(a in n for a in _ALCOHOL)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def clamp_density(d: Density) -> Density:
    """Плотность в санитарные границы (INV-11.2): 0…900 ккал/100 г, макрос 0…100 г/100 г."""
    return Density(
        _clamp(d.kcal_per_100g, 0.0, MAX_KCAL_PER_100G),
        _clamp(d.protein_per_100g, 0.0, MAX_MACRO_PER_100G),
        _clamp(d.fat_per_100g, 0.0, MAX_MACRO_PER_100G),
        _clamp(d.carb_per_100g, 0.0, MAX_MACRO_PER_100G),
    )


def clamp_grams(grams: float) -> float:
    """Граммовку порции в границы (INV-11.2): 1…2000 г."""
    return _clamp(grams, MIN_PORTION_G, MAX_PORTION_G)


def clamp_macros(m: Macros) -> Macros:
    """Только санитарные границы (INV-11.2), БЕЗ Атвотер-сверки. Для названных пользователем
    тоталов (FR-22): явные цифры юзера/меню доверяем, лишь отсекаем абсурд."""
    return Macros(
        _clamp(m.kcal, 0.0, MAX_ITEM_KCAL),
        _clamp(m.protein, 0.0, MAX_ITEM_MACRO_G),
        _clamp(m.fat, 0.0, MAX_ITEM_MACRO_G),
        _clamp(m.carb, 0.0, MAX_ITEM_MACRO_G),
    )


def vet_macros(m: Macros) -> Macros:
    """Финальный gate ОЦЕНКИ (INV-11.1): согласованность ккал↔БЖУ (Атвотер) + границы.

    Для LLM-оценок (плотность×граммы): и ккал, и макросы от модели — должны биться. Названные
    юзером тоталы сюда НЕ идут (для них clamp_macros — доверяем). Идемпотентен.
    """
    p = _clamp(m.protein, 0.0, MAX_ITEM_MACRO_G)
    f = _clamp(m.fat, 0.0, MAX_ITEM_MACRO_G)
    c = _clamp(m.carb, 0.0, MAX_ITEM_MACRO_G)
    kcal = _clamp(reconcile_kcal(m.kcal, p, f, c), 0.0, MAX_ITEM_KCAL)
    return Macros(kcal, p, f, c)
