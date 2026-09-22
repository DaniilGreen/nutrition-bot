"""Прогон доменных `demo()` как теста.

По CLAUDE.md §3 нетривиальная логика оставляет один runnable-чек — `demo()` с
ассертами. Но CI гоняет `pytest`, а не модули, и до 22.09.2026 эти ассерты были
документацией, а не гейтом: сломать их можно было, не уронив ни одного теста.
Один параметризованный тест возвращает им смысл.
"""
from __future__ import annotations

import importlib
import pkgutil

import pytest

import app.domain


def _modules_with_demo() -> list[str]:
    names = []
    for info in pkgutil.iter_modules(app.domain.__path__):
        module = importlib.import_module(f"app.domain.{info.name}")
        if callable(getattr(module, "demo", None)):
            names.append(info.name)
    return sorted(names)


@pytest.mark.parametrize("name", _modules_with_demo())
def test_доменная_демка_проходит(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    importlib.import_module(f"app.domain.{name}").demo()
    capsys.readouterr()  # демки печатают «…: ок» — в отчёт это не нужно
