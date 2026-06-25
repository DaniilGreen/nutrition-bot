"""Курируемые продукты — то, чего нет в открытых базах: русские/азиатские блюда.

USDA отлично знает цельные продукты, но «борщ» у него — американский консервный
свекольный отвар (~10 ккал), а не наш борщ (~50). Открытой RU-базы блюд нет.
Поэтому ~35 частых блюд и стейплов выверены руками и кладутся в `products` с
source='curated'. Дальше работает существующий механизм: LLM видит ключ в
known_keys, мапит «борщ» → borscht и берёт эти числа, а не выдумывает свои.

Значения на 100 г. portion_sizes — граммы для S/M/L (вопрос про порцию).
"""
from __future__ import annotations

from collections.abc import Sequence

_S_SOUP = {"small": 250, "medium": 350, "large": 450}
_S_PHO = {"small": 350, "medium": 500, "large": 650}
_S_MAIN = {"small": 200, "medium": 300, "large": 400}

# key, name, kcal, protein, fat, carb (на 100 г), portion_sizes|None
CURATED: list[dict] = [
    {"key": "borscht", "name": "Борщ", "kcal": 50, "p": 2.5, "f": 2.5, "c": 5, "sizes": _S_SOUP},
    {"key": "shchi", "name": "Щи", "kcal": 35, "p": 1.5, "f": 1.5, "c": 4, "sizes": _S_SOUP},
    {"key": "solyanka", "name": "Солянка", "kcal": 60, "p": 4, "f": 4, "c": 3, "sizes": _S_SOUP},
    {"key": "ukha", "name": "Уха", "kcal": 45, "p": 4, "f": 2, "c": 2, "sizes": _S_SOUP},
    {"key": "pho_bo", "name": "Фо бо", "kcal": 50, "p": 4, "f": 1.5, "c": 5, "sizes": _S_PHO},
    {"key": "tom_yum", "name": "Том ям", "kcal": 60, "p": 4, "f": 3, "c": 4, "sizes": _S_PHO},
    {"key": "pad_thai", "name": "Пад тай", "kcal": 150, "p": 8, "f": 6, "c": 18, "sizes": _S_MAIN},
    {"key": "plov", "name": "Плов", "kcal": 210, "p": 8, "f": 9, "c": 24, "sizes": _S_MAIN},
    {"key": "shawarma", "name": "Шаверма", "kcal": 210, "p": 12, "f": 11, "c": 16, "sizes": _S_MAIN},
    {"key": "pelmeni", "name": "Пельмени", "kcal": 250, "p": 12, "f": 12, "c": 24, "sizes": _S_MAIN},
    {"key": "vareniki_potato", "name": "Вареники с картошкой", "kcal": 180, "p": 5, "f": 5, "c": 28, "sizes": _S_MAIN},
    {"key": "sushi_roll", "name": "Роллы", "kcal": 150, "p": 5, "f": 4, "c": 24, "sizes": _S_MAIN},
    {"key": "sushi", "name": "Суши", "kcal": 140, "p": 6, "f": 3, "c": 22, "sizes": _S_MAIN},
    {"key": "olivier_salad", "name": "Салат оливье", "kcal": 200, "p": 5, "f": 16, "c": 9, "sizes": _S_MAIN},
    {"key": "caesar_salad", "name": "Салат цезарь", "kcal": 200, "p": 9, "f": 15, "c": 7, "sizes": _S_MAIN},
    {"key": "vinaigrette_salad", "name": "Винегрет", "kcal": 80, "p": 1.5, "f": 5, "c": 7, "sizes": _S_MAIN},
    {"key": "goulash", "name": "Гуляш", "kcal": 150, "p": 14, "f": 8, "c": 4, "sizes": _S_MAIN},
    {"key": "cutlet_pork_beef", "name": "Котлета", "kcal": 250, "p": 15, "f": 18, "c": 8, "sizes": None, "cat": "cutlet"},
    {"key": "cutlet_chicken", "name": "Куриная котлета", "kcal": 210, "p": 16, "f": 13, "c": 8, "sizes": None, "cat": "cutlet"},
    {"key": "syrniki", "name": "Сырники", "kcal": 215, "p": 17, "f": 8, "c": 18, "sizes": None},
    {"key": "bliny", "name": "Блины", "kcal": 190, "p": 6, "f": 7, "c": 26, "sizes": None},
    {"key": "omelette", "name": "Омлет", "kcal": 180, "p": 11, "f": 14, "c": 2, "sizes": None},
    {"key": "fried_eggs", "name": "Яичница", "kcal": 190, "p": 13, "f": 15, "c": 1, "sizes": None},
    {"key": "oatmeal_water", "name": "Овсянка на воде", "kcal": 90, "p": 3, "f": 2, "c": 15, "sizes": None, "cat": "oatmeal"},
    {"key": "oatmeal_milk", "name": "Овсянка на молоке", "kcal": 120, "p": 4.5, "f": 4, "c": 16, "sizes": None, "cat": "oatmeal"},
    {"key": "buckwheat_boiled", "name": "Гречка отварная", "kcal": 110, "p": 4, "f": 1, "c": 21, "sizes": None},
    {"key": "rice_boiled", "name": "Рис отварной", "kcal": 130, "p": 2.7, "f": 0.3, "c": 28, "sizes": None},
    {"key": "pasta_boiled", "name": "Макароны отварные", "kcal": 157, "p": 5.8, "f": 0.9, "c": 31, "sizes": None},
    {"key": "mashed_potato", "name": "Картофельное пюре", "kcal": 90, "p": 2, "f": 3, "c": 14, "sizes": None},
    {"key": "tvorog_0", "name": "Творог обезжиренный", "kcal": 79, "p": 18, "f": 0.6, "c": 1.8, "sizes": None, "cat": "tvorog"},
    {"key": "tvorog_3", "name": "Творог 3%", "kcal": 113, "p": 18, "f": 3, "c": 3.5, "sizes": None, "cat": "tvorog"},
    {"key": "tvorog_5", "name": "Творог 5%", "kcal": 120, "p": 16, "f": 5, "c": 3, "sizes": None, "cat": "tvorog"},
    {"key": "tvorog_9", "name": "Творог 9%", "kcal": 160, "p": 16, "f": 9, "c": 3, "sizes": None, "cat": "tvorog"},
    {"key": "chicken_breast_boiled", "name": "Куриная грудка отварная", "kcal": 130, "p": 29, "f": 1.8, "c": 0, "sizes": None, "cat": "chicken"},
    {"key": "chicken_breast_fried", "name": "Куриная грудка жареная", "kcal": 200, "p": 30, "f": 8, "c": 0, "sizes": None, "cat": "chicken"},
    {"key": "chicken_grilled", "name": "Курица гриль", "kcal": 200, "p": 22, "f": 12, "c": 0, "sizes": None, "cat": "chicken"},
    {"key": "whey_protein", "name": "Протеин (сывороточный)", "kcal": 370, "p": 75, "f": 6, "c": 8, "sizes": None},
    {"key": "gold_standard_whey", "name": "Gold Standard Whey", "kcal": 395, "p": 79, "f": 3, "c": 10, "sizes": None},
    {"key": "pangasius", "name": "Пангасиус", "kcal": 120, "p": 18, "f": 5, "c": 0, "sizes": None},
    {"key": "cod", "name": "Треска", "kcal": 105, "p": 23, "f": 1, "c": 0, "sizes": None},
    {"key": "tilapia", "name": "Тилапия", "kcal": 96, "p": 20, "f": 1.7, "c": 0, "sizes": None},
    {"key": "pollock", "name": "Минтай", "kcal": 79, "p": 18, "f": 1, "c": 0, "sizes": None},
    {"key": "salmon", "name": "Лосось", "kcal": 200, "p": 20, "f": 13, "c": 0, "sizes": None},
    {"key": "shrimp", "name": "Креветки", "kcal": 95, "p": 19, "f": 1, "c": 1, "sizes": None},
]


