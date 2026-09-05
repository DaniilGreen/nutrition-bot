"""Запись через API (§8.3, MA-M2): текст, уточнение, голос, удаление.

Черновик уточнения не покидает сервер — клиент получает только идентификатор.
"""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from tests.fakes import StubNlu, StubStt
from tests.initdata_fixtures import valid

H = {"X-Telegram-Init-Data": None}


def _meal(name: str, key: str, grams: float, kcal: float, clarify=None) -> ParsedMessage:
    """Плотности берём физически осмысленные: приложение сверяет их по Атвотеру
    (INV-10) и молча пересчитает выдуманные, а тест будет выглядеть сломанным."""
    protein = round(kcal * 0.19, 1)   # ~курица: почти весь калораж из белка
    fat = round(kcal * 0.022, 1)
    return ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name=name, canonical_key=key, unit=Unit.GRAM, grams=grams,
                kcal_per_100g=kcal, protein_per_100g=protein, fat_per_100g=fat,
                carb_per_100g=0, confidence=0.9, clarify_options=clarify,
            )
        ],
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
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _hdr(**extra):
    # Ключ записи обязателен (§7.5): свежий на каждый запрос, если тест не задал свой.
    return {"X-Telegram-Init-Data": valid(), "Idempotency-Key": uuid4().hex, **extra}


async def test_запись_текстом(client, fake_container):
    fake_container.nlu = StubNlu(_meal("куриная грудка", "chicken_breast", 200, 165))
    async with client as c:
        r = await c.post("/api/meals", json={"text": "200 г куриной грудки"}, headers=_hdr())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["kind"] == "logged"
    assert d["meal"]["items"][0]["grams"] == 200
    assert d["meal"]["kcal"] == 330  # 165 ккал/100 г × 200 г, без пересчёта
    # Остаток отдаётся сразу: фронт не должен ходить за днём второй раз.
    assert d["totals"]["kcal_left"] == round(d["totals"]["kcal_left"], 1)


@pytest.mark.parametrize("payload", [{}, {"text": ""}, {"text": "   "}])
async def test_пустой_текст_400(client, payload):
    async with client as c:
        r = await c.post("/api/meals", json=payload, headers=_hdr())
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "empty_text"


async def test_слишком_длинный_текст_400(client):
    async with client as c:
        r = await c.post("/api/meals", json={"text": "а" * 1001}, headers=_hdr())
    assert r.status_code == 400


async def test_уточнение_отдаёт_только_идентификатор(client, fake_container):
    """Черновик содержит плотности КБЖУ — наружу он уходить не должен (INV-1)."""
    fake_container.nlu = StubNlu(
        _meal("плов", "pilaf", 300, 180, clarify=["с курицей", "с говядиной"])
    )
    async with client as c:
        r = await c.post("/api/meals", json={"text": "плов"}, headers=_hdr())
    d = r.json()
    assert d["kind"] == "clarify"
    assert d["options"] == ["с курицей", "с говядиной"]
    assert isinstance(d["draft_id"], str) and len(d["draft_id"]) > 10
    # Ни плотностей, ни граммовок, ни имени ключа в ответе быть не должно.
    body = r.text
    assert "kcal_per_100g" not in body
    assert "pilaf" not in body


async def test_уточнение_доводит_запись_до_конца(client, fake_container):
    fake_container.nlu = StubNlu(
        _meal("плов", "pilaf", 300, 180, clarify=["с курицей", "с говядиной"])
    )
    async with client as c:
        first = (await c.post("/api/meals", json={"text": "плов"}, headers=_hdr())).json()
        r = await c.post(
            "/api/meals/clarify",
            json={"draft_id": first["draft_id"], "answer": "плов с курицей"},
            headers=_hdr(),
        )
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "logged"


async def test_черновик_одноразовый(client, fake_container):
    """Повторный ответ на то же уточнение записал бы приём дважды."""
    fake_container.nlu = StubNlu(_meal("плов", "pilaf", 300, 180, clarify=["с курицей"]))
    async with client as c:
        first = (await c.post("/api/meals", json={"text": "плов"}, headers=_hdr())).json()
        body = {"draft_id": first["draft_id"], "answer": "плов с курицей"}
        assert (await c.post("/api/meals/clarify", json=body, headers=_hdr())).status_code == 200
        again = await c.post("/api/meals/clarify", json=body, headers=_hdr())
    assert again.status_code == 400
    assert again.json()["error"]["code"] == "draft_gone"


