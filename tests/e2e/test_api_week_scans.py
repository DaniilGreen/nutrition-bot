"""Недельный баланс и замеры InBody через API (MINIAPP-SPEC §6.23–6.24).

Тест держит решения владельца: цели по замеру меняются только кнопкой,
несошедшийся бланк не сохраняется без подтверждения, два фото одного бланка —
один замер, пустой день не входит в баланс недели.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.application.dto import ScanReading
from app.application.entities import Meal, WeightLog
from app.domain.goals import Goal
from app.domain.value_objects import Macros
from tests.fakes import StubVision
from tests.initdata_fixtures import valid

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 500

# Бланк владельца — как прочла бы модель. Прибор печатает М.Д.Г, внизу история.
BLANK = ScanReading(
    brand="InBody", device="InBody270", date_text="09.20.2026 15:44", language="vi",
    other_dates=("01.13.26 13:15",),
    height_cm=180, age=30, sex="male", weight_kg=91.8, skeletal_muscle_kg=41.4,
    body_fat_kg=19.8, body_fat_pct=21.6, fat_free_kg=72.0, water_l=52.8,
    protein_kg=14.3, minerals_kg=4.86, visceral_level=9, bmr_kcal=1925,
    smi=9.4, inbody_score=82,
    lean_arm_l_kg=4.47, lean_arm_r_kg=4.29, lean_trunk_kg=33.2,
    lean_leg_l_kg=10.62, lean_leg_r_kg=10.57,
    fat_arm_l_kg=1.1, fat_arm_r_kg=1.1, fat_trunk_kg=11.2,
    fat_leg_l_kg=2.6, fat_leg_r_kg=2.6,
)


@pytest.fixture
def client(fake_container):
    from app.main_api import app
    from app.web.drafts import DraftStore
    from app.web.limits import IdempotencyStore, RateLimiter

    app.state.container = fake_container
    app.state.drafts = DraftStore()
    app.state.idempotency = IdempotencyStore()
    app.state.limiter = RateLimiter()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _hdr(**extra):
    return {"X-Telegram-Init-Data": valid(), "Idempotency-Key": uuid4().hex, **extra}


async def _login(c) -> int:
    return int((await c.get("/api/me", headers=_hdr())).json()["user"]["id"])


async def _cut_goal(fc, uid):
    await fc.users.set_goal(uid, Goal(2176, 194, 72, 188, "cut"))


async def _eat(fc, uid, when: datetime, kcal: float):
    await fc.meals.add(Meal(user_id=uid, eaten_at=when, input_type="text", raw_input="x",
                            items=[], source_confidence=1.0, totals=Macros(kcal, 0, 0, 0)))


# --- неделя -------------------------------------------------------------


async def test_неделя_в_дне_и_отдельно(client, fake_container):
    async with client as c:
        uid = await _login(c)
        await _cut_goal(fake_container, uid)
        user = (await fake_container.users.list_active())[0]
        user.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        now = datetime.now(UTC)
        await _eat(fake_container, uid, now - timedelta(days=1), 1800)
        day = (await c.get("/api/day", headers=_hdr())).json()
        wk = (await c.get("/api/week", headers=_hdr())).json()["week"]
        past = (await c.get("/api/day?days_ago=1", headers=_hdr())).json()
        bad = await c.get("/api/week?weeks_ago=9", headers=_hdr())
    assert day["week_balance"]["goal_kcal"] == 2176
    assert wk["days"] and len(wk["days"]) == 7
    assert past["week_balance"] is None
    assert bad.status_code == 400
    # Вчера в балансе, если вчера на этой неделе.
    today = now.astimezone().date()
    if today.weekday() > 0:
        assert wk["balance_kcal"] == 1800 - 2176


async def test_прошлая_неделя_без_оставшихся_дней(client, fake_container):
    async with client as c:
        uid = await _login(c)
        await _cut_goal(fake_container, uid)
        wk = (await c.get("/api/week?weeks_ago=1", headers=_hdr())).json()["week"]
    assert wk["remaining_days"] == 0 and wk["per_day_kcal"] is None


# --- InBody -------------------------------------------------------------


async def _scan_card(c, fc, reading=BLANK):
    fc.vision = StubVision(None, scan=reading)
    r = await c.post("/api/meals/photo", content=JPEG, headers=_hdr())
    assert r.status_code == 200, r.text
    return r.json()


async def test_бланк_читается_сходится_и_сохраняется(client, fake_container):
    """AC-MA-53."""
    async with client as c:
        uid = await _login(c)
        await _cut_goal(fake_container, uid)
        d = await _scan_card(c, fake_container)
        assert d["kind"] == "body_scan"
        assert d["bad"] == [] and not d["needs_confirm"]
        assert d["scan"]["date"] == "2026-09-20" and d["scan"]["time"] == "15:44"
        r = await c.post("/api/body-scans", headers=_hdr(),
                         json={"draft_id": d["draft_id"], "fields": {}})
        assert r.status_code == 200, r.text
        saved = r.json()
        goal_after = (await c.get("/api/me", headers=_hdr())).json()["goal"]
    assert saved["updated"] is False
    # AC-MA-56: предложение есть, цели не менялись.
    prop = saved["proposal"]
    assert prop["current"]["protein_g"] == 194
    assert prop["proposed"]["protein_g"] == 173
    assert prop["bmr_kcal"] == 1925 and prop["changed"]
    assert goal_after["protein_g"] == 194
    # Вес замера лёг в ленту.
    logs = await fake_container.weights.history(uid, 10)
    assert [w.weight_kg for w in logs] == [91.8]


async def test_два_фото_одного_бланка_один_замер(client, fake_container):
    """AC-MA-54 / MA-INV-51."""
    async with client as c:
        await _login(c)
        first = await _scan_card(c, fake_container)
        await c.post("/api/body-scans", headers=_hdr(),
                     json={"draft_id": first["draft_id"], "fields": {}})
        second = await _scan_card(c, fake_container)
        assert second["existing"] is True
        r = await c.post("/api/body-scans", headers=_hdr(),
                         json={"draft_id": second["draft_id"], "fields": {}})
        ov = (await c.get("/api/body-scans", headers=_hdr())).json()
    assert r.json()["updated"] is True
    assert len(ov["scans"]) == 1
    assert len(await fake_container.weights.history(1, 10)) == 1


async def test_несошедшийся_бланк_без_подтверждения_не_пишется(client, fake_container):
    """AC-MA-55 / MA-INV-50."""
    from dataclasses import replace

    async with client as c:
        await _login(c)
        d = await _scan_card(c, fake_container, replace(BLANK, body_fat_kg=91.8))
        assert "body_fat_kg" in d["bad"] and d["needs_confirm"]
        no = await c.post("/api/body-scans", headers=_hdr(),
                          json={"draft_id": d["draft_id"], "fields": {}})
        assert no.status_code == 400 and no.json()["error"]["code"] == "scan_mismatch"
        # Правка поля — сохраняется без подтверждения; правка попадает в метрику.
        ok = await c.post("/api/body-scans", headers=_hdr(),
                          json={"draft_id": d["draft_id"], "fields": {"body_fat_kg": "19,8"}})
    assert ok.status_code == 200, ok.text
    assert fake_container.scans.corrected == ["body_fat_kg"]


async def test_подтверждение_пропускает(client, fake_container):
    from dataclasses import replace

    async with client as c:
        await _login(c)
        d = await _scan_card(c, fake_container, replace(BLANK, bmr_kcal=1952))
        ok = await c.post("/api/body-scans", headers=_hdr(),
                          json={"draft_id": d["draft_id"], "fields": {}, "confirmed": True})
    assert ok.status_code == 200


async def test_чужой_бланк_просит_подтверждения(client, fake_container):
    """AC-MA-61."""
    from dataclasses import replace

    async with client as c:
        uid = await _login(c)
        await fake_container.weights.add(
            WeightLog(user_id=uid, measured_at=datetime(2026, 5, 1, tzinfo=UTC), weight_kg=91.0))
        d = await _scan_card(c, fake_container, replace(
            BLANK, weight_kg=58.0, body_fat_kg=14.0, fat_free_kg=44.0, body_fat_pct=24.1,
            skeletal_muscle_kg=24.0, water_l=32.0, protein_kg=8.8, minerals_kg=3.2,
            bmr_kcal=1320, height_cm=160, sex="female"))
    assert d["foreign"] and d["needs_confirm"]


async def test_дата_без_подсказок_неоднозначна(client, fake_container):
    """AC-MA-62."""
    from dataclasses import replace

    async with client as c:
        await _login(c)
        d = await _scan_card(c, fake_container,
                             replace(BLANK, date_text="05.03.2026 08:10", language="en",
                                     other_dates=()))
        assert d["date_ambiguous"] and d["needs_confirm"]
        # Человек подтвердил дату полем — сохраняется без «Всё верно».
        ok = await c.post("/api/body-scans", headers=_hdr(), json={
            "draft_id": d["draft_id"], "fields": {"date": "2026-05-03"}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["scan"]["date"] == "2026-05-03"


async def test_история_бланка_решает_порядок_даты(client, fake_container):
    """AC-MA-64: вьетнамский InBody печатает М.Д — это видно по истории внизу бланка."""
    from dataclasses import replace

    async with client as c:
        await _login(c)
        d = await _scan_card(c, fake_container, replace(
            BLANK, date_text="05.03.2026 08:10"))
    assert d["scan"]["date"] == "2026-05-03"
    assert not d["date_ambiguous"] and not d["needs_confirm"]


async def test_сегменты_и_индекс_доезжают_до_карточки(client, fake_container):
    """AC-MA-66: связь «индекс × рост² = мышцы конечностей» сошлась."""
    async with client as c:
        await _login(c)
        d = await _scan_card(c, fake_container)
    assert d["bad"] == [] and not d["needs_confirm"] and not d["dropped_segments"]
    assert d["scan"]["smi"] == 9.4 and d["scan"]["inbody_score"] == 82
    assert d["scan"]["segments"]["arm_l"] == {"lean": 4.47, "fat": 1.1}
    assert d["scan"]["segments"]["trunk"]["lean"] == 33.2


async def test_кривые_сегменты_отбрасываются_а_не_блокируют(client, fake_container):
    """AC-MA-67, MA-INV-57: сохранение не гаснет из-за декоративного блока."""
    from dataclasses import replace

    async with client as c:
        await _login(c)
        d = await _scan_card(c, fake_container, replace(BLANK, lean_leg_r_kg=1.5))
        assert d["bad"] == [] and not d["needs_confirm"] and d["dropped_segments"]
        assert d["scan"]["segments"] is None
        ok = await c.post("/api/body-scans", headers=_hdr(),
                          json={"draft_id": d["draft_id"], "fields": {}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["scan"]["segments"] is None
    assert ok.json()["scan"]["smi"] == 9.4      # индекс сам по себе не пострадал


async def test_неполные_сегменты_не_сохраняются(client, fake_container):
    """AC-MA-68: четыре из пяти всегда провалят сумму — значит их нет."""
    from dataclasses import replace

    async with client as c:
        await _login(c)
        d = await _scan_card(c, fake_container, replace(BLANK, fat_trunk_kg=None))
    assert d["scan"]["segments"] is None and not d["needs_confirm"]


async def test_темп_и_срок_в_итогах(client, fake_container):
    """AC-MA-72, AC-MA-74 и AC-MA-76 на живом API."""
    from dataclasses import replace

    async with client as c:
        uid = await _login(c)
        await _cut_goal(fake_container, uid)
        for days_ago, w, f, ffm in ((34, 93.4, 22.8, 70.6), (0, 91.6, 20.9, 70.7)):
            d = await _scan_card(c, fake_container, replace(
                BLANK, date_text=(datetime.now(UTC) - timedelta(days=days_ago))
                .strftime("%m.%d.%Y 08:00"), weight_kg=w, body_fat_kg=f, fat_free_kg=ffm,
                body_fat_pct=round(f / w * 100, 1), smi=None, skeletal_muscle_kg=None,
                water_l=None, protein_kg=None, minerals_kg=None, bmr_kcal=None))
            r = await c.post("/api/body-scans", headers=_hdr(),
                             json={"draft_id": d["draft_id"], "fields": {}, "confirmed": True})
            assert r.status_code == 200, r.text
        one = (await c.get("/api/body-scans", headers=_hdr())).json()
        assert one["pace"]["fat_kg_per_month"] == -1.7
        assert one["pace"]["noise_kg_per_month"] == 1.3 and one["pace"]["meaningful"]
        # Цели по проценту жира нет — срока тоже нет.
        assert one["forecast"] == {"verdict": "no_target", "target_pct": None, "months": None}

        await c.patch("/api/settings/prefs", headers=_hdr(), json={"target_fat_pct": 15})
        two = (await c.get("/api/body-scans", headers=_hdr())).json()
    assert two["forecast"]["verdict"] == "ok"
    assert 4.5 < two["forecast"]["months"] < 5.5


async def test_несошедшийся_бланк_перечитывается(client, fake_container):
    """Числа не сошлись — читаем бланк второй раз и берём связное чтение."""
    from dataclasses import replace

    async with client as c:
        await _login(c)
        fake_container.vision = StubVision(
            None, scans=[replace(BLANK, body_fat_kg=91.8), BLANK])
        r = await c.post("/api/meals/photo", content=JPEG, headers=_hdr())
    d = r.json()
    assert fake_container.vision.scan_calls == 2
    assert d["bad"] == [] and not d["needs_confirm"]
    assert d["scan"]["body_fat_kg"] == 19.8


async def test_нечитаемая_дата_не_отказ(client, fake_container):
    """AC-MA-65, MA-INV-55: бланк с хорошими числами из-за даты не выбрасываем."""
    from dataclasses import replace

    async with client as c:
        await _login(c)
        d = await _scan_card(c, fake_container, replace(BLANK, date_text="—"))
        assert d["kind"] == "body_scan" and d["date_problem"] == "unread"
        assert d["needs_confirm"]
        no = await c.post("/api/body-scans", headers=_hdr(),
                          json={"draft_id": d["draft_id"], "fields": {}})
        assert no.status_code == 400 and no.json()["error"]["code"] == "scan_mismatch"
        ok = await c.post("/api/body-scans", headers=_hdr(), json={
            "draft_id": d["draft_id"], "fields": {"date": "2026-09-20"}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["scan"]["date"] == "2026-09-20"


@pytest.mark.parametrize(("patch", "message"), [
    ({"brand": "Tanita", "device": "BC-545"}, "InBody"),
    ({"weight_kg": None}, "вес"),
])
async def test_бланк_отказы(client, fake_container, patch, message):
    from dataclasses import replace

    async with client as c:
        await _login(c)
        d = await _scan_card(c, fake_container, replace(BLANK, **patch))
    assert d["kind"] == "unknown" and message in d["message"]


async def test_дата_в_будущем_отказ(client, fake_container):
    from dataclasses import replace

    async with client as c:
        await _login(c)
        d = await _scan_card(c, fake_container, replace(BLANK, date_text="05.03.2099 10:00"))
        r = await c.post("/api/body-scans", headers=_hdr(),
                         json={"draft_id": d["draft_id"], "fields": {}, "confirmed": True})
    assert d["date_problem"] == "future"
    assert r.status_code == 400 and r.json()["error"]["code"] == "scan_future"


async def test_применить_цели_только_кнопкой_и_в_историю(client, fake_container):
    """MA-INV-52, AC-MA-59: применённая цель не переписывает прошлые дни недели."""
    async with client as c:
        uid = await _login(c)
        await _cut_goal(fake_container, uid)
        d = await _scan_card(c, fake_container)
        await c.post("/api/body-scans", headers=_hdr(),
                     json={"draft_id": d["draft_id"], "fields": {}})
        r = await c.post("/api/body-scans/apply-goal", headers=_hdr())
        me = (await c.get("/api/me", headers=_hdr())).json()
    assert r.status_code == 200, r.text
    assert me["goal"]["protein_g"] == 173
    sources = [s for _, _, s in fake_container.users.history[uid]]
    assert sources[-1] == "scan"


async def test_без_замера_применять_нечего(client, fake_container):
    async with client as c:
        await _login(c)
        r = await c.post("/api/body-scans/apply-goal", headers=_hdr())
    assert r.status_code == 400 and r.json()["error"]["code"] == "no_scan"


async def test_удаление_замера(client, fake_container):
    async with client as c:
        await _login(c)
        d = await _scan_card(c, fake_container)
        s = (await c.post("/api/body-scans", headers=_hdr(),
                          json={"draft_id": d["draft_id"], "fields": {}})).json()
        gone = await c.delete(f"/api/body-scans/{s['scan']['id']}", headers=_hdr())
        again = await c.delete(f"/api/body-scans/{s['scan']['id']}", headers=_hdr())
    assert gone.status_code == 200 and again.status_code == 404


async def test_сводка_после_двух_замеров(client, fake_container):
    """AC-MA-57: 60 дней, записаны все дни — расход с кнопкой."""
    from dataclasses import replace

    async with client as c:
        uid = await _login(c)
        await _cut_goal(fake_container, uid)
        first = await _scan_card(c, fake_container, replace(
            BLANK, date_text=(datetime.now(UTC) - timedelta(days=60)).strftime("%m.%d.%Y 08:00")))
        await c.post("/api/body-scans", headers=_hdr(),
                     json={"draft_id": first["draft_id"], "fields": {}})
        for i in range(61):
            await _eat(fake_container, uid, datetime.now(UTC) - timedelta(days=i, hours=1), 2100)
        second = await _scan_card(c, fake_container, replace(
            BLANK, date_text=datetime.now(UTC).strftime("%m.%d.%Y 00:05"),
            weight_kg=88.8, body_fat_kg=17.4, fat_free_kg=71.4, body_fat_pct=19.6,
            water_l=52.3, protein_kg=14.2, minerals_kg=4.85, bmr_kcal=1912,
            skeletal_muscle_kg=41.0))
        assert second["bad"] == [], second
        await c.post("/api/body-scans", headers=_hdr(),
                     json={"draft_id": second["draft_id"], "fields": {}})
        ov = (await c.get("/api/body-scans", headers=_hdr())).json()
    assert len(ov["scans"]) == 2
    assert ov["change"]["body_fat_kg"] == -2.4
    assert ov["energy"]["verdict"] == "ok", ov["energy"]
    assert ov["energy"]["proposable"] is True
    assert ov["proposal"]["base"] == "energy"
    assert ov["diary"]["actual_kg"] == -3.0


async def test_перепроверка_после_правки(client, fake_container):
    from dataclasses import replace

    async with client as c:
        await _login(c)
        d = await _scan_card(c, fake_container, replace(BLANK, body_fat_kg=91.8))
        r = await c.post("/api/body-scans/check", headers=_hdr(),
                         json={"draft_id": d["draft_id"], "fields": {"body_fat_kg": "19.8"}})
        bad = await c.post("/api/body-scans/check", headers=_hdr(),
                           json={"draft_id": d["draft_id"], "fields": {"body_fat_kg": "много"}})
        gone = await c.post("/api/body-scans/check", headers=_hdr(),
                            json={"draft_id": "нет", "fields": {}})
    assert r.status_code == 200 and r.json()["bad"] == [] and not r.json()["needs_confirm"]
    assert r.json()["draft_id"] == d["draft_id"]
    assert bad.status_code == 400 and gone.status_code == 400


async def test_сверка_по_двум_близким_замерам(client, fake_container):
    """Замеры в 10 днях: расхода нет (окно короче 28), но «ждали — показали» есть."""
    from dataclasses import replace

    async with client as c:
        uid = await _login(c)
        await _cut_goal(fake_container, uid)
        for i in range(11):
            await _eat(fake_container, uid, datetime.now(UTC) - timedelta(days=i, hours=1), 1900)
        for days_ago, w, f, ffm in ((10, 91.8, 19.8, 72.0), (0, 90.8, 19.0, 71.8)):
            d = await _scan_card(c, fake_container, replace(
                BLANK, date_text=(datetime.now(UTC) - timedelta(days=days_ago))
                .strftime("%m.%d.%Y 00:05"), weight_kg=w, body_fat_kg=f, fat_free_kg=ffm,
                body_fat_pct=None, water_l=None, bmr_kcal=None, skeletal_muscle_kg=None))
            r = await c.post("/api/body-scans", headers=_hdr(),
                             json={"draft_id": d["draft_id"], "fields": {}, "confirmed": True})
            assert r.status_code == 200, r.text
        ov = (await c.get("/api/body-scans", headers=_hdr())).json()
    assert ov["energy"] is None
    assert ov["diary"]["actual_kg"] == -1.0
    assert ov["diary"]["verdict"] in {"agree", "less_lost", "more_lost"}
