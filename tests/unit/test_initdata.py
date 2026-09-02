"""Проверка подписи initData (§7.1, MA-INV-6/7/8).

Счастливый путь тут наименее интересен: ценность в отказах. Каждый тест —
конкретный способ подделать или испортить строку.
"""
from __future__ import annotations

import json
import time

import pytest

from app.web.auth import MAX_AGE_SECONDS, InitDataError, verify_init_data
from tests.initdata_fixtures import TOKEN, sign, valid  # noqa: F401


def test_подлинная_строка_даёт_пользователя():
    u = verify_init_data(valid(), TOKEN)
    assert u.telegram_id == 777
    assert u.username == "dg"


def test_подменённый_user_ломает_подпись():
    """Главный сценарий атаки: подставить чужой id, оставив чужую подпись."""
    good = valid()
    evil = good.replace("777", "778")
    assert evil != good
    with pytest.raises(InitDataError):
        verify_init_data(evil, TOKEN)


def test_чужой_токен_не_подходит():
    with pytest.raises(InitDataError):
        verify_init_data(valid(), "999:OTHER")


def test_протухшая_строка():
    old = str(int(time.time()) - MAX_AGE_SECONDS - 60)
    with pytest.raises(InitDataError, match="просрочена"):
        verify_init_data(valid(auth_date=old), TOKEN)


def test_на_границе_срока_ещё_годна():
    edge = str(int(time.time()) - MAX_AGE_SECONDS + 60)
    assert verify_init_data(valid(auth_date=edge), TOKEN).telegram_id == 777


def test_дата_из_будущего():
    future = str(int(time.time()) + 3600)
    with pytest.raises(InitDataError, match="будущего"):
        verify_init_data(valid(auth_date=future), TOKEN)


def test_небольшое_расхождение_часов_прощаем():
    skew = str(int(time.time()) + 60)
    assert verify_init_data(valid(auth_date=skew), TOKEN).telegram_id == 777


@pytest.mark.parametrize(
    "bad",
    ["", "hash=abc", "user=%7B%7D&auth_date=1", "не=строка&hash=zz"],
    ids=["пусто", "только-hash", "без-подписи", "мусор"],
)
def test_испорченные_строки(bad):
    with pytest.raises(InitDataError):
        verify_init_data(bad, TOKEN)


def test_пустой_токен_не_принимаем():
    """Иначе secret_key стабилен и подпись подделывается без знания токена."""
    with pytest.raises(InitDataError, match="BOT_TOKEN"):
        verify_init_data(valid(), "")


def test_без_поля_user():
    fields = {"auth_date": str(int(time.time())), "query_id": "AAE"}
    with pytest.raises(InitDataError, match="user"):
        verify_init_data(sign(fields), TOKEN)


def test_user_без_числового_id():
    broken = json.dumps({"id": "777"}, separators=(",", ":"))
    with pytest.raises(InitDataError, match="числового id"):
        verify_init_data(valid(user=broken), TOKEN)


def test_дубль_ключа_отвергаем():
    """parse_qsl оставил бы последний, а Telegram подписывал другой набор."""
    with pytest.raises(InitDataError):
        verify_init_data(valid() + "&auth_date=1", TOKEN)
