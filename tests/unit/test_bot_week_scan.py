"""Бот: недельный баланс в итоге и приём бланка InBody (§6.23–6.24, «Бот»)."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TgUser

from app.application.body_scans import GoalProposal, ScanCheck
from app.bot import handlers
from app.bot.formatters import format_goal_proposal, format_scan_card
from app.bot.keyboards import scan_keyboard
from app.bot.ui import blocks
from app.domain.body_scan import BodyScan
from app.domain.goals import Goal
from app.domain.week import balance
from tests.unit.test_bot_photo import _State

SCAN = BodyScan(measured_at=datetime(2026, 5, 3, 8, 44, tzinfo=UTC), weight_kg=91.8,
                body_fat_kg=19.8, fat_free_kg=72.0, body_fat_pct=21.6,
                skeletal_muscle_kg=41.4, water_l=52.8, visceral_level=9, bmr_kcal=1925,
                height_cm=180, age=30, sex="male", device="InBody270")


def _check(**kw) -> ScanCheck:
    base = dict(scan=SCAN, bad=set(), foreign=False, date_ambiguous=False,
                date_problem=None, existing=None)
    base.update(kw)
    return ScanCheck(**base)


def _buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def test_карточка_бланка_в_часовом_поясе_и_без_отметок():
    text = format_scan_card(_check(), "Etc/GMT-7")
    assert "03.05.2026 15:44" in text and "Вес: <b>91,8</b> кг" in text
    assert "⚠️" not in text and "Минералы" not in text
    assert [b.text for b in _buttons(scan_keyboard(_check()))] == ["💾 Сохранить", "✖️ Отмена"]


def test_несошедшееся_отмечено_и_сохраняется_только_подтверждением():
    chk = _check(bad={"body_fat_kg", "weight_kg"})
    text = format_scan_card(chk, "UTC")
    assert "⚠️ Жир" in text and "⚠️ Вес" in text and "мини-аппе" in text
    labels = [b.text for b in _buttons(scan_keyboard(chk))]
    assert labels[0] == "✅ Всё верно — сохранить"
    assert all("Сохранить" != x for x in labels)


def test_будущая_дата_без_сохранения():
    chk = _check(date_problem="future")
    assert [b.text for b in _buttons(scan_keyboard(chk))] == ["✖️ Отмена"]
    assert "будущем" in format_scan_card(chk, "UTC")


def test_повтор_бланка_обновляет():
    chk = _check(existing=SCAN)
    assert _buttons(scan_keyboard(chk))[0].text == "💾 Обновить замер"
    assert "сохраню поверх" in format_scan_card(chk, "UTC")


def test_было_стало():
    prop = GoalProposal(current=Goal(2176, 194, 72, 188, "cut"),
                        proposed=Goal(2251, 173, 75, 221, "cut"), bmr_kcal=1925,
                        bmr_source="inbody", activity="medium", burn_kcal=2647,
                        base="estimate", fat_free_kg=72.0)
    text = format_goal_proposal(prop)
    assert "Белок: 194 → <b>173</b> г (−21)" in text
    assert "1925 обмен × 1,375 активность × похудение" in text


def _ctx(kcal, today_idx, goal=2176):
    mon = date(2026, 9, 14)
    days = [(mon + timedelta(days=i), k, goal) for i, k in enumerate(kcal)]
    w = balance(days, today=mon + timedelta(days=today_idx))
    return SimpleNamespace(week_bal=w)


def test_неделя_в_итоге_дня():
    d = _ctx([1800, 1800, 1800, 1800, 2556, None, None], 4)
    text = blocks.week_balance(d)
    assert "в запасе 1124 ккал" in text and "по 2738" in text
    # Понедельник — молчим.
    assert blocks.week_balance(_ctx([1800] + [None] * 6, 0)) == ""
    # Ниже пола — «не догнать».
    low = _ctx([2176, 2176, 2176, 2176, 9000, None, None], 4)
    assert "не догнать" in blocks.week_balance(low)


def test_воскресная_сводка_по_записанным_дням():
    d = _ctx([2000, 2000, None, 2000, 2000, 2000, 2300], 6)
    text = blocks.week_balance(d)
    assert "Цель за 6 дн." in text and "Записано 6 из 7" in text
    assert "в запасе 756 ккал" in text
    assert "Итог" not in blocks.week_balance(d, sunday_summary=False)
    # /week в воскресенье показывает тот же итог, что вечернее сообщение (§6.23).
    from app.bot.ui import screens
    assert "Итог" in blocks.week_balance(d)
    del screens


def _callback(data: str) -> CallbackQuery:
    bot = AsyncMock()
    msg = Message.model_construct(
        message_id=5, date=None, chat=Chat.model_construct(id=1, type="private")
    ).as_(bot)
    cb = CallbackQuery.model_construct(
        id="1", from_user=TgUser.model_construct(id=1, is_bot=False, first_name="T"),
        chat_instance="ci", data=data, message=msg,
    )
    return cb.as_(bot)


async def test_сохранение_без_подтверждения_при_расхождении():
    scans = AsyncMock()
    scans.save.side_effect = ValueError("mismatch")
    state = _State({"scan": SCAN, "ambiguous": False})
    cb = _callback("scan:save")
    await handlers.on_scan_btn(cb, object(), AsyncMock(), state, scans)
    assert scans.save.await_args.kwargs["confirmed"] is False
    assert not state.cleared
    sent = [c.args[0] for c in cb.bot.await_args_list]
    assert any("Всё верно" in (getattr(m, "text", None) or "") for m in sent)


async def test_подтверждение_сохраняет_и_предлагает_цели():
    scans = AsyncMock()
    scans.save.return_value = (replace(SCAN, id=1), False)
    prop = GoalProposal(current=Goal(2176, 194, 72, 188, "cut"),
                        proposed=Goal(2251, 173, 75, 221, "cut"), bmr_kcal=1925,
                        bmr_source="inbody", activity="medium", burn_kcal=2647,
                        base="estimate", fat_free_kg=72.0)
    scans.overview.return_value = SimpleNamespace(proposal=prop)
    state = _State({"scan": SCAN, "ambiguous": True})
    cb = _callback("scan:confirm")
    await handlers.on_scan_btn(cb, object(), AsyncMock(), state, scans)
    kw = scans.save.await_args.kwargs
    assert kw["confirmed"] is True and kw["date_ambiguous"] is True
    assert state.cleared
    last = cb.bot.await_args_list[-1].args[0]
    assert "Цели по замеру" in last.text and last.reply_markup is not None


async def test_устаревшая_карточка_бланка():
    scans = AsyncMock()
    cb = _callback("scan:save")
    await handlers.on_scan_btn(cb, object(), AsyncMock(), _State({}), scans)
    scans.save.assert_not_awaited()
