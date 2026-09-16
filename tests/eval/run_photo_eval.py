"""Замер фото блюда (FR-34, MINIAPP-SPEC §6.22) на Nutrition5k.

Nutrition5k (Google) — блюда из столовой, взвешенные по ингредиентам, с КБЖУ и
снимком сверху. Здесь — выборка из ~50 блюд: `picked.txt` (dish_id,kcal,mass,n)
и `img/<dish_id>.jpg`. Датасет в репозиторий не кладём: он чужой и тяжёлый.

Два режима на каждое блюдо, путь — ровно продовый («фото → фраза → разбор»):

- `photo` — только снимок, граммы на глаз модели;
- `photo+mass` — снимок и истинный вес тарелки подписью.

Масло: чип «без» в обоих режимах — в датасете масло уже среди ингредиентов, а
угадывать ответ человека за него мы не берёмся. Бьёт реальный OpenAI (~$1).

    python -m tests.eval.run_photo_eval --data /data [--limit 10] [--save]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from datetime import UTC, datetime
from pathlib import Path

from app.application.results import MealLogged, PlateRead, VariantClarificationNeeded
from tests.eval.catalog import build_eval_assistant

OUT = Path(__file__).resolve().parent / "baselines" / "photo.json"


def _summary(errs: list[float]) -> dict:
    if not errs:
        return {"n": 0}
    abs_ = sorted(abs(e) for e in errs)
    return {
        "n": len(errs),
        "mape_median": round(statistics.median(abs_) * 100, 1),
        "mape_mean": round(statistics.fmean(abs_) * 100, 1),
        "p90": round(abs_[min(len(abs_) - 1, -(-9 * len(abs_) // 10) - 1)] * 100, 1),
        # Знак: минус — занижаем. Рынок занижает на треть (NIH 2026).
        "bias_mean": round(statistics.fmean(errs) * 100, 1),
        "within_20": round(sum(a <= 0.20 for a in abs_) / len(abs_), 2),
    }


async def _one(assistant, user, image: bytes, caption: str) -> tuple[float, float, str] | str:
    read = await assistant.read_photo(user, image, caption=caption)
    if not isinstance(read, PlateRead):
        return f"не блюдо: {read!r}"[:200]
    text = assistant.photo_text(read.reading, read.grams, fat="none")
    res = await assistant.log_photo(user, text, weighed=read.weighed)
    # Уточнение варианта человек закрыл бы тапом — берём первый предложенный,
    # как самый частый выбор (D-19 ставит его первым).
    for _ in range(3):
        if not isinstance(res, VariantClarificationNeeded):
            break
        res = await assistant.resolve_clarification(
            user, res.draft, res.item_index, res.options[0], raw_input=text
        )
    if not isinstance(res, MealLogged):
        return f"не записалось: {type(res).__name__} на «{text}»"
    items = "; ".join(f"{i.name_snapshot.split(' · ')[0]} {i.grams:g}г={i.macros.kcal:.0f}"
                      for i in res.meal.items)
    return res.meal.totals.kcal, sum(read.grams), items


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/data")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    from openai import AsyncOpenAI

    from app.config import get_settings
    from app.infrastructure.vision.openai_vision import OpenAiVision

    settings = get_settings()
    vision = OpenAiVision(AsyncOpenAI(api_key=settings.openai_api_key), settings.openai_model)
    assistant, user = await build_eval_assistant(vision=vision)

    data = Path(args.data)
    rows = [line.split(",") for line in (data / "picked.txt").read_text().split()]
    if args.limit:
        rows = rows[: args.limit]

    kcal_err: dict[str, list[float]] = {"photo": [], "photo+mass": []}
    mass_err: list[float] = []
    failed: dict[str, int] = {"photo": 0, "photo+mass": 0}
    detail = []
    for dish, kcal, mass, _n in rows:
        true_kcal, true_mass = float(kcal), float(mass)
        image = (data / "img" / f"{dish}.jpg").read_bytes()
        line = {"dish": dish, "kcal": true_kcal, "mass": true_mass}
        for mode, caption in (("photo", ""), ("photo+mass", f"{true_mass:g}")):
            try:
                got = await _one(assistant, user, image, caption)
            except Exception as exc:  # noqa: BLE001 — замер не должен падать на одном блюде
                got = f"ошибка: {exc!r}"[:300]
            if not isinstance(got, tuple):
                print(dish, mode, got)
                failed[mode] += 1
                continue
            est_kcal, est_mass, items = got
            line[f"{mode}_items"] = items
            kcal_err[mode].append((est_kcal - true_kcal) / true_kcal)
            line[mode] = round(est_kcal)
            if mode == "photo":
                mass_err.append((est_mass - true_mass) / true_mass)
                line["mass_est"] = round(est_mass)
        detail.append(line)
        print(json.dumps(line, ensure_ascii=False))

    report = {
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "model": settings.openai_model,
        "dataset": "Nutrition5k cafe1, overhead RGB, 150–700 г, ≥3 ингредиентов",
        "photo": {**_summary(kcal_err["photo"]), "failed": failed["photo"]},
        "photo+mass": {**_summary(kcal_err["photo+mass"]), "failed": failed["photo+mass"]},
        "mass_photo": _summary(mass_err),
        "detail": detail,
    }
    print(json.dumps({k: v for k, v in report.items() if k != "detail"},
                     ensure_ascii=False, indent=1))
    if args.save:
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
