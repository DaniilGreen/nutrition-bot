"""Курируемые продукты — стейплы, у которых плотность почти не гуляет.

Список писался под gpt-4.1, когда модели нельзя было доверить русскую и азиатскую кухню:
у USDA «борщ» — американский консервный свекольный отвар (~10 ккал). С переходом на
gpt-5.1 (D-33) гипотезу проверили прогоном всех 44 позиций: на 23 модель сошлась с
константой в пределах 10%, а на рецептурных блюдах оказалась ЛУЧШЕ неё.

Поэтому рецептурные блюда отсюда убраны (D-35). Дело не в самих числах, а в том, что одна
константа обслуживала несовместимые варианты: борщ постный (45), домашний (55) и украинский
со свининой (80) получали одинаковые 50 — уточнение стиралось при матчинге на общий ключ.
Такие блюда теперь оценивает модель, а при большом разбросе она сама спрашивает, какой
именно вариант (порог уточнений, D-33).

Осталось то, где курирование даёт детерминизм даром: крупы, творог, мясо, рыба, протеин и
стандартные заготовки. Курируемые размеры порций для супов больше не нужны — бытовые меры
(«тарелка супа» → 350 г) описаны в промпте и воспроизводятся стабильно.

Значения на 100 г. portion_sizes — граммы для S/M/L (вопрос про порцию).
"""
from __future__ import annotations

from collections.abc import Sequence

# Пресеты размеров порций удалены вместе с рецептурными блюдами: их носили только супы и
# составные горячие. Бытовые меры теперь в промпте и дают те же числа стабильно.

# key, name, kcal, protein, fat, carb (на 100 г), portion_sizes|None
CURATED: list[dict] = [
    # Рецептурные блюда (супы, салаты, составные горячие) убраны — см. докстринг и D-35.
    # Их оценивает модель: она различает варианты, а константа их схлопывала.
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
            if existing is not None and existing.source in ("manual", "recipe"):
                continue  # юзер уже выверил вручную (или это его рецепт) — не перетираем
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