async def test_несуществующий_черновик_400(client):
    async with client as c:
        r = await c.post(
            "/api/meals/clarify", json={"draft_id": "нет-такого", "answer": "х"}, headers=_hdr()
        )
    assert r.status_code == 400


async def test_вес_записывается_через_тот_же_вход(client, fake_container):
    fake_container.nlu = StubNlu(
        ParsedMessage(intent=Intent.WEIGHT, weight_kg=91.5, confidence=0.95)
    )
    async with client as c:
        r = await c.post("/api/meals", json={"text": "вес 91.5"}, headers=_hdr())
    assert r.json()["kind"] == "weight"
    assert r.json()["weight_kg"] == 91.5


async def test_нераспознанное_не_выдумывает_приём(client, fake_container):
    fake_container.nlu = StubNlu(ParsedMessage(intent=Intent.UNKNOWN, confidence=0.2))
    async with client as c:
        r = await c.post("/api/meals", json={"text": "жопа"}, headers=_hdr())
    assert r.status_code == 200
    assert r.json()["kind"] == "unknown"


async def test_удаление_приёма(client, fake_container):
    fake_container.nlu = StubNlu(_meal("грудка", "chicken", 200, 165))
    async with client as c:
        logged = (await c.post("/api/meals", json={"text": "грудка"}, headers=_hdr())).json()
        mid = logged["meal"]["id"]
        r = await c.delete(f"/api/meals/{mid}", headers=_hdr())
        day = (await c.get("/api/day", headers=_hdr())).json()
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert day["meals"] == []


async def test_удаление_чужого_приёма_404(client):
    async with client as c:
        r = await c.delete("/api/meals/99999", headers=_hdr())
    assert r.status_code == 404


async def test_голос_без_stt_отдаёт_503(client):
    """STT не сконфигурирован — отказ обязан быть внятным, а не 500."""
    async with client as c:
        r = await c.post("/api/meals/voice", content=b"\x00\x01\x02", headers=_hdr())
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "stt_off"


async def test_пустое_аудио_400(client):
    async with client as c:
        r = await c.post("/api/meals/voice", content=b"", headers=_hdr())
    assert r.status_code == 400


async def test_слишком_большое_аудио_400(client):
    async with client as c:
        r = await c.post("/api/meals/voice", content=b"x" * 1_600_000, headers=_hdr())
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "audio_too_big"


async def test_запись_требует_подписи(client):
    async with client as c:
        assert (await c.post("/api/meals", json={"text": "х"})).status_code == 401
        assert (await c.post("/api/meals/voice", content=b"x")).status_code == 401
        assert (await c.delete("/api/meals/1")).status_code == 401


async def test_сбой_модели_объясняет_себя(client, fake_container):
    """Обрыв связи с OpenAI не должен выглядеть как пустой 500 (§7.4)."""

    class Broken:
        async def parse_samples(self, text, known_keys):
            import openai

            raise openai.APIConnectionError(request=None)  # type: ignore[arg-type]

        async def parse(self, text, known_keys):
            return await self.parse_samples(text, known_keys)

    from app.main_api import app

    fake_container.nlu = Broken()
    # raise_app_exceptions=False — иначе транспорт пробросит исключение наружу
    # и обработчик 503 не отработает. В живом сервере он срабатывает (проверено
    # curl'ом), тут это особенность тестового транспорта, а не кода.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as c:
        r = await c.post("/api/meals", json={"text": "яйца"}, headers=_hdr())
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "upstream_unavailable"


async def test_повтор_с_тем_же_ключом_не_пишет_дважды(client, fake_container):
    """§7.5: двойной тап и ретрай сети иначе дают два приёма — цифра врёт вдвое."""
    fake_container.nlu = StubNlu(_meal("грудка", "chicken", 200, 165))
    # Ключ ASCII: заголовки HTTP не переносят кириллицу.
    hdr = {**_hdr(), "Idempotency-Key": "same-key-0001"}
    async with client as c:
        first = (await c.post("/api/meals", json={"text": "грудка"}, headers=hdr)).json()
        second = (await c.post("/api/meals", json={"text": "грудка"}, headers=hdr)).json()
        day = (await c.get("/api/day", headers=_hdr())).json()
    assert first["meal"]["id"] == second["meal"]["id"]
    assert len(day["meals"]) == 1


