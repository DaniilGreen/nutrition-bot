"""Каталог-редактор (§8.4, MA-M3): список, поиск, правка, слияние дублей."""
from __future__ import annotations

import httpx
import pytest

from app.domain.value_objects import Density
from tests.initdata_fixtures import valid


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


def _hdr():
    return {"X-Telegram-Init-Data": valid()}


async def _seed(repo, *rows):
    from app.application.entities import Product

    out = []
    for key, name, kcal, source in rows:
        out.append(
            await repo.create(
                Product(
                    canonical_key=key,
                    display_name=name,
                    density=Density(
                        kcal_per_100g=kcal, protein_per_100g=round(kcal * 0.19, 1),
                        fat_per_100g=round(kcal * 0.02, 1), carb_per_100g=0.0,
                    ),
                    source=source,
                )
            )
        )
    return out


async def test_каталог_страницами(client, fake_container):
    """Полный список отдавать нельзя: их больше двух сотен."""
    rows = [(f"p{i}", f"продукт {i:02d}", 100 + i, "llm") for i in range(25)]
    await _seed(fake_container.products, *rows)
    async with client as c:
        first = (await c.get("/api/products?limit=10", headers=_hdr())).json()
        second = (await c.get("/api/products?limit=10&offset=10", headers=_hdr())).json()
    assert first["total"] == 25
    assert len(first["items"]) == 10
    assert first["items"][0]["name"] != second["items"][0]["name"]


async def test_поиск_по_имени(client, fake_container):
    await _seed(fake_container.products,
                ("chicken", "куриная грудка", 165, "curated"),
                ("rice", "рис отварной", 130, "llm"))
    async with client as c:
        r = (await c.get("/api/products?q=куриная", headers=_hdr())).json()
    assert r["total"] == 1
    assert r["items"][0]["key"] == "chicken"


async def test_процент_в_запросе_не_становится_маской(client, fake_container):
    """Ввод «100%» не должен превращаться в «что угодно»."""
    await _seed(fake_container.products, ("milk", "молоко 3.2%", 60, "llm"))
    async with client as c:
        r = (await c.get("/api/products?q=%25%25%25", headers=_hdr())).json()
    assert r["total"] == 0


async def test_карточка_со_счётчиком(client, fake_container):
    """«Ел 9 раз» — про ЭТОГО человека, не про обоих."""
    (p,) = await _seed(fake_container.products, ("chicken", "грудка", 165, "curated"))
    async with client as c:
        uid = int((await c.get("/api/me", headers=_hdr())).json()["user"]["id"])
        fake_container.products.set_usage(p.id, 9, user_id=uid)
        r = (await c.get(f"/api/products/{p.id}", headers=_hdr())).json()
    assert r["product"]["uses"] == 9
    assert r["product"]["source"] == "curated"


async def test_чужой_счётчик_не_виден(client, fake_container):
    """Сколько раз это ел другой человек — его дело. Данные о здоровье."""
    (p,) = await _seed(fake_container.products, ("chicken", "грудка", 165, "curated"))
    fake_container.products.set_usage(p.id, 42, user_id=999)
    async with client as c:
        r = (await c.get(f"/api/products/{p.id}", headers=_hdr())).json()
    assert r["product"]["uses"] == 0


async def test_несуществующий_продукт_404(client):
    async with client as c:
        r = await c.get("/api/products/9999", headers=_hdr())
    assert r.status_code == 404


async def test_переименование(client, fake_container):
    (p,) = await _seed(fake_container.products, ("x", "непонятное", 100, "llm"))
    async with client as c:
        r = await c.patch(f"/api/products/{p.id}", json={"name": "гречка отварная"}, headers=_hdr())
    assert r.status_code == 200
    assert r.json()["product"]["name"] == "гречка отварная"


@pytest.mark.parametrize("name", ["", "я", "  ", "х" * 121])
async def test_негодное_имя_400(client, fake_container, name):
    (p,) = await _seed(fake_container.products, ("x", "продукт", 100, "llm"))
    async with client as c:
        r = await c.patch(f"/api/products/{p.id}", json={"name": name}, headers=_hdr())
    assert r.status_code == 400


