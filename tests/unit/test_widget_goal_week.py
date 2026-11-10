"""«🎯 Цель» виджетом и листание «📈 Недели» (FR-49.9, FR-49.5, D-72). Фейки: без БД и Telegram."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.application.entities import Meal, MealItem
from app.bot.ui import screens
from app.bot.ui.widgets import LIMIT, goal_data
from app.bot.widgets import goal as wgoal
from app.bot.widgets import live, template
from app.bot.widgets import week as wweek
from app.domain.goals import Goal, rebalance
from app.domain.value_objects import Macros


def test_цель_те_же_числа_что_rich() -> None:
    """Граммы и г/кг — как в `goal_screen`; доли ккал (Б×4, Ж×9, У×4) считает Python."""
    g = Goal(2098, 170, 74, 188, "cut")
    d = json.loads(goal_data(g, 89.8, "🎯 Твоя дневная норма"))
    html = screens.goal_screen(g, 89.8)
    assert d["k"] == 2098 and d["tg"] == "cut" and d["wk"] == 1
    assert [r[0] for r in d["m"]] == [170, 74, 188]
    assert d["m"][0][1] == " (1.9\u00a0г/кг)" and d["m"][1][1] == " (0.82\u00a0г/кг)"
    assert "(1.9 г/кг)" in html and "(0.82 г/кг)" in html
    assert [r[2] for r in d["m"]] == [32, 32, 36] and len(goal_data(g, 89.8).encode()) <= LIMIT
    assert json.loads(goal_data(g, None))["wk"] == 0


@pytest.fixture
def world(fake_container):  # noqa: ANN001, ANN201
    async def make(weight: float | None = 90.0):  # noqa: ANN202
        assistant, users = fake_container.assistant_for(None)
        u = await users.get_or_create(telegram_id=5, name="x", locale="ru", timezone="UTC")
        if weight:
            await assistant._weights.log(u, weight)
        live.EXTRA.set({"users": users, "scans": None})
        return assistant, users, u

    return make


async def test_смена_цели_тем_же_rebalance(world) -> None:  # noqa: ANN001
    assistant, users, u = await world()
    before = await assistant.current_goal(u)
    out = await wgoal.on_goal(5, 7, "g:t:bulk:k1k1", u, assistant)
    assert out.reply["ok"] == 1 and out.reply["tg"] == "bulk"
    saved = await assistant.current_goal(u)
    assert saved == rebalance(before.daily_kcal, 90.0, "bulk")  # калории те же — делится иначе
    assert out.reply["k"] == round(before.daily_kcal)
    for bad in ("g:t:fat:k2k2", "g:t:cut", "g:x:cut:k3k3"):
        assert (await wgoal.on_goal(5, 7, bad, u, assistant)).reply["err"] == "bad"


async def test_без_веса_цель_не_сменить(world) -> None:  # noqa: ANN001
    assistant, _, u = await world(weight=None)
    assert (await wgoal.on_goal(5, 7, "g:t:cut:k1k1", u, assistant)).reply == {"err": "noweight"}


def _meal(uid: int, at: datetime, kcal: float) -> Meal:
    m = Macros(kcal, kcal / 15, kcal / 30, kcal / 8)
    return Meal(user_id=uid, eaten_at=at, input_type="text", raw_input="x",
                items=[MealItem(name_snapshot="еда", grams=300, macros=m)],
                source_confidence=1.0, totals=m)


async def test_листание_недель(world, fake_container) -> None:  # noqa: ANN001
    """`k:1` — прошлая неделя той же `week_balance`, что «Итоги»; `k:0` — текущая; неделя без
    записей — `empty`; дальше 52 и кривое — `bad`."""
    assistant, _, u = await world()
    now = datetime.now(UTC)
    monday = (now - timedelta(days=now.weekday() + 7)).replace(hour=12, minute=0)
    for i in range(7):
        await fake_container.meals.add(_meal(u.id, monday + timedelta(days=i), 1900 + i * 10))
    await fake_container.meals.add(_meal(u.id, now - timedelta(minutes=5), 600))
    r = (await wweek.on_week(5, 7, "k:1", u, assistant)).reply
    w = await assistant.week_balance(u, 1)
    assert r["wa"] == 1 and len(r["c"]) == 7 and r["t"] == screens._span(w.start, w.end)
    assert [c[2] for c in r["c"]] == [1900 + i * 10 for i in range(7)]
    assert (await wweek.on_week(5, 7, "k:0", u, assistant)).reply.get("wa") in (0, None)
    assert (await wweek.on_week(5, 7, "k:6", u, assistant)).reply == {"err": "empty"}
    for bad in ("k:53", "k:x", "k:-1"):
        assert (await wweek.on_week(5, 7, bad, u, assistant)).reply == {"err": "bad"}
    html = template("week")
    assert "W.request('k:'+n)" in html and "d.wa" not in html and "D.wa" in html