async def test_разные_ключи_пишут_разное(client, fake_container):
    """Съел дважды — записалось дважды. Идемпотентность не должна это ломать."""
    fake_container.nlu = StubNlu(_meal("грудка", "chicken", 200, 165))
    async with client as c:
        await c.post("/api/meals", json={"text": "грудка"},
                     headers={**_hdr(), "Idempotency-Key": "a"})
        await c.post("/api/meals", json={"text": "грудка"},
                     headers={**_hdr(), "Idempotency-Key": "b"})
        day = (await c.get("/api/day", headers=_hdr())).json()
    assert len(day["meals"]) == 2


async def test_чужой_ключ_не_отдаёт_чужой_ответ(client, fake_container):
    """Ключ скоупится пользователем: иначе один прочитал бы ответ другого."""
    from app.web.limits import IdempotencyStore

    store = IdempotencyStore()
    store.put(999, "общий", {"kind": "logged", "meal": {"id": 1}})
    assert store.get(1, "общий") is None


async def test_слишком_частая_запись_429(client, fake_container):
    """Платный путь без потолка выжигается залипшей кнопкой за минуты."""
    fake_container.nlu = StubNlu(_meal("грудка", "chicken", 200, 165))
    async with client as c:
        codes = []
        for i in range(22):
            r = await c.post("/api/meals", json={"text": f"грудка {i}"}, headers=_hdr())
            codes.append(r.status_code)
    assert 429 in codes
    assert codes.count(200) == 20  # лимит 20 в минуту


def _needs_portion(name: str, key: str, kcal: float) -> ParsedMessage:
    """Позиция без веса и без штук — приложение обязано спросить порцию."""
    return ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name=name, canonical_key=key, unit=Unit.PORTION, grams=None,
                kcal_per_100g=kcal, protein_per_100g=round(kcal * 0.19, 1),
                fat_per_100g=round(kcal * 0.022, 1), carb_per_100g=0, confidence=0.9,
                portion_options={"small": 200.0, "medium": 350.0, "large": 500.0},
            )
        ],
    )


async def test_уточнение_порции_помечено_и_без_вариантов(client, fake_container):
    """Клиент обязан отличать «выбери имя» от «сколько было»: перепутав, он
    отправит «300 г» как название продукта."""
    fake_container.nlu = StubNlu(_needs_portion("борщ", "borsch", 60))
    async with client as c:
        r = (await c.post("/api/meals", json={"text": "борщ"}, headers=_hdr())).json()
    assert r["kind"] == "clarify"
    assert r["ask"] == "portion"
    assert r["options"] == []


async def test_ответ_граммами_доводит_приём_до_записи(client, fake_container):
    """Раньше этот путь был тупиком — записать было нечем, приём терялся."""
    fake_container.nlu = StubNlu(_needs_portion("борщ", "borsch", 60))
    async with client as c:
        q = (await c.post("/api/meals", json={"text": "борщ"}, headers=_hdr())).json()
        r = (await c.post("/api/meals/clarify",
                          json={"draft_id": q["draft_id"], "answer": "350"},
                          headers=_hdr())).json()
        day = (await c.get("/api/day", headers=_hdr())).json()
    assert r["kind"] == "logged", r
    assert r["meal"]["items"][0]["grams"] == 350
    assert len(day["meals"]) == 1


async def test_ответ_размером_тоже_записывает(client, fake_container):
    fake_container.nlu = StubNlu(_needs_portion("борщ", "borsch", 60))
    async with client as c:
        q = (await c.post("/api/meals", json={"text": "борщ"}, headers=_hdr())).json()
        r = (await c.post("/api/meals/clarify",
                          json={"draft_id": q["draft_id"], "answer": "средняя"},
                          headers=_hdr())).json()
    assert r["kind"] == "logged"
    assert r["meal"]["items"][0]["grams"] == 350


