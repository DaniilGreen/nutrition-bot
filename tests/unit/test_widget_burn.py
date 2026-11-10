"""«🔥 Реальный расход» виджетом (FR-50, AC-295, D-65). Фейки: без БД и без Telegram.

Держим: числа — ровно те, что отдаёт `BodyScanService.overview` «Итогам» аппки (MA-INV-1), а не
второй расчёт; каждое состояние говорит, чего не хватает; цель — только при узком разбросе.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.bot.ui.widgets import LIMIT, burn_data
from app.bot.widgets import burn, template
from app.bot.widgets.transport import SendWidget
from app.domain import energy

TODAY = date(2026, 11, 10)


def _scan(d: date) -> SimpleNamespace:
    return SimpleNamespace(measured_at=datetime(d.year, d.month, d.day, 8, tzinfo=UTC))


def _ov(**kw: object) -> SimpleNamespace:
    base = dict(scans=[_scan(date(2026, 9, 1)), _scan(date(2026, 10, 31))], spend=None,
                diary=None, diary_since=None, proposal=None, burn_kcal=2350.4)
    base.update(kw)
    return SimpleNamespace(**base)


def test_расход_те_же_числа_что_итоги() -> None:
    """AC-295: вилка = tdee ± error из `energy.expenditure` — то самое, что «Итоги» аппки."""
    sp = energy.expenditure(days=60, logged_days=55, logged_kcal_total=2100 * 55,
                            d_fat_kg=-3.0, d_lean_kg=-0.5)
    diary = energy.diary_check(days=60, logged_days=55, logged_kcal_total=2100 * 55,
                               burn_kcal=2350, actual_weight_kg=-2.4)
    prop = SimpleNamespace(base="energy", changed=True,
                           proposed=SimpleNamespace(daily_kcal=2064.2))
    d = json.loads(burn_data(_ov(spend=sp, diary=diary, diary_since=date(2026, 9, 1),
                                 proposal=prop), TODAY))
    assert d["st"] == "ok" and d["t"] == sp.tdee_kcal and d["er"] == sp.error_kcal
    assert d["in"] == sp.intake_kcal and d["fb"] == 2350 and d["pp"] == 2064
    assert d["ld"] == 55 and d["dd"] == 60 and d["dt"] == "Сходится" and d["da"] == 0
    assert all(isinstance(v, int) for k, v in d.items() if k in ("t", "er", "in", "fb", "pp"))


def test_широкий_разброс_цель_не_предлагается() -> None:
    sp = energy.expenditure(days=30, logged_days=28, logged_kcal_total=2100 * 28,
                            d_fat_kg=-1.5, d_lean_kg=-0.2)
    prop = SimpleNamespace(base="estimate", changed=True,
                           proposed=SimpleNamespace(daily_kcal=1900))
    d = json.loads(burn_data(_ov(spend=sp, proposal=prop), TODAY))
    assert d["st"] == "ok" and "pp" not in d and "ещё один замер через месяц" in d["nt"]


@pytest.mark.parametrize(("ov", "st", "words"), [
    (_ov(scans=[]), "none", "Пришли фото бланка InBody"),
    (_ov(scans=[_scan(date(2026, 11, 1))]), "wait", "не раньше 29 ноя"),
    (_ov(scans=[_scan(date(2026, 9, 1))]), "wait", "зазор с прошлым уже больше 4 недель"),
    (_ov(spend=energy.expenditure(days=60, logged_days=30, logged_kcal_total=60000,
                                  d_fat_kg=-1, d_lean_kg=0)),
     "few", "Записано 30 полных дн. из 60, нужно от 48"),
])
def test_состояния_говорят_чего_не_хватает(ov: SimpleNamespace, st: str, words: str) -> None:
    d = json.loads(burn_data(ov, TODAY))
    assert d["st"] == st and words in d["nt"]


def test_сверка_тревога_словами_аппки() -> None:
    diary = energy.diary_check(days=40, logged_days=38, logged_kcal_total=1800 * 38,
                               burn_kcal=2400, actual_weight_kg=-0.5)
    d = json.loads(burn_data(_ov(diary=diary, diary_since=date(2026, 9, 1)), TODAY))
    assert d["dt"] == "Ушло меньше, чем ждали" and d["da"] == 1 and "−0,5 кг вместо" in d["dx"]


def test_шаблон_и_данные_в_бюджете() -> None:
    sp = energy.expenditure(days=60, logged_days=55, logged_kcal_total=2100 * 55,
                            d_fat_kg=-3.0, d_lean_kg=-0.5)
    diary = energy.diary_check(days=60, logged_days=20, logged_kcal_total=2100 * 20,
                               burn_kcal=2350, actual_weight_kg=None)
    text = burn_data(_ov(spend=sp, diary=diary, diary_since=date(2026, 9, 1)), TODAY)
    assert len(text.encode()) <= LIMIT
    html = template("burn")
    for k in json.loads(text):
        assert k == "v" or f"d.{k}" in html, k


class _Scans:
    def __init__(self, ov: SimpleNamespace) -> None:
        self.ov = ov

    async def overview(self, user: object) -> SimpleNamespace:
        return self.ov


class _Bot:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[object] = []
        self.fail = fail

    async def __call__(self, m: object) -> object:
        if self.fail:
            raise RuntimeError("400")
        self.calls.append(m)
        return SimpleNamespace(message_id=1)


async def test_показ_виджет_или_текст(monkeypatch: pytest.MonkeyPatch) -> None:
    """С флагом — виджет `burn` с подписью без цифр и кнопкой на «Итоги»; сбой — те же строки
    текстом, ни одного сообщения сверх этого."""
    from unittest.mock import AsyncMock

    from app.application.entities import User
    from app.bot import handlers

    user = User(telegram_id=1, name="x", locale="ru", timezone="UTC", id=1)
    scans = _Scans(_ov(scans=[]))
    bot = _Bot()
    assert await burn.show(bot, 1, user, scans)  # type: ignore[arg-type]
    [sent] = bot.calls
    assert isinstance(sent, SendWidget) and sent.html == template("burn")
    assert sent.caption is None  # D-75
    monkeypatch.setattr(handlers.widgets_live, "enabled", lambda u, s=None: True)
    monkeypatch.setattr(handlers.widgets_burn, "show", AsyncMock(return_value=False))
    msg = AsyncMock()
    msg.chat = SimpleNamespace(id=1)
    await handlers._show_burn(msg, user, scans)  # type: ignore[arg-type]
    text = msg.answer.await_args.args[0]
    assert text.startswith("🔥 Реальный расход\n") and "бланка InBody" in text
    assert msg.answer.await_count == 1


async def test_кнопка_расхода_под_весом() -> None:
    """Вход в FR-50 — кнопка В САМОМ виджете «⚖️ Вес» (`k:burn`, D-82): под сообщением только
    аппка; виджет принимает запросы; колбэк тот же — его ловит `on_burn`."""
    from datetime import timedelta

    from app.bot.widgets import live

    t0 = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
    bot = _Bot()
    assert await live.show_weight(bot, 1, [(t0 + timedelta(days=i), 86 - i / 10) for i in range(5)],  # type: ignore[arg-type]
                                  "UTC", "cut")
    sent = bot.calls[0]
    kb = sent.reply_markup
    assert sent.allow_queries and f"data-k={burn.CALLBACK}" in sent.html
    assert kb is None or not any(b.callback_data for r in kb.inline_keyboard for b in r)