def validate_curated(rows: Sequence[dict] = CURATED) -> None:
    """Self-check: уникальные ключи, неотрицательные макросы и Atwater сходится.

    Atwater-проверка (4P+9F+4C ≈ kcal) ловит грубые опечатки — ровно тот класс
    бага, где у протеина оказался 0.9 г белка.
    """
    seen: set[str] = set()
    for r in rows:
        key = r["key"]
        assert key and key not in seen, f"bad/duplicate key: {key}"
        seen.add(key)
        assert all(r[k] >= 0 for k in ("kcal", "p", "f", "c")), f"negative macro: {key}"
        atwater = 4 * r["p"] + 9 * r["f"] + 4 * r["c"]
        assert abs(atwater - r["kcal"]) <= 0.3 * r["kcal"] + 20, (
            f"macros don't add up for {key}: atwater={atwater} vs kcal={r['kcal']}"
        )


async def seed_curated(session_factory: object) -> int:
    """Идемпотентный upsert курируемых продуктов. Ручные правки юзера не трогаем."""
    from sqlalchemy import select

    from app.infrastructure.db.models import ProductORM

    validate_curated()
    written = 0
    async with session_factory() as session:  # type: ignore[operator]
        for r in CURATED:
            res = await session.execute(
                select(ProductORM).where(ProductORM.canonical_key == r["key"])
            )
            existing = res.scalar_one_or_none()
            if existing is not None and existing.source == "manual":
                continue  # юзер уже выверил вручную — не перетираем
            target = existing or ProductORM(canonical_key=r["key"])
            target.display_name = r["name"]
            target.category = r.get("cat")
            target.kcal_per_100g = r["kcal"]
            target.protein_per_100g = r["p"]
            target.fat_per_100g = r["f"]
            target.carb_per_100g = r["c"]
            target.portion_sizes = r["sizes"]
            target.default_portion_g = r["sizes"]["medium"] if r["sizes"] else None
            target.source = "curated"
            target.is_estimate = False
            if existing is None:
                session.add(target)
            written += 1
        await session.commit()
    return written


if __name__ == "__main__":
    validate_curated()
    print(f"OK: {len(CURATED)} curated products valid")
