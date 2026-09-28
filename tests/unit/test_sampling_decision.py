"""INV-12: сэмплы — ровно для тех позиций, что станут новым продуктом (28.09.2026).

Адаптер отсекает знакомое по ключу. Но ключ мог совпасть, а продукт — оказаться чужим
(«сырники» под ключом творога): тогда запись создаст новый продукт, и одна оценка его
плотности пиннилась бы навсегда. Этот случай ловит сервис — той же логикой, что запись.
"""
from __future__ import annotations

from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit
from app.application.entities import Product
from app.domain.value_objects import Density
from tests.conftest import FakeContainer, _FakeDb, TOKEN
from tests.fakes import InMemoryUserRepo, SamplingStubNlu
from app.config import Settings


def _meal(key: str, name: str) -> ParsedMessage:
    item = ParsedItem(name=name, canonical_key=key, unit=Unit.GRAM, grams=150.0,
                      kcal_per_100g=220.0, protein_per_100g=15, fat_per_100g=9, carb_per_100g=20)
    return ParsedMessage(intent=Intent.MEAL, items=[item])


async def _run(nlu: SamplingStubNlu) -> None:
    fc = FakeContainer(settings=Settings(bot_token=TOKEN, database_url="sqlite+aiosqlite:///:memory:"),
                       users=InMemoryUserRepo(), db=_FakeDb())
    await fc.products.create(Product(canonical_key="cottage_cheese_9", display_name="творог 9%",
                                     density=Density(159, 16.7, 9, 2), is_estimate=False))
    fc.nlu = nlu
    assistant, users = fc.assistant_for(None)
    user = await users.get_or_create(telegram_id=1, name="т", locale="ru", timezone="UTC")
    await assistant.process(user, "текст")


async def test_чужой_продукт_под_знакомым_ключом_сэмплируется():
    nlu = SamplingStubNlu([_meal("cottage_cheese_9", "сырники")])
    await _run(nlu)
    assert nlu.forced


async def test_знакомый_продукт_не_сэмплируется_повторно():
    nlu = SamplingStubNlu([_meal("cottage_cheese_9", "творог 9%")])
    await _run(nlu)
    assert not nlu.forced


async def test_новый_ключ_сервис_не_форсирует():
    """Новый ключ адаптер видел сам; не сэмплировал — значит k = 1, и форс был бы впустую."""
    nlu = SamplingStubNlu([_meal("syrniki_new", "сырники")])
    await _run(nlu)
    assert not nlu.forced
