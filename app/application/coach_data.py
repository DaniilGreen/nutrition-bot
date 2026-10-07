"""Данные, которые ассистент может запросить сам (§6.21).

MA-INV-39: объект создаётся УЖЕ привязанным к человеку. Ни в одном методе нет
аргумента пользователя, и в схеме инструментов его тоже нет — модель не может
попросить чужие приёмы не потому, что мы проверяем аргумент, а потому, что
такого аргумента не существует. Это граница доверия, упрощать нельзя.

Что здесь НЕ происходит: расчёт КБЖУ. Отдаются числа, посчитанные приложением
раньше (INV-1). Модель их читает, а не считает.
"""
from __future__ import annotations

from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from app.application.entities import User
from app.application.ports import BodyScanRepo, MealRepo, ProductRepo, WeightRepo
from app.application.stats_service import StatsService
from app.clock import Clock, utcnow
from app.domain.body_scan import pace
from app.domain.prefs import day_start
from app.domain.timeframe import local_date

# Потолок строк в ответе инструмента. Один вопрос «покажи всё» иначе втащит
# в промпт весь дневник и съест месячный бюджет.
MAX_ROWS = 60
MAX_DAYS = 90


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    """Аргумент приходит от модели — то есть это ввод, а не константа."""
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


class CoachData:
    def __init__(
        self,
        user: User,
        stats: StatsService,
        meals: MealRepo,
        weights: WeightRepo,
        products: ProductRepo | None = None,
        scans: BodyScanRepo | None = None,
        clock: Clock = utcnow,
    ) -> None:
        self._user = user
        self._stats = stats
        self._meals = meals
        self._weights = weights
        self._products = products
        self._scans = scans
        self._clock = clock

    @property
    def _uid(self) -> int:
        uid = self._user.id
        if uid is None:  # до сохранения пользователя инструментов быть не может
            raise ValueError("пользователь без id")
        return uid

    async def day_totals(self, days: Any = 14) -> list[dict[str, Any]]:
        """По дням: сколько съедено и из чего. Дни без записей тоже отдаём —
        по ним видно пропуски, а это половина ответа на «почему не худею»."""
        n = _clamp(days, 1, MAX_DAYS, 14)
        rows = await self._stats.last_days_macros(self._user, n)
        return [
            {
                "date": d.isoformat(),
                "kcal": round(m.kcal),
                "protein_g": round(m.protein),
                "fat_g": round(m.fat),
                "carb_g": round(m.carb),
                "meals": count,
            }
            for d, m, count in rows[-MAX_ROWS:]
        ]

    async def meals_on(self, date_str: Any = None) -> dict[str, Any]:
        """Приёмы конкретного дня с позициями: время, что записано, граммовка."""
        today = local_date(self._clock(), self._user.timezone, day_start(self._user.ui_prefs))
        day = _parse_day(date_str, today)
        days_ago = (today - day).days
        if days_ago < 0 or days_ago > 365:
            return {"date": day.isoformat(), "meals": [],
                    "note": "День вне доступного диапазона."}
        meals, actual = await self._stats.day_view(self._user, days_ago)
        zone = ZoneInfo(self._user.timezone)
        return {
            "date": actual.isoformat(),
            "meals": [
                {
                    "at": m.eaten_at.astimezone(zone).strftime("%H:%M"),
                    "said": m.raw_input[:120],
                    "kcal": round(m.totals.kcal),
                    # Без Б/Ж/У модель честно отвечала «в базе только ккал» (§6.21.2).
                    **_pfc(m.totals),
                    "items": [
                        {
                            "name": i.name_snapshot,
                            "grams": round(i.grams),
                            "kcal": round(i.macros.kcal),
                            **_pfc(i.macros),
                            "estimated": bool(i.is_estimate),
                        }
                        for i in m.items
                    ],
                }
                for m in meals[:MAX_ROWS]
            ],
        }

    async def top_products(self, days: Any = 30, by: Any = "kcal") -> list[dict[str, Any]]:
        """Вклад продуктов за окно. Именно вклад, а не частота: редкий крупный
        обед весит больше ежедневного кофе."""
        n = _clamp(days, 1, MAX_DAYS, 30)
        key = by if by in ("kcal", "protein", "fat", "carb") else "kcal"
        rows = await self._meals.contributions(self._uid, days=n, limit=20, by=key)
        return [
            {"name": name, "total": round(total), "unit": key, "times": times}
            for _pid, name, total, times in rows[:MAX_ROWS]
        ]

    async def weight_log(self, limit: Any = 14) -> list[dict[str, Any]]:
        """Замеры веса. Без них вопрос «почему вес стоит» упирается в пустоту."""
        n = _clamp(limit, 1, MAX_ROWS, 14)
        logs = await self._weights.history(self._uid, n)
        return [
            {"date": log.measured_at.date().isoformat(), "kg": round(log.weight_kg, 1)}
            for log in logs
        ]


    async def accuracy(self, days: Any = 30) -> dict[str, Any]:
        """Насколько цифрам дневника можно верить — и что именно чинить.

        Доля без списка продуктов бесполезна: «у тебя 63 % оценок» не говорит,
        с чего начать. Поэтому отдаём и то, какие продукты эту долю дают —
        по ним видно, чью этикетку стоит снять, чтобы цифры перестали врать.
        """
        n = _clamp(days, 1, MAX_DAYS, 30)
        est_kcal, total_kcal = await self._meals.estimate_share(self._uid, days=n)
        share = round(est_kcal / total_kcal, 2) if total_kcal else 0.0
        out: dict[str, Any] = {
            "days": n,
            "share_estimated": share,
            "kcal_estimated": round(est_kcal),
            "kcal_total": round(total_kcal),
            "note": "Оценка — плотность придумала модель. Этикетка — цифры с упаковки.",
        }
        if self._products is None:
            return out
        # ponytail: пересечение двух готовых запросов вместо третьего с JOIN.
        # Потолок известен: список оценочных продуктов обрезан двумя сотнями,
        # и очень старая оценка в него не попадёт. Понадобится точнее — запрос
        # вклада с фильтром по is_estimate.
        estimated = {p.id for p in await self._products.list_estimated(200)}
        rows = await self._meals.contributions(self._uid, days=n, limit=20, by="kcal")
        out["estimated_products"] = [
            {"name": name, "kcal": round(total), "times": times}
            for pid, name, total, times in rows
            if pid in estimated
        ][:10]
        return out


    async def body_scans(self, limit: Any = 6) -> dict[str, Any]:
        """Замеры состава тела и темп между двумя последними (§6.24).

        Отдаём то, что нельзя вывести из веса: сколько ушло жира, а сколько —
        безжировой массы. На вопрос «не теряю ли мышцы» другого ответа нет.
        """
        if self._scans is None:
            return {"scans": [], "pace": None}
        rows = await self._scans.list_for(self._uid)
        rows = rows[-_clamp(limit, 1, 20, 6):]
        step = pace(rows[-2], rows[-1]) if len(rows) >= 2 else None
        return {
            "scans": [
                {
                    "date": s.measured_at.date().isoformat(),
                    "weight_kg": s.weight_kg,
                    "body_fat_kg": s.body_fat_kg,
                    "body_fat_pct": s.body_fat_pct,
                    "fat_free_kg": s.fat_free_kg,
                    "skeletal_muscle_kg": s.skeletal_muscle_kg,
                    "smi": s.smi,
                    "inbody_score": s.inbody_score,
                }
                for s in rows
            ],
            "pace": None if step is None else {
                "days": step.days,
                "fat_kg_per_month": step.fat_kg_per_month,
                "lean_kg_per_month": step.lean_kg_per_month,
                # Меньше этого — погрешность прибора, а не изменение.
                "noise_kg_per_month": step.noise_kg_per_month,
            },
        }


