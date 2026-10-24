"""Тарелка с фото: доли → граммы → текст для разбора (FR-34, MINIAPP-SPEC §6.22).

Фото говорит, ЧТО лежит на тарелке, человек — СКОЛЬКО. Здесь только арифметика над
граммами и сборка фразы; КБЖУ по-прежнему считает обычный путь разбора (INV-1).
Правило одно на бот и мини-апп, поэтому живёт в домене.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_COMPONENTS = 8
MIN_ITEM_G = 1.0
MAX_ITEM_G = 3000.0
MAX_PLATE_G = 5000.0

# Масло при готовке: граммы по чипу. «Ложка» — столовая, ~10 г масла.
FAT_GRAMS: dict[str, float] = {"none": 0.0, "little": 5.0, "spoon": 10.0, "lots": 20.0}
FAT_KINDS = ("масло подсолнечное", "масло сливочное", "масло оливковое")
DEFAULT_FAT_KIND = FAT_KINDS[0]

# Заправка: что и сколько добавить позицией.
DRESSINGS: dict[str, tuple[str, float] | None] = {
    "none": None,
    "sour_cream": ("сметана 15%", 20.0),
    "mayo": ("майонез", 15.0),
    "oil": ("масло подсолнечное", 10.0),
}


@dataclass
class Component:
    name: str
    share: float          # доля по массе от модели, 0..1 (сумма может не сходиться)
    grams: float          # оценка модели, г
    fixed: bool = False   # вес этой позиции назван человеком — делёж его не трогает
    note: str = ""        # D-MA-20: «на готовку» — строку добавило приложение, а не глаз модели


def clean_name(name: str) -> str:
    """MA-INV-47: запятая разделила бы позицию надвое, цифра подменила бы вес.

    Процент жирности («сметана 15%») — часть имени, а не вес: его сохраняем.
    """
    out = re.sub(r"\d+(?:[.,]\d+)?\s*%", lambda m: m.group().replace(",", ".").replace(" ", ""),
                 name or "")
    out = re.sub(r"(?<![\d.])\d+(?:[.,]\d+)?\s*(?:кг|гр|грамм\w*|г|мл|л|kg|g|ml)(?!\w)",
                 " ", out, flags=re.IGNORECASE)
    out = re.sub(r"[,;\n\r\t]+", " ", out)
    out = re.sub(r"(?<![\d.])\d+(?:\.\d+)?(?!\.?\d*%)", " ", out)
    out = re.sub(r"\s+", " ", out).strip(" -–—:.")
    return out[:80]


def caption_numbers(text: str) -> set[float]:
    """Числа, которые человек действительно написал. Вес, прочитанный моделью из
    подписи, принимаем, только если он тут есть: иначе выдумка модели прошла бы
    как «взвешено»."""
    return {
        float(m.replace(",", "."))
        for m in re.findall(r"\d+(?:[.,]\d+)?", text or "")
    }


def said(value: float | None, numbers: set[float]) -> bool:
    """Число от модели есть среди написанных человеком — с допуском на запись
    float в JSON («150» могло вернуться как 150.0000001)."""
    return value is not None and any(abs(value - n) < 0.5 for n in numbers)


def split(components: list[Component], total_g: float | None) -> list[float]:
    """Граммы каждой позиции.

    Нет общего веса — оценки модели как есть. Есть — позиции с названным весом
    остаются, остаток делится пропорционально оценкам граммов; нет оценок — по
    долям; нет и их — поровну.

    Почему оценки, а не доли: замер 16.09.2026 — модель давала миндалю 30% тарелки
    по площади, хотя сама же насчитала ему 48 г из 350. Граммы она считает
    поштучно, доли — на глаз; у плотной мелочи (орехи, сыр) это разница втрое.
    """
    est = [_bound(c.grams) for c in components]
    if not total_g or total_g <= 0 or not components:
        return est
    fixed = sum(est[i] for i, c in enumerate(components) if c.fixed)
    free = [i for i, c in enumerate(components) if not c.fixed]
    rest = max(total_g - fixed, 0.0)
    if not free:
        return est
    weights = [est[i] for i in free]
    if sum(weights) <= 0:
        weights = [max(components[i].share, 0.0) for i in free]
    if sum(weights) <= 0:
        weights = [1.0] * len(free)
    total_w = sum(weights)
    out = list(est)
    for i, w in zip(free, weights, strict=True):
        out[i] = float(round(rest * w / total_w))
    return out


def _bound(grams: float | None) -> float:
    g = float(grams or 0)
    return float(round(min(max(g, 0.0), MAX_ITEM_G)))


def valid_grams(grams: list[float]) -> bool:
    """MA-INV-44: каждая позиция 1–3000 г, сумма ≤ 5000 г, хотя бы одна позиция."""
    return (
        bool(grams)
        and all(MIN_ITEM_G <= g <= MAX_ITEM_G for g in grams)
        and sum(grams) <= MAX_PLATE_G
    )


def extras(fat: str | None, fat_kind: str | None, dressing: str | None) -> list[tuple[str, float]]:
    """Добавки по выбранным чипам. Неизвестный выбор — как «без»: гадать за человека
    хуже, чем недописать ложку масла, которую он не отметил."""
    out: list[tuple[str, float]] = []
    g = FAT_GRAMS.get(fat or "none", 0.0)
    if g:
        out.append((fat_kind if fat_kind in FAT_KINDS else DEFAULT_FAT_KIND, g))
    d = DRESSINGS.get(dressing or "none")
    if d:
        out.append(d)
    return out


def compose(parts: list[tuple[str, float]]) -> str:
    """«макароны отварные 260 г, курица 110 г» — ровно та фраза, что набрал бы человек."""
    return ", ".join(f"{clean_name(n)} {g:g} г" for n, g in parts if clean_name(n) and g > 0)


def demo() -> None:
    pasta = [
        Component("макароны отварные", 0.6, 250),
        Component("курица жареная", 0.3, 100),
        Component("соус сливочный", 0.1, 40),
    ]
    assert split(pasta, None) == [250, 100, 40]
    assert split(pasta, 390) == [250, 100, 40]
    assert split(pasta, 780) == [500, 200, 80]
    # Названный вес курицы остаётся, остаток 250 делится 250:40.
    pasta[1].fixed = True
    assert split(pasta, 350) == [216, 100, 34]
    # Доли — вторая очередь: миндаль «30% площади» не перебивает поштучные 48 г.
    nuts = [Component("яблоко", 0.3, 180), Component("миндаль", 0.3, 48)]
    assert split(nuts, 228) == [180, 48]
    # Нет оценок — по долям; нет и их — поровну.
    assert split([Component("а", 1, 0), Component("б", 3, 0)], 200) == [50, 150]
    assert split([Component("а", 0, 0), Component("б", 0, 0)], 200) == [100, 100]
    # Названное больше общего веса — остаток ноль, а не минус.
    assert split([Component("а", 1, 500, True), Component("б", 1, 50)], 300) == [500, 0]

    assert clean_name("курица, 150г; жареная\n") == "курица жареная"
    assert clean_name("суп 0,5 л") == "суп"
    assert clean_name("сметана 15%") == "сметана 15%"
    assert clean_name("молоко 2,5%") == "молоко 2.5%"
    assert compose([("макароны", 260.0), ("курица, жареная", 110.0), ("пусто", 0)]) == (
        "макароны 260 г, курица жареная 110 г"
    )
    assert compose([("сметана 15%", 20.0)]) == "сметана 15% 20 г"
    assert extras("spoon", "масло сливочное", "mayo") == [
        ("масло сливочное", 10.0), ("майонез", 15.0)
    ]
    assert extras("lots", "бензин", None) == [(DEFAULT_FAT_KIND, 20.0)]
    assert extras("whatever", None, "nope") == []

    assert caption_numbers("тарелка 420 г, курица 150,5") == {420.0, 150.5}
    assert caption_numbers("") == set()
    assert said(150.0000001, {150.0}) and not said(151, {150.0}) and not said(None, {1.0})

    assert valid_grams([100, 200])
    assert not valid_grams([])
    assert not valid_grams([0.0])
    assert not valid_grams([3001])
    assert not valid_grams([3000, 2500])
    print("plate ok")


if __name__ == "__main__":
    demo()
