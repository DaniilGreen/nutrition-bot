"""Скриншот меню с напечатанными КБЖУ (FR-38). Модель переписывает цифры, решает приложение.

Цифры на порцию берутся как свои (FR-22): модель тут не оценивает, а читает. Поэтому всё, что
можно проверить без неё, проверяется здесь — какие позиции брать и не перепутана ли цифра.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.nutrition import atwater_suspect
from app.domain.plausibility import is_alcoholic


@dataclass(frozen=True)
class MenuLine:
    """Позиция меню как напечатана: ккал и Б/Ж/У на порцию, порция и счётчик заказа, если видны."""

    name: str
    kcal: float
    protein: float | None = None
    fat: float | None = None
    carb: float | None = None
    portion: str | None = None
    count: int | None = None


def picked(lines: list[MenuLine]) -> list[MenuLine]:
    """Что записывать. Видны счётчики корзины — только позиции со счётчиком: на скриншоте
    списка рядом лежат блюда, которые человек не заказывал. Счётчиков нет — всё с калориями."""
    with_kcal = [ln for ln in lines if ln.name.strip() and ln.kcal > 0]
    counted = [ln for ln in with_kcal if ln.count]
    return [ln for ln in counted if ln.count > 0] if counted else with_kcal


def title(line: MenuLine) -> str:
    """Название как напечатано плюс порция, если её нет в названии: «Super C Boost 360 мл»."""
    name = line.name.strip()
    portion = (line.portion or "").strip()
    return f"{name} {portion}" if portion and portion.lower() not in name.lower() else name


def totals(line: MenuLine) -> tuple[float, float | None, float | None, float | None]:
    """Ккал и Б/Ж/У с учётом счётчика (две порции — вдвое)."""
    k = max(line.count or 1, 1)

    def x(v: float | None) -> float | None:
        return None if v is None else round(v * k, 1)

    return round(line.kcal * k, 1), x(line.protein), x(line.fat), x(line.carb)


def misread(line: MenuLine) -> bool:
    """Ккал не сходятся с Б/Ж/У — вероятно, модель перепутала цифру (та же сверка, что у
    этикетки, FR-27). Проверяем, только когда напечатаны все четыре числа."""
    if None in (line.protein, line.fat, line.carb) or is_alcoholic(line.name):
        return False
    return atwater_suspect(line.kcal, line.protein, line.fat, line.carb)


def demo() -> None:
    caesar = MenuLine("Chicken Caesar Salad", 410, 45.6, 11.4, 32.2)
    smoothie = MenuLine("Super C Boost", 100, 1.4, 0.4, 24.9, portion="360 мл", count=1)
    # AC-60/AC-61: настоящие цифры сходятся, перепутанная (41 вместо 410) — нет
    assert not misread(caesar) and not misread(smoothie)
    assert misread(MenuLine("Chicken Caesar Salad", 41, 45.6, 11.4, 32.2))
    assert not misread(MenuLine("пиво Tiger", 210, 1.5, 0, 16))  # алкоголь мимо
    assert not misread(MenuLine("салат", 300))  # без Б/Ж/У сверять нечего
    assert title(smoothie) == "Super C Boost 360 мл"
    assert title(MenuLine("Сок 300 мл", 120, portion="300 мл")) == "Сок 300 мл"
    # AC-62: счётчик «2» — вдвое
    assert totals(MenuLine("йогурт", 462, 28.8, 13.5, 62.9, count=2)) == (924, 57.6, 27, 125.8)
    # в корзине только заказанное
    menu = [MenuLine("A", 100, count=1), MenuLine("B", 200), MenuLine("C", 300, count=0)]
    assert [ln.name for ln in picked(menu)] == ["A"]
    assert [ln.name for ln in picked([MenuLine("A", 100), MenuLine("B", 0)])] == ["A"]
    print("меню: ок")


if __name__ == "__main__":
    demo()