async def test_небольшая_это_маленькая_а_не_большая(client, fake_container):
    """Подстрока «больш» внутри «небольшая» раньше давала БОЛЬШУЮ порцию."""
    fake_container.nlu = StubNlu(_needs_portion("борщ", "borsch", 60))
    async with client as c:
        q = (await c.post("/api/meals", json={"text": "борщ"}, headers=_hdr())).json()
        r = (await c.post("/api/meals/clarify",
                          json={"draft_id": q["draft_id"], "answer": "небольшая"},
                          headers=_hdr())).json()
    assert r["meal"]["items"][0]["grams"] == 200


async def test_непонятный_ответ_не_съедает_черновик_молча(client, fake_container):
    fake_container.nlu = StubNlu(_needs_portion("борщ", "borsch", 60))
    async with client as c:
        q = (await c.post("/api/meals", json={"text": "борщ"}, headers=_hdr())).json()
        r = (await c.post("/api/meals/clarify",
                          json={"draft_id": q["draft_id"], "answer": "ну как обычно"},
                          headers=_hdr())).json()
    assert r["kind"] == "unknown"
    assert "размером" in r["message"] or "граммами" in r["message"]


async def _logged(c, fake_container):
    fake_container.nlu = StubNlu(_meal("грудка", "chicken", 200, 165))
    r = (await c.post("/api/meals", json={"text": "грудка 200 г"}, headers=_hdr())).json()
    return r["meal"]


async def test_правка_граммовки_масштабирует_снапшот(client, fake_container):
    """MA-INV-22: правка достижима из ленты. INV-3: снапшот масштабируется,
    а НЕ пересчитывается из каталога."""
    async with client as c:
        m = await _logged(c, fake_container)
        item = m["items"][0]
        before = item["kcal"]
        r = (await c.patch(f"/api/meals/{m['id']}/items/{item['id']}",
                           json={"grams": 100}, headers=_hdr())).json()
    assert r["item"]["grams"] == 100
    assert r["old_grams"] == 200
    assert r["item"]["kcal"] == round(before / 2, 1)     # ровно половина
    assert r["totals"]["kcal"] == r["item"]["kcal"]


async def test_правка_чужой_позиции_404(client, fake_container):
    async with client as c:
        m = await _logged(c, fake_container)
        r = await c.patch(f"/api/meals/{m['id']}/items/99999",
                          json={"grams": 100}, headers=_hdr())
    assert r.status_code == 404


async def test_правка_в_чужом_приёме_404(client, fake_container):
    async with client as c:
        m = await _logged(c, fake_container)
        r = await c.patch(f"/api/meals/99999/items/{m['items'][0]['id']}",
                          json={"grams": 100}, headers=_hdr())
    assert r.status_code == 404


@pytest.mark.parametrize("body", [{}, {"grams": 0}, {"grams": -5},
                                  {"grams": 99999}, {"grams": "много"}])
async def test_негодный_вес_400(client, fake_container, body):
    async with client as c:
        m = await _logged(c, fake_container)
        r = await c.patch(f"/api/meals/{m['id']}/items/{m['items'][0]['id']}",
                          json=body, headers=_hdr())
    assert r.status_code == 400


async def test_правка_требует_подписи(client):
    async with client as c:
        assert (await c.patch("/api/meals/1/items/1", json={"grams": 100})).status_code == 401


async def test_голос_идемпотентен(client, fake_container):
    """Голос — самый дорогой путь: повтор после таймаута сети писал два приёма,
    потому что фронт слал ключ, а обработчик его не принимал."""
    fake_container.nlu = StubNlu(_meal("грудка", "chicken", 200, 165))
    fake_container.stt = StubStt("грудка 200 г")
    h = {**_hdr(), "Content-Type": "audio/ogg", "Idempotency-Key": "voice-1"}
    async with client as c:
        a = (await c.post("/api/meals/voice", content=b"x" * 100, headers=h)).json()
        b = (await c.post("/api/meals/voice", content=b"x" * 100, headers=h)).json()
        day = (await c.get("/api/day", headers=_hdr())).json()
    assert a["meal"]["id"] == b["meal"]["id"]
    assert len(day["meals"]) == 1


