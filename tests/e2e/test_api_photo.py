"""Фото блюда через API (FR-34, MINIAPP-SPEC §6.22, AC-MA-43…48).

Фото раскладывает тарелку, вес — от человека, КБЖУ — обычный разбор фразы.
Тест держит именно эту границу: в разбор уходит текст из имён ЧЕРНОВИКА и
граммов карточки, а не то, что прислал клиент.
"""
from __future__ import annotations

import re
from urllib.parse import quote
from uuid import uuid4

import httpx
import pytest

from app.application.dto import Intent, ParsedItem, ParsedMessage, PlateReading, Unit
from app.domain.plate import Component
from app.domain.spend import Usage
from tests.fakes import InMemorySpendRepo, StubVision
from tests.initdata_fixtures import valid

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 500


class EchoNlu:
    """Разбирает «имя N г, …» как настоящий разбор: по позиции на кусок, с явным весом.
    Плотность — 100 ккал/100 г, у масла — 900: хватает, чтобы видеть, что масло дошло."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def parse(self, text: str, known_keys: object) -> ParsedMessage:
        self.texts.append(text)
        items = []
        for part in text.split(", "):
            m = re.fullmatch(r"(.+) (\d+(?:\.\d+)?) г", part)
            assert m, f"разбор получил не фразу из карточки: {part!r}"
            name, grams = m.group(1), float(m.group(2))
            kcal = 900.0 if "масло" in name else 100.0
            items.append(ParsedItem(
                name=name, canonical_key=name.replace(" ", "_"), unit=Unit.GRAM,
                grams=grams, kcal_per_100g=kcal, protein_per_100g=5.0,
                fat_per_100g=100.0 if kcal == 900.0 else 3.0, carb_per_100g=10.0,
            ))
        return ParsedMessage(intent=Intent.MEAL, items=items)

    async def parse_samples(self, text: str, known_keys: object) -> list[ParsedMessage]:
        return [await self.parse(text, known_keys)]


def _plate(**kw) -> PlateReading:
    base = dict(
        kind="dish", dish="паста с курицей",
        components=[
            Component("макароны отварные", 0.6, 250),
            Component("курица жареная", 0.3, 100),
            Component("соус сливочный", 0.1, 40),
        ],
        ask_fat=True, fat_kind="масло сливочное", ask_dressing=False,
    )
    base.update(kw)
    return PlateReading(**base)


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


def _all(d, grams=None):
    return [{"i": c["i"], "grams": (grams or {}).get(c["i"], c["grams"])}
            for c in d["components"]]


async def test_фото_без_подписи_даёт_карточку_на_глаз_и_пишет_оценкой(client, fake_container):
    """AC-MA-43."""
    fake_container.vision = StubVision(None, _plate())
    nlu = EchoNlu()
    fake_container.nlu = nlu
    async with client as c:
        r = await c.post("/api/meals/photo", content=JPEG, headers=_hdr())
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["kind"] == "photo" and d["weighed"] is False
        assert [x["grams"] for x in d["components"]] == [250, 100, 40]
        assert d["ask_fat"] is True and "kcal" not in str(d)
        r2 = await c.post("/api/meals/photo/commit", headers=_hdr(), json={
            "draft_id": d["draft_id"], "components": _all(d), "fat": "none",
        })
    assert r2.status_code == 200, r2.text
    meal = r2.json()["meal"]
    assert nlu.texts == ["макароны отварные 250 г, курица жареная 100 г, соус сливочный 40 г"]
    assert meal["kcal"] == pytest.approx(390, abs=1)          # 390 г × 100 ккал/100 г
    assert all(i["is_estimate"] for i in meal["items"])
    assert all("вес на глаз" in i["name"] for i in meal["items"])
    assert meal["input_type"] == "photo"


async def test_вес_в_подписи_делится_по_долям(client, fake_container):
    """AC-MA-44."""
    fake_container.vision = StubVision(None, _plate(caption_total_g=780))
    fake_container.nlu = EchoNlu()
    async with client as c:
        d = (await c.post("/api/meals/photo", content=JPEG,
                          headers=_hdr(**{"X-Caption": quote("420 и 780")}))).json()
        assert d["weighed"] is True and d["total_g"] == 780
        assert [x["grams"] for x in d["components"]] == [500, 200, 80]
        r2 = await c.post("/api/meals/photo/commit", headers=_hdr(), json={
            "draft_id": d["draft_id"], "components": _all(d), "weighed": True,
        })
    items = r2.json()["meal"]["items"]
    assert sum(i["grams"] for i in items) == pytest.approx(780)
    assert not any("на глаз" in i["name"] for i in items)


async def test_выдуманный_моделью_вес_не_считается_взвешенным(client, fake_container):
    """Модель «прочла» 400 в подписи, где этого числа нет, — это не вес от человека."""
    fake_container.vision = StubVision(None, _plate(caption_total_g=400))
    async with client as c:
        d = (await c.post("/api/meals/photo", content=JPEG,
                          headers=_hdr(**{"X-Caption": quote("вкусно")}))).json()
    assert d["weighed"] is False and d["total_g"] is None
    assert [x["grams"] for x in d["components"]] == [250, 100, 40]


async def test_вес_позиции_из_подписи_остаётся_а_остаток_делится(client, fake_container):
    comps = _plate().components
    comps[1] = Component("курица жареная", 0.3, 150, fixed=True)
    fake_container.vision = StubVision(None, _plate(components=comps, caption_total_g=400))
    async with client as c:
        d = (await c.post("/api/meals/photo", content=JPEG,
                          headers=_hdr(**{"X-Caption": quote("400, курица 150")}))).json()
    grams = [x["grams"] for x in d["components"]]
    assert grams[1] == 150 and sum(grams) == pytest.approx(400, abs=1)


async def test_чип_масла_добавляет_позицию(client, fake_container):
    """AC-MA-45: «Ложка» → 10 г того масла, что назвала модель."""
    fake_container.vision = StubVision(None, _plate())
    nlu = EchoNlu()
    fake_container.nlu = nlu
    async with client as c:
        d = (await c.post("/api/meals/photo", content=JPEG, headers=_hdr())).json()
        r2 = await c.post("/api/meals/photo/commit", headers=_hdr(), json={
            "draft_id": d["draft_id"], "components": _all(d), "fat": "spoon",
            "dressing": "mayo",   # заправку модель не спрашивала — игнорируем
        })
    names = [i["name"] for i in r2.json()["meal"]["items"]]
    assert any(n.startswith("масло сливочное") for n in names)
    assert not any("майонез" in n for n in names)
    assert nlu.texts[0].endswith("масло сливочное 10 г")


async def test_убранная_позиция_не_пишется_и_правка_граммов_доходит(client, fake_container):
    fake_container.vision = StubVision(None, _plate())
    nlu = EchoNlu()
    fake_container.nlu = nlu
    async with client as c:
        d = (await c.post("/api/meals/photo", content=JPEG, headers=_hdr())).json()
        await c.post("/api/meals/photo/commit", headers=_hdr(), json={
            "draft_id": d["draft_id"],
            "components": [{"i": 0, "grams": 300}, {"i": 1, "grams": 90}],
        })
    assert nlu.texts == ["макароны отварные 300 г, курица жареная 90 г"]


async def test_этикетка_через_ту_же_камеру(client, fake_container):
    """AC-MA-46."""
    item = ParsedItem(name="йогурт 2%", canonical_key="", unit=Unit.GRAM,
                      kcal_per_100g=60, protein_per_100g=4, fat_per_100g=2,
                      carb_per_100g=6.5)
    fake_container.vision = StubVision(item)
    async with client as c:
        d = (await c.post("/api/meals/photo", content=JPEG, headers=_hdr())).json()
        assert d["kind"] == "label"
        r2 = await c.post("/api/meals/clarify", headers=_hdr(),
                          json={"draft_id": d["draft_id"], "answer": "200"})
    assert r2.json()["meal"]["kcal"] == pytest.approx(120, abs=0.5)


async def test_не_еда_спокойный_отказ(client, fake_container):
    fake_container.vision = StubVision(None, PlateReading(kind="none"))
    async with client as c:
        r = await c.post("/api/meals/photo", content=JPEG, headers=_hdr())
    assert r.status_code == 200
    assert r.json()["kind"] == "unknown"


async def test_лимит_не_пускает_фото_но_пускает_текст(client, fake_container):
    """AC-MA-47 / MA-INV-45."""
    vision = StubVision(None, _plate())
    fake_container.vision = vision
    fake_container.nlu = EchoNlu()
    spend = InMemorySpendRepo()
    await spend.add(user_id=None, model="gpt-5.1", purpose="nlu", input_tokens=0,
                    output_tokens=0, cost_usd=Usage("gpt-5.1", 0, 0).cost_usd + 99)
    fake_container.spend = spend
    async with client as c:
        r = await c.post("/api/meals/photo", content=JPEG, headers=_hdr())
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "ai_limit"
        assert vision.photo_calls == 0
        t = await c.post("/api/meals", json={"text": "рис 100 г"}, headers=_hdr())
    assert t.status_code == 200 and t.json()["kind"] == "logged"


@pytest.mark.parametrize("rows", [
    [],
    [{"i": 0, "grams": 0}],
    [{"i": 0, "grams": 9000}],
    [{"i": 0, "grams": 3000}, {"i": 1, "grams": 2500}],
    [{"i": 7, "grams": 100}],
    [{"i": 0, "grams": "много"}],
])
async def test_кривые_граммы_отказ_и_черновик_жив(client, fake_container, rows):
    """AC-MA-48 / MA-INV-44."""
    fake_container.vision = StubVision(None, _plate())
    fake_container.nlu = EchoNlu()
    async with client as c:
        d = (await c.post("/api/meals/photo", content=JPEG, headers=_hdr())).json()
        bad = await c.post("/api/meals/photo/commit", headers=_hdr(),
                           json={"draft_id": d["draft_id"], "components": rows})
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] in {"empty_plate", "bad_grams"}
        ok = await c.post("/api/meals/photo/commit", headers=_hdr(),
                          json={"draft_id": d["draft_id"], "components": _all(d)})
    assert ok.status_code == 200 and ok.json()["kind"] == "logged"


async def test_черновик_одноразовый_и_чужой_не_отдаётся(client, fake_container):
    fake_container.vision = StubVision(None, _plate())
    fake_container.nlu = EchoNlu()
    async with client as c:
        d = (await c.post("/api/meals/photo", content=JPEG, headers=_hdr())).json()
        body = {"draft_id": d["draft_id"], "components": _all(d)}
        first = await c.post("/api/meals/photo/commit", headers=_hdr(), json=body)
        assert first.status_code == 200
        again = await c.post("/api/meals/photo/commit", headers=_hdr(), json=body)
    assert again.status_code == 400 and again.json()["error"]["code"] == "draft_gone"


async def test_имя_из_черновика_не_ломает_разбор(client, fake_container):
    """MA-INV-47: запятая и цифры в имени от модели не порождают лишних позиций."""
    comps = [Component("курица, 150г; с овощами", 1.0, 200)]
    fake_container.vision = StubVision(None, _plate(components=comps, ask_fat=False))
    nlu = EchoNlu()
    fake_container.nlu = nlu
    async with client as c:
        d = (await c.post("/api/meals/photo", content=JPEG, headers=_hdr())).json()
        await c.post("/api/meals/photo/commit", headers=_hdr(),
                     json={"draft_id": d["draft_id"], "components": _all(d), "fat": "lots"})
    assert nlu.texts == ["курица с овощами 200 г"]


async def test_без_vision_понятный_отказ(client, fake_container):
    fake_container.vision = None
    async with client as c:
        r = await c.post("/api/meals/photo", content=JPEG, headers=_hdr())
    assert r.json()["kind"] == "unknown"


@pytest.mark.parametrize("path", ["/api/meals/photo", "/api/meals/photo/commit"])
async def test_ключ_записи_обязателен(client, fake_container, path):
    """§7.5: снимок — самый дорогой вызов, запись — удвоенные калории при повторе."""
    fake_container.vision = StubVision(None, _plate())
    hdr = {"X-Telegram-Init-Data": valid()}
    async with client as c:
        r = await (c.post(path, content=JPEG, headers=hdr) if path.endswith("photo")
                   else c.post(path, json={"draft_id": "x", "components": []}, headers=hdr))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "no_idempotency_key"


async def test_ключ_вместо_имени_возвращается_к_словам_фразы(client, fake_container):
    """Замер 16.09.2026: разбор отдал «grapes_red_fresh» именем позиции."""
    class KeyNlu(EchoNlu):
        async def parse(self, text, known_keys):
            parsed = await super().parse(text, known_keys)
            parsed.items[1].name = "grapes_red_fresh"
            return parsed

    fake_container.vision = StubVision(None, _plate(ask_fat=False))
    fake_container.nlu = KeyNlu()
    async with client as c:
        d = (await c.post("/api/meals/photo", content=JPEG, headers=_hdr())).json()
        r = await c.post("/api/meals/photo/commit", headers=_hdr(),
                         json={"draft_id": d["draft_id"], "components": _all(d)})
    names = [i["name"] for i in r.json()["meal"]["items"]]
    assert names[1].startswith("курица жареная")


async def test_подпись_доходит_до_vision_и_не_живёт_в_адресе(client, fake_container):
    vision = StubVision(None, _plate())
    fake_container.vision = vision
    async with client as c:
        await c.post("/api/meals/photo", content=JPEG,
                     headers=_hdr(**{"X-Caption": quote("тарелка 420")}))
    assert vision.seen_caption == "тарелка 420"


async def test_чужой_черновик_не_записывается(client, fake_container):
    """Единственная проверка прав на черновик — привязка к user_id (MA-INV-43)."""
    fake_container.vision = StubVision(None, _plate())
    fake_container.nlu = EchoNlu()
    other = {"id": 778, "first_name": "Чужой"}
    import json as _json
    stranger = valid(user=_json.dumps(other, ensure_ascii=False, separators=(",", ":")))
    async with client as c:
        d = (await c.post("/api/meals/photo", content=JPEG, headers=_hdr())).json()
        r = await c.post("/api/meals/photo/commit",
                         headers={"X-Telegram-Init-Data": stranger, "Idempotency-Key": "x1"},
                         json={"draft_id": d["draft_id"], "components": _all(d)})
        assert r.status_code == 400 and r.json()["error"]["code"] == "draft_gone"
        mine = await c.post("/api/meals/photo/commit", headers=_hdr(),
                            json={"draft_id": d["draft_id"], "components": _all(d)})
    assert mine.status_code == 200


async def test_слишком_много_позиций_отказ(client, fake_container):
    fake_container.vision = StubVision(None, _plate())
    async with client as c:
        d = (await c.post("/api/meals/photo", content=JPEG, headers=_hdr())).json()
        r = await c.post("/api/meals/photo/commit", headers=_hdr(), json={
            "draft_id": d["draft_id"],
            "components": [{"i": i, "grams": 10} for i in range(9)],
        })
    assert r.status_code == 400


async def test_сбой_разбора_не_сжигает_карточку(client, fake_container):
    class Down(EchoNlu):
        fail = True

        async def parse(self, text, known_keys):
            if self.fail:
                self.fail = False
                raise RuntimeError("сеть")
            return await super().parse(text, known_keys)

    fake_container.vision = StubVision(None, _plate(ask_fat=False))
    fake_container.nlu = Down()
    from app.main_api import app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as c:
        d = (await c.post("/api/meals/photo", content=JPEG, headers=_hdr())).json()
        body = {"draft_id": d["draft_id"], "components": _all(d)}
        first = await c.post("/api/meals/photo/commit", headers=_hdr(), json=body)
        assert first.status_code >= 500
        again = await c.post("/api/meals/photo/commit", headers=_hdr(), json=body)
    assert again.status_code == 200 and again.json()["kind"] == "logged"


async def test_позиция_без_имени_не_показывается(client, fake_container):
    comps = [Component("123 г", 0.5, 100), Component("рис отварной", 0.5, 150)]
    fake_container.vision = StubVision(None, _plate(components=comps))
    async with client as c:
        d = (await c.post("/api/meals/photo", content=JPEG, headers=_hdr())).json()
    assert [x["name"] for x in d["components"]] == ["рис отварной"]
