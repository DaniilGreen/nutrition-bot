"""Замер фото блюда (FR-34, MINIAPP-SPEC §6.22) на Nutrition5k.

Nutrition5k (Google) — блюда из столовой, взвешенные по ингредиентам, с КБЖУ и
снимком сверху. Здесь — выборка из ~50 блюд: `picked.txt` (dish_id,kcal,mass,n)
и `img/<dish_id>.jpg`. Датасет в репозиторий не кладём: он чужой и тяжёлый.

Два режима на каждое блюдо, путь — ровно продовый («фото → фраза → разбор»):

- `photo` — только снимок, граммы на глаз модели;
- `photo+mass` — снимок и истинный вес тарелки подписью.

Масло: как в проде (D-MA-20) — приложение само добавляет строку «на готовку», её ккал входят
в расчёт: в датасете масло тоже среди ингредиентов. В оценку МАССЫ тарелки она не входит —
это не глаз модели, а добавка приложения.

Бьёт реальную модель — деньги; провайдер и модель — флагами (NFR-6), потолок `--max-usd`
жёсткий: новый вызов не начинается. На каждое блюдо — 2 режима × (фото + разбор).

    python -m tests.eval.run_photo_eval --data /data [--limit 10] [--save]
    python -m tests.eval.run_photo_eval --data /data --provider anthropic \
        --model claude-opus-5-5 --max-usd 3 --limit 20
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from datetime import UTC, datetime
from pathlib import Path

from app.application.results import MealLogged, PlateRead, VariantClarificationNeeded
from tests.eval.catalog import build_eval_assistant, eval_settings
from tests.eval.harness import BudgetExceeded, EvalMeter

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
    # Строки «на готовку» (масло, заправка) — в фразе, как у человека, нажавшего «Записать».
    text = assistant.photo_text(read.reading, read.grams)
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
    # Масса тарелки — только то, что оценила модель: масло добавило приложение (D-MA-20), и с
    # ним ошибка массы мерила бы чужую добавку, а не глаз.
    eyed = sum(g for c, g in zip(read.reading.components, read.grams, strict=True) if not c.note)
    return res.meal.totals.kcal, eyed, items


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/data")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--provider", choices=("openai", "anthropic"), default=None,
                    help="по умолчанию — LLM_PROVIDER из окружения")
    ap.add_argument("--model", default="", help="модель фото; пусто — умолчание провайдера")
    ap.add_argument("--nlu-model", default="", help="модель разбора фразы с карточки")
    ap.add_argument("--effort", default="", help="Claude: low|medium|high|xhigh|max")
    ap.add_argument("--thinking-off", action="store_true")
    ap.add_argument("--max-usd", type=float, default=3.0,
                    help="жёсткий потолок трат прогона (по прод-ценам)")
    args = ap.parse_args()

    from app.config import get_settings

    settings = eval_settings(
        args.provider or get_settings().llm_provider, nlu_model=args.nlu_model,
        vision_model=args.model, effort=args.effort, thinking_off=args.thinking_off,
    )
    meter = EvalMeter(args.max_usd)
    assistant, user = await build_eval_assistant(settings, meter, with_vision=True)

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
                meter.check()
                got = await _one(assistant, user, image, caption)
            except BudgetExceeded as exc:
                print(f"⛔ {exc} — дальше не идём, отчёт по сделанному")
                break
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
        if meter.spent_usd >= meter.max_usd:
            break

    records = sum(len(v) for v in kcal_err.values()) + sum(failed.values())
    report = {
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "provider": settings.llm_provider,
        "model": settings.model_for("vision"),
        "nlu_model": settings.model_for("nlu"),
        # Запись — одно блюдо в одном режиме: фото + разбор фразы с карточки.
        "cost": meter.summary(records),
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