async def test_повтор_идемпотентен(client, fake_container):
    fake_container.nlu = StubNlu(_meal("грудка", "chicken", 200, 165))
    h = {**_hdr(), "Idempotency-Key": "rep-1"}
    async with client as c:
        m = (await c.post("/api/meals", json={"text": "грудка"}, headers=_hdr())).json()["meal"]
        a = (await c.post(f"/api/meals/{m['id']}/repeat", headers=h)).json()
        b = (await c.post(f"/api/meals/{m['id']}/repeat", headers=h)).json()
        day = (await c.get("/api/day", headers=_hdr())).json()
    assert a["meal"]["id"] == b["meal"]["id"]
    assert len(day["meals"]) == 2      # оригинал + один повтор, не два


async def test_уточнение_под_лимитом(client, fake_container):
    """resolve_clarification идёт в модель — значит платный путь и обязан
    иметь потолок, как обычная запись."""
    fake_container.nlu = StubNlu(_meal("грудка", "chicken", 200, 165))
    async with client as c:
        codes = []
        for i in range(22):
            r = await c.post("/api/meals/clarify",
                             json={"draft_id": f"нет-{i}", "answer": "да"}, headers=_hdr())
            codes.append(r.status_code)
    assert 429 in codes


@pytest.mark.parametrize(
    ("ctype", "expect"),
    [("audio/webm;codecs=opus", "voice.webm"),
     ("audio/ogg", "voice.ogg"),
     ("audio/mp4", "voice.mp4"),          # Safari и Telegram на iOS
     ("application/octet-stream", "voice.webm")],
)
async def test_имя_файла_по_типу_записи(client, fake_container, ctype, expect):
    """Whisper выбирает парсер по расширению. Запись Safari, названная
    «voice.ogg», не распознавалась вовсе — хотя сама запись проходила."""
    fake_container.nlu = StubNlu(_meal("грудка", "chicken", 200, 165))
    fake_container.stt = StubStt("грудка 200 г")
    async with client as c:
        await c.post("/api/meals/voice", content=b"x" * 100,
                     headers={**_hdr(), "Content-Type": ctype,
                              "Idempotency-Key": f"ct-{ctype}"})
    assert fake_container.stt.seen_filename == expect


def _named_volume(name: str, key: str, kcal: float, model_grams: float) -> ParsedMessage:
    """Модель ставит объём в имя, а граммовку берёт свою — ровно так и вышло
    с «Пиво Сайгон 0.3 л»: 500 г вместо 300."""
    return ParsedMessage(
        intent=Intent.MEAL,
        confidence=0.9,
        items=[
            ParsedItem(
                name=name, canonical_key=key, unit=Unit.GRAM, grams=model_grams,
                kcal_per_100g=kcal, protein_per_100g=0.5, fat_per_100g=0.0,
                carb_per_100g=3.6, confidence=0.9,
            )
        ],
    )


async def test_объём_в_имени_старше_оценки_модели(client, fake_container):
    """INV-7: явный вес — первый шаг цепочки. Названные 0.3 л это 300 г,
    а не «кружка пива» из оценки."""
    fake_container.nlu = StubNlu(_named_volume("Пиво Сайгон 0.3 л", "beer_saigon", 42, 500))
    async with client as c:
        r = (await c.post("/api/meals", json={"text": "пиво сайгон 03"},
                          headers=_hdr())).json()
    item = r["meal"]["items"][0]
    assert item["grams"] == 300, item
    # 42 ккал/100 г × 300 г = 126, а не 210.
    assert 120 <= item["kcal"] <= 132, item


async def test_жирность_в_имени_не_путается_с_объёмом(client, fake_container):
    """«молоко 3.2%» — не 3,2 грамма. На такой ошибке цифра уехала бы в ноль."""
    fake_container.nlu = StubNlu(_named_volume("молоко 3.2%", "milk_32", 60, 200))
    async with client as c:
        r = (await c.post("/api/meals", json={"text": "молоко 200"},
                          headers=_hdr())).json()
    assert r["meal"]["items"][0]["grams"] == 200


async def test_согласная_граммовка_не_переписывается(client, fake_container):
    """Если модель и имя сходятся, ничего не трогаем."""
    fake_container.nlu = StubNlu(_named_volume("кола 500 мл", "cola", 42, 500))
    async with client as c:
        r = (await c.post("/api/meals", json={"text": "кола 500 мл"},
                          headers=_hdr())).json()
    assert r["meal"]["items"][0]["grams"] == 500
