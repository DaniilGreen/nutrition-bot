"""Группы дублей считаются один раз на каталог (16.10.2026: «Итоги» открывались ~10 с)."""
from __future__ import annotations

import asyncio

from app.application import assistant as mod
from app.application.entities import Product
from app.domain.value_objects import Density
from tests.conftest import TOKEN, FakeContainer, InMemoryUserRepo, Settings, _FakeDb


async def test_группы_считаются_один_раз_и_пересчитываются_при_смене_каталога(monkeypatch):
    c = FakeContainer(settings=Settings(bot_token=TOKEN, database_url="sqlite+aiosqlite:///:memory:"),
                      users=InMemoryUserRepo(), db=_FakeDb())
    for key, name in [("yog", "йогурт греческий"), ("yog_2", "йогурт греческий 200 г"),
                      ("rice", "рис отварной")]:
        await c.products.create(Product(canonical_key=key, display_name=name,
                                        density=Density(100, 5, 3, 10), source="llm"))
    calls = 0
    real = mod.group

    def counting(rows):
        nonlocal calls
        calls += 1
        return real(rows)

    monkeypatch.setattr(mod, "group", counting)
    mod._GROUPS_MEMO.clear()
    a, _ = c.assistant_for(None)
    first, second = await asyncio.gather(a.duplicate_groups(), a.duplicate_groups())
    assert calls == 1  # два запроса «Итогов» разом — один пересчёт
    assert [[p.display_name for p in g] for g in first[0]] == \
        [["йогурт греческий", "йогурт греческий 200 г"]] == \
        [[p.display_name for p in g] for g in second[0]]

    await c.products.create(Product(canonical_key="rice_2", display_name="рис отварной",
                                    density=Density(130, 3, 0, 28), source="llm"))
    groups, _ = await a.duplicate_groups()
    assert calls == 2 and len(groups) == 2  # новый продукт — новая подпись, пересчёт
