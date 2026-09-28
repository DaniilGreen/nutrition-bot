"""Стоит ли переспрашивать (FR-20, FR-21). Модель заполняет, приложение решает (INV-1).

28.09.2026 gpt-5.1 зря переспрашивала в 43 записях из 133: правило «спрашивай, если разброс ≥25%»
стояло в промпте, но прикидки в ответе не было, и модель спрашивала по привычке. Теперь она
отдаёт числа, а порог проверяет код — его нельзя «пропустить».
"""
from __future__ import annotations

from collections.abc import Sequence

# Крайние варианты различаются в 1.25 раза и больше — тофу мягкий 55 и плотный 145 — спрашиваем;
# грудка отварная 137 и на гриле 165 (1.2×) — нет.
VARIANT_RATIO = 1.25
# Разница между большой и маленькой порцией меньше этого — размер не спрашиваем: ошибка
# меньше, чем разброс оценки самой плотности.
PORTION_KCAL = 40.0


def worth_asking_variant(options: Sequence[str], kcals: Sequence[float] | None) -> bool:
    """Нет оценок или их число не совпало с вариантами — спрашиваем, как раньше: лишний
    вопрос дешевле неверной плотности, которая пиннится навсегда (INV-2)."""
    if not kcals or len(kcals) != len(options):
        return True
    lo, hi = min(kcals), max(kcals)
    if lo <= 0:
        return True
    return hi / lo >= VARIANT_RATIO


def worth_asking_portion(
    zero_calorie: bool, kcal_per_100g: float, sizes: dict[str, float] | None
) -> bool:
    """Без калорий размер ничего не меняет; без размеров от модели — спрашиваем, как раньше.

    Плотность 0 без флага zero_calorie — не «ноль калорий», а «модель не оценила»: у
    продукта из каталога она вправе её не давать (плотность берётся из базы, INV-2).
    Считать по нулю значило бы молча не спросить размер знакомого борща."""
    if zero_calorie:
        return False
    if kcal_per_100g <= 0 or not sizes or "small" not in sizes or "large" not in sizes:
        return True
    return (sizes["large"] - sizes["small"]) * kcal_per_100g / 100 >= PORTION_KCAL


def demo() -> None:
    # AC-57 / AC-58
    assert not worth_asking_variant(["отварная", "гриль"], [137, 165])
    assert worth_asking_variant(["мягкий", "плотный"], [55, 145])
    # Нет чисел или их меньше, чем вариантов — спрашиваем по-старому.
    assert worth_asking_variant(["а", "б"], None)
    assert worth_asking_variant(["а", "б", "в"], [100, 200])
    assert worth_asking_variant(["а", "б"], [0, 100])
    # AC-59: вода; кофе с молоком 200/400 мл по 40 ккал — 80 ккал, спрашиваем.
    assert not worth_asking_portion(True, 0, {"small": 200, "large": 500})
    assert worth_asking_portion(False, 40, {"small": 200, "large": 400})
    # Чёрный кофе без флага, 2 ккал/100 г: 200 против 400 мл — 4 ккал, не спрашиваем.
    assert not worth_asking_portion(False, 2, {"small": 200, "large": 400})
    assert worth_asking_portion(False, 250, None)
    # Знакомый борщ: модель плотность не дала — спрашиваем, а не считаем по нулю.
    assert worth_asking_portion(False, 0, {"small": 250, "large": 450})
    print("стоит ли спрашивать: ок")


if __name__ == "__main__":
    demo()
