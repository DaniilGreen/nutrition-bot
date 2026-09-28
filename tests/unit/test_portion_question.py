"""FR-21: размер порции не спрашиваем, когда он ничего не меняет (28.09.2026)."""
from app.application.assistant import _first_unclarified_index
from app.application.dto import Intent, ParsedItem, ParsedMessage, Unit


def _item(name, kcal, sizes, zero=False):
    return ParsedItem(name=name, canonical_key=name, unit=Unit.PORTION, kcal_per_100g=kcal,
                      portion_options=sizes, zero_calorie=zero)


def test_вода_без_вопроса_о_размере():
    p = ParsedMessage(intent=Intent.MEAL, items=[_item("вода", 0, {"small": 200, "large": 500}, True)])
    assert _first_unclarified_index(p) is None and p.items[0].portion_hint == "medium"


def test_суп_спрашиваем():
    p = ParsedMessage(intent=Intent.MEAL, items=[_item("борщ", 60, {"small": 250, "large": 450})])
    assert _first_unclarified_index(p) == 0 and p.items[0].portion_hint is None


def test_пропуск_не_мешает_спросить_следующую_позицию():
    p = ParsedMessage(intent=Intent.MEAL, items=[
        _item("кофе чёрный", 2, {"small": 200, "large": 400}),
        _item("круассан", 400, {"small": 80, "large": 160}),
    ])
    assert _first_unclarified_index(p) == 1


def test_знакомый_продукт_без_плотности_спрашиваем():
    """Для продукта из каталога модель плотность не даёт (0) — это не «бескалорийный»."""
    p = ParsedMessage(intent=Intent.MEAL, items=[_item("борщ", 0, {"small": 250, "large": 450})])
    assert _first_unclarified_index(p) == 0