def _pfc(m: Any) -> dict[str, float]:
    return {"protein_g": round(m.protein, 1), "fat_g": round(m.fat, 1),
            "carb_g": round(m.carb, 1)}


def _parse_day(value: Any, today: date) -> date:
    """Дата от модели. Не разобрали — сегодня: отказ на кривом аргументе стоил
    бы человеку ответа, а «сегодня» это осмысленный запасной вариант.

    `today` — по поясу человека: сервер живёт в UTC, и с полуночи до 7 утра его
    «сегодня» было бы вчерашним днём (§6.21.2)."""
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            pass
    return today


def demo() -> None:
    """Runnable-чек: аргументы от модели зажимаются, мусор не роняет."""
    assert _clamp("7", 1, 90, 14) == 7
    assert _clamp(500, 1, 90, 14) == 90        # потолок
    assert _clamp(0, 1, 90, 14) == 1           # пол
    assert _clamp("вчера", 1, 90, 14) == 14    # мусор → дефолт
    assert _clamp(None, 1, 90, 14) == 14
    today = date(2026, 10, 7)  # «сегодня» человека, а не сервера
    assert _parse_day("2026-09-01", today) == date(2026, 9, 1)
    assert _parse_day("2026-09-01T10:00:00", today) == date(2026, 9, 1)
    assert _parse_day("позавчера", today) == today
    assert _parse_day(None, today) == today
    print("данные ассистента: ок")


if __name__ == "__main__":
    demo()