async def test_правка_плотности(client, fake_container):
    (p,) = await _seed(fake_container.products, ("x", "продукт", 100, "llm"))
    async with client as c:
        r = await c.patch(
            f"/api/products/{p.id}",
            json={"kcal_per_100g": 165, "protein_per_100g": 31,
                  "fat_per_100g": 3.6, "carb_per_100g": 0},
            headers=_hdr(),
        )
    assert r.status_code == 200
    assert r.json()["product"]["kcal_per_100g"] == 165


@pytest.mark.parametrize(
    "body",
    [
        {"kcal_per_100g": 5000, "protein_per_100g": 1, "fat_per_100g": 1, "carb_per_100g": 1},
        {"kcal_per_100g": -5, "protein_per_100g": 1, "fat_per_100g": 1, "carb_per_100g": 1},
        {"kcal_per_100g": 100, "protein_per_100g": 310, "fat_per_100g": 1, "carb_per_100g": 1},
        {"kcal_per_100g": 100},
    ],
    ids=["калорийнее-жира", "отрицательная", "310г-белка", "неполная"],
)
async def test_невозможная_плотность_отвергается(client, fake_container, body):
    """Домен зажал бы значение молча — на границе доверия отказ понятнее."""
    (p,) = await _seed(fake_container.products, ("x", "продукт", 100, "llm"))
    async with client as c:
        r = await c.patch(f"/api/products/{p.id}", json=body, headers=_hdr())
    assert r.status_code == 400


async def test_группы_дублей_показываются_с_разбросом(client, fake_container):
    await _seed(fake_container.products,
                ("borscht", "борщ", 60, "llm"),
                ("borscht_2", "борщ", 62, "manual"),
                ("chicken", "куриная грудка", 165, "curated"))
    async with client as c:
        r = (await c.get("/api/products/duplicates/groups", headers=_hdr())).json()
    assert len(r["groups"]) == 1
    g = r["groups"][0]
    assert len(g["items"]) == 2
    assert g["safe"] is True          # 60 → 62, слияние цифры не двинет
    assert g["spread"] < 0.15
    assert "affects" in g             # AC-MA-26: сколько записей переедет


async def test_группа_с_большим_разбросом_помечена_небезопасной(client, fake_container):
    """Слить 60 и 130 значит заметно двинуть историю — решает человек (D-32)."""
    await _seed(fake_container.products,
                ("borscht", "борщ", 60, "llm"),
                ("borscht_2", "борщ", 130, "llm"))
    async with client as c:
        r = (await c.get("/api/products/duplicates/groups", headers=_hdr())).json()
    assert r["groups"][0]["safe"] is False


async def test_слияние_удаляет_проигравших(client, fake_container):
    a, b = await _seed(fake_container.products,
                       ("borscht", "борщ", 60, "llm"),
                       ("borscht_2", "борщ", 62, "manual"))
    fake_container.products.set_usage(a.id, 3)
    fake_container.products.set_usage(b.id, 1)
    async with client as c:
        r = await c.post(
            "/api/products/merge",
            json={"winner_id": b.id, "loser_ids": [a.id]},
            headers=_hdr(),
        )
        left = (await c.get("/api/products", headers=_hdr())).json()
    assert r.status_code == 200
    assert r.json()["items_repointed"] == 3   # история перепривязана, не потеряна
    assert left["total"] == 1
    assert left["items"][0]["id"] == b.id


@pytest.mark.parametrize(
    "body",
    [{}, {"winner_id": 1}, {"winner_id": 1, "loser_ids": []}, {"winner_id": 1, "loser_ids": [1]}],
    ids=["пусто", "без-проигравших", "пустой-список", "сам-себя"],
)
async def test_негодное_слияние_400(client, fake_container, body):
    await _seed(fake_container.products, ("x", "продукт", 100, "llm"))
    async with client as c:
        r = await c.post("/api/products/merge", json=body, headers=_hdr())
    assert r.status_code == 400


async def test_слияние_в_несуществующего_404(client, fake_container):
    (p,) = await _seed(fake_container.products, ("x", "продукт", 100, "llm"))
    async with client as c:
        r = await c.post(
            "/api/products/merge", json={"winner_id": 9999, "loser_ids": [p.id]}, headers=_hdr()
        )
    assert r.status_code == 404


async def test_каталог_требует_подписи(client):
    async with client as c:
        assert (await c.get("/api/products")).status_code == 401
        assert (await c.post("/api/products/merge", json={})).status_code == 401
