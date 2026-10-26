"""Правка карточки фото словами — без модели (FR-34.1, D-MA-30).

Пока висит карточка, «курица 150», «без риса», «это булгур, а не рис», «добавь соус 30 г»
и вес тарелки числом — это правка карточки. Формы узкие и разбираются регулярками: правка
должна быть мгновенной, бесплатной и предсказуемой, а модель и так позовут при «Записать».
Не разобралось — это не правка: решает обработчик (обычный путь, еда не теряется).

Здесь только разбор фразы и арифметика над карточкой; имя позиции ищет `naming.match_names`
(то же правило, что у ответа на карточку приёма, FR-44).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.naming import _same_word, _tokens, match_names
from app.domain.plate import Component, clean_name, split


@dataclass(frozen=True)
class Edit:
    op: str                    # total | grams | off | on | rename | add
    name: str = ""             # позиция, как её назвал человек; пусто — не назвал («это булгур»)
    grams: float | None = None
    new: str = ""              # rename/add: новое имя
    unit: bool = True          # число было с единицей («150 г»); «2» без неё — не граммы наверняка


_N = r"(\d+(?:[.,]\d+)?)"
_U = r"\s*(кг|г|гр|грамм\w*|мл)?\.?"
_ADD = r"(?:добавь|добавить|добавил\w*|ещ[её]|плюс|\+)"
_OFF = r"(?:без|убери|убрать|удали|удалить|минус|не\s+ел\w*|не\s+было)"
_ON = r"(?:верни|вернуть)"
_THIS = r"(?:это|там|тут)"


def _g(num: str, unit: str | None) -> float:
    g = float(num.replace(",", "."))
    return round(g * 1000, 1) if unit == "кг" else g


# «это вкусно», «это был обед», «это всё» — не переименование, а реплика (ревью 26.10.2026).
_NOT_FOOD = frozenset({
    "вкусно", "невкусно", "всё", "все", "обед", "ужин", "завтрак", "перекус", "полдник", "много",
    "мало", "норм", "нормально", "ок", "окей", "хорошо", "отлично", "неважно", "так", "правильно",
    "верно", "она", "он", "оно", "они", "мой", "моя", "моё", "мое", "мои", "не", "то", "фото",
    "тарелка", "порция", "еда", "блюдо", "ерунда", "супер", "класс", "жесть", "что",
})


def _named(text: str) -> str:
    """Имя без хвостового «было», «съел» и знаков. Без букв или с цифрой — не имя."""
    name = re.sub(r"(?i)\s*\b(?:съел\w*|съедено|ел[аи]?)\b", "", text)
    name = re.sub(r"\s+(?:было|весит)$", "", name.strip(" .:=—-"))
    return name if re.search(r"[^\W\d_]", name) and not re.search(r"\d", name) else ""


def _names(text: str) -> list[str]:
    """«риса и хлеба» — две позиции."""
    return [n for n in (_named(p) for p in re.split(r"\s+и\s+", text)) if n]


def _with_grams(text: str) -> tuple[str, float | None, bool]:
    """«булгур 150 г» → («булгур», 150, True); «булгур» → («булгур», None, True)."""
    m = re.fullmatch(rf"(.+?)\s+{_N}{_U}", text)
    if m:
        return _named(m.group(1)), _g(m.group(2), m.group(3)), bool(m.group(3))
    return _named(text), None, True


def _one(part: str) -> list[Edit] | None:
    p = re.sub(r"\s+", " ", part.lower().strip(" .!"))
    m = re.fullmatch(
        rf"(?:(?:вся|весь|всё|все) )?(?:(?:тарелк\w*|порци\w*|вес|всего|итого|блюдо|миск\w*)"
        rf" ?[:=—-]? ?)?{_N}{_U}", p,
    )
    if m:
        return [Edit("total", grams=_g(m.group(1), m.group(2)), unit=bool(m.group(2)))]
    for pattern, op in ((rf"{_OFF} (.+)", "off"), (r"(.+?) не (?:ел\w*|было)", "off"),
                        (rf"{_ON} (.+)", "on")):
        m = re.fullmatch(pattern, p)
        if m:
            names = _names(m.group(1))
            return [Edit(op, name=n) for n in names] if names else None
    m = re.fullmatch(rf"{_ADD} ?{_N}{_U} (.+)", p)
    if m:
        new = _named(m.group(3))
        return [Edit("add", new=new, grams=_g(m.group(1), m.group(2)),
                     unit=bool(m.group(2)))] if new else None
    m = re.fullmatch(rf"{_ADD} ?(.+)", p)
    if m:
        new, grams, unit = _with_grams(m.group(1))
        return [Edit("add", new=new, grams=grams, unit=unit)] if new else None
    for pattern, new_at in (
        (r"не (.+?) а (.+)", 2),                                # «не рис, а булгур» — первой
        (rf"(?:{_THIS} )?(?:был[аио]? )?(.+?) а не (.+)", 1),   # «это булгур, а не рис»
        (r"(?:замени(?:ть)? )?(.+?) ?(?:→|->|=>) ?(.+)", 2),     # «рис → булгур»
        (r"замени(?:ть)? (.+?) на (.+)", 2),                    # «замени рис на булгур»
    ):
        m = re.fullmatch(pattern, p)
        if m:
            old = _named(m.group(3 - new_at))
            new, grams, unit = _with_grams(m.group(new_at))
            if not old or not new:
                return None
            return [Edit("rename", name=old, new=new, grams=grams, unit=unit)]
    # Голое «это булгур» — только кандидат: обработчик ВСЕГДА спрашивает, что заменить.
    m = re.fullmatch(rf"{_THIS} (?:был[аио]? )?(.+)", p)
    if m:
        new, grams, unit = _with_grams(m.group(1))
        if not new or new.split()[0] in _NOT_FOOD:
            return None
        return [Edit("rename", new=new, grams=grams, unit=unit)]
    m = re.fullmatch(rf"(.+?) (?:было |весит )?[:=—-]? ?{_N}{_U}", p)
    if m and _named(m.group(1)):
        return [Edit("grams", name=_named(m.group(1)), grams=_g(m.group(2), m.group(3)),
                     unit=bool(m.group(3)))]
    m = re.fullmatch(rf"{_N}\s*(кг|г|гр|грамм\w*|мл)\.? (.+)", p)   # «150 г курицы» — с единицей
    if m and _named(m.group(3)):
        return [Edit("grams", name=_named(m.group(3)), grams=_g(m.group(1), m.group(2)))]
    return None


def parse(text: str) -> list[Edit] | None:
    """Правки из сообщения или None — это не правка карточки.

    Несколько правок — через запятую, «;» или строку («курица 150, без риса»). Хоть один кусок
    не разобрался — None целиком: полправки молча хуже, чем обычный путь для всей фразы."""
    text = re.sub(r",\s*а\s+", " а ", text or "")  # «это булгур, а не рис» — одна правка
    # Запятая между цифрами — десятичная («0,34 кг»), а не разделитель правок.
    parts = [p for p in re.split(r",(?!\d)|(?<!\d),|[;\n]", text) if p.strip()]
    out: list[Edit] = []
    for part in parts:
        edits = _one(part)
        if not edits:
            return None
        out.extend(edits)
    return out or None


def find(names: list[str], query: str) -> list[int]:
    """Позиции карточки, которые человек назвал («курицы» → «курица жареная»)."""
    return match_names(names, query)


# Короткий список «одна и та же еда разными словами» — только для порядка кнопок в вопросе
# «чей вес?», не для правки без вопроса. ponytail: дополнять по случаям, а не впрок.
_SYNONYMS = (
    ("кур", "курин", "цыпл", "филе", "грудк", "бедр", "голен", "крыл"),
    ("картош", "картоф", "пюре", "фри", "драник"),
    ("мяс", "говя", "свин", "стейк", "телят", "баран", "котлет", "фарш"),
    ("рыб", "лосос", "семг", "форел", "треск", "тунец", "минта"),
    ("макарон", "паст", "спагет", "лапш"),
)


def similar(names: list[str], query: str) -> list[int]:
    """Позиции, похожие на названное, когда строгого совпадения нет: общее слово (словоформа),
    общее начало из 4 букв («курицу» ~ «куриное») или одна группа синонимов."""
    q = _tokens(query)

    def near(a: str, b: str) -> bool:
        if _same_word(a, b) or (len(a) >= 4 and len(b) >= 4 and a[:4] == b[:4]):
            return True
        return any(any(a.startswith(x) for x in g) and any(b.startswith(x) for x in g)
                   for g in _SYNONYMS)

    return [i for i, n in enumerate(names) if any(near(a, b) for a in q for b in _tokens(n))]


def apply(
    edit: Edit, i: int | None, comps: list[Component], grams: list[float], off: list[int],
    total: float | None,
) -> None:
    """Правка на месте. Вес, названный человеком, делает позицию `fixed`: делёж тарелки её
    больше не двигает; тарелка уже взвешена — остаток делится заново (`plate.split`)."""
    if edit.op == "total" and edit.grams:
        grams[:] = split(_current(comps, grams), edit.grams)
    elif edit.op == "add" and edit.grams:
        comps.append(Component(clean_name(edit.new), 0.0, edit.grams, True))
        grams.append(edit.grams)
    elif i is None:
        return
    elif edit.op == "off":
        if i not in off:
            off.append(i)
    elif edit.op == "on":
        if i in off:
            off.remove(i)
    else:  # grams | rename
        if edit.op == "rename":
            comps[i].name = clean_name(edit.new)
        if edit.grams is not None:
            comps[i].grams, comps[i].fixed, comps[i].note = edit.grams, True, ""
            grams[i] = edit.grams
            if total:
                grams[:] = split(_current(comps, grams), total)


def _current(comps: list[Component], grams: list[float]) -> list[Component]:
    return [Component(c.name, c.share, g, c.fixed) for c, g in zip(comps, grams, strict=True)]


def demo() -> None:
    """Runnable-чек форм правки."""
    assert parse("340") == [Edit("total", grams=340, unit=False)]
    assert parse("340 г") == [Edit("total", grams=340)]
    assert parse("тарелка 340") == [Edit("total", grams=340, unit=False)]
    assert parse("Вся тарелка 340 г") == [Edit("total", grams=340)]
    assert parse("0,34 кг") == [Edit("total", grams=340)]
    assert parse("курица 150") == [Edit("grams", name="курица", grams=150, unit=False)]
    assert parse("курица 150 г") == [Edit("grams", name="курица", grams=150)]
    assert parse("курицы было 150") == [Edit("grams", name="курицы", grams=150, unit=False)]
    assert parse("150 г курицы") == [Edit("grams", name="курицы", grams=150)]
    assert parse("без риса") == [Edit("off", name="риса")]
    assert parse("убери рис") == [Edit("off", name="рис")]
    assert parse("рис не ел") == [Edit("off", name="рис")]
    assert parse("без риса и хлеба") == [Edit("off", name="риса"), Edit("off", name="хлеба")]
    assert parse("верни рис") == [Edit("on", name="рис")]
    assert parse("это булгур, а не рис") == [Edit("rename", name="рис", new="булгур")]
    assert parse("не рис, а булгур") == [Edit("rename", name="рис", new="булгур")]
    assert parse("рис → булгур") == [Edit("rename", name="рис", new="булгур")]
    assert parse("рис -> булгур") == [Edit("rename", name="рис", new="булгур")]
    assert parse("замени рис на булгур") == [Edit("rename", name="рис", new="булгур")]
    assert parse("это булгур") == [Edit("rename", new="булгур")]
    for text in ("это вкусно", "это был обед", "это всё", "Это много"):
        assert parse(text) is None, text
    assert parse("курицу съел 150") == [Edit("grams", name="курицу", grams=150, unit=False)]
    assert parse("ещё 2 яйца") == [Edit("add", new="яйца", grams=2, unit=False)]
    assert parse("+1 яйцо") == [Edit("add", new="яйцо", grams=1, unit=False)]
    assert parse("это был булгур 150 г") == [Edit("rename", new="булгур", grams=150)]
    assert parse("добавь соус 30 г") == [Edit("add", new="соус", grams=30)]
    assert parse("+ соус 30г") == [Edit("add", new="соус", grams=30)]
    assert parse("ещё 30 г соуса") == [Edit("add", new="соуса", grams=30)]
    assert parse("добавь соус") == [Edit("add", new="соус")]
    assert parse("курица 150, без риса") == [
        Edit("grams", name="курица", grams=150, unit=False), Edit("off", name="риса")]
    # Не правка: обычный путь решит сам.
    for text in ("вкусно", "сколько осталось?", "борщ 300 г и хлеб", "кофе", "", "2 яйца",
                 "курица 150, вкусно"):
        assert parse(text) is None, text

    comps = [Component("макароны", 0.6, 250), Component("курица", 0.3, 100)]
    grams: list[float] = [250.0, 100.0]
    off: list[int] = []
    apply(Edit("grams", name="курица", grams=150), 1, comps, grams, off, None)
    assert grams == [250, 150] and comps[1].fixed
    apply(Edit("total", grams=400), None, comps, grams, off, None)
    assert grams == [250, 150]
    apply(Edit("grams", name="курица", grams=100), 1, comps, grams, off, 400)
    assert grams == [300, 100]                      # остаток тарелки поделён заново
    apply(Edit("rename", name="макароны", new="булгур, 5"), 0, comps, grams, off, 400)
    assert comps[0].name == "булгур" and grams == [300, 100]
    apply(Edit("add", new="соус", grams=30), None, comps, grams, off, 400)
    assert comps[2].fixed and grams == [300, 100, 30]
    apply(Edit("off", name="соус"), 2, comps, grams, off, 400)
    apply(Edit("off", name="соус"), 2, comps, grams, off, 400)
    assert off == [2]
    apply(Edit("on", name="соус"), 2, comps, grams, off, 400)
    assert off == []
    assert find(["рис отварной", "курица жареная"], "риса") == [0]
    assert find(["рисовая лапша"], "рис") == []
    assert find(["курица жареная", "курица в соусе"], "курица") == [0, 1]
    plate = ["рис отварной", "куриное филе жареное", "пюре картофельное", "стейк"]
    assert find(plate, "курица") == [] and similar(plate, "курица") == [1]
    assert similar(plate, "курицу") == [1]
    assert similar(plate, "картошка") == [2] and similar(plate, "мясо") == [3]
    assert similar(plate, "яблоко") == []
    print("plate_edit ok")


if __name__ == "__main__":
    demo()
