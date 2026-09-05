"""Статика версионируется отпечатком (иначе новый фронт не доезжает до телефона).

05.09.2026: выкатили экран без мигания и камеру этикеток, сервер отдавал верный
`app.js` — а на телефоне остался старый. Telegram кэширует мини-апп жёстче
браузера, и одинаковый URL ему нечем отличить от вчерашнего.
"""
from __future__ import annotations

import httpx
import pytest

from app.main_api import WEB_DIR, app, asset_version, index_html


@pytest.fixture
def client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def test_ссылки_на_статику_действительно_подменяются():
    """Главная ловушка: замена по точной строке. Поменяют разметку в index.html —
    подстановка тихо перестанет срабатывать, и страница снова замёрзнет в кэше."""
    source = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    out = index_html("deadbeef", source)
    assert 'href="/app.css?v=deadbeef"' in out
    assert 'src="/app.js?v=deadbeef"' in out
    # И ни одной ссылки без версии не осталось.
    assert 'href="/app.css"' not in out
    assert 'src="/app.js"' not in out


def test_отпечаток_идёт_за_содержимым(tmp_path, monkeypatch):
    """Версия обязана меняться вместе с файлом, иначе она бесполезна."""
    import app.main_api as m

    js = tmp_path / "app.js"
    css = tmp_path / "app.css"
    js.write_text("одно", encoding="utf-8")
    css.write_text("c", encoding="utf-8")
    monkeypatch.setattr(m, "WEB_DIR", tmp_path)

    first = m.asset_version()
    js.write_text("другое", encoding="utf-8")
    assert m.asset_version() != first
    js.write_text("одно", encoding="utf-8")
    assert m.asset_version() == first  # то же содержимое — тот же URL, кэш живёт


async def test_корень_отдаёт_версию_и_не_кэшируется(client):
    async with client as c:
        r = await c.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # Страница-точка-входа НЕ кэшируется: иначе версия внутри неё замёрзнет
    # вместе с ней, и отпечаток перестанет что-либо решать.
    assert "no-store" in r.headers.get("cache-control", "")
    assert f'/app.js?v={asset_version()}' in r.text


async def test_статика_по_версионному_адресу_отдаётся(client):
    """Строка запроса не должна мешать StaticFiles отдать файл."""
    async with client as c:
        r = await c.get(f"/app.js?v={asset_version()}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
