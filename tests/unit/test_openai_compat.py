"""Совместимость с gpt-5.x: temperature нельзя слать явно (400 на каждом вызове).

Почему тест структурный, а не обычный: остальные тесты гоняются на in-memory фейках и до
реального API не доходят — вписанный обратно `temperature=0` они пропустят, CI будет зелёным,
а прод ляжет на первом же сообщении. Поэтому проверяем сами исходники адаптеров.
"""
from __future__ import annotations

import ast
from pathlib import Path

from openai import Omit

from app.infrastructure import openai_compat
from app.infrastructure.nlu import openai_nlu
from app.infrastructure.vision import openai_vision

ADAPTERS = (openai_nlu, openai_vision)


def test_gpt5_gets_no_temperature() -> None:
    """gpt-5.x принимает только дефолтную temperature=1 — явную не шлём вовсе."""
    for model in ("gpt-5.1", "gpt-5.6-sol", "gpt-5-mini"):
        assert isinstance(openai_compat.temperature_for(model, 0.0), Omit), model
        assert not openai_compat.supports_temperature(model), model


def test_older_models_keep_temperature() -> None:
    """На gpt-4.1 поведение не меняется: temperature по-прежнему передаётся."""
    assert openai_compat.temperature_for("gpt-4.1", 0.0) == 0.0
    assert openai_compat.temperature_for("gpt-4.1", 0.6) == 0.6
    assert openai_compat.supports_temperature("gpt-4.1")


def test_adapters_never_pass_raw_temperature() -> None:
    """Страж класса: temperature мимо `temperature_for` снова убьёт gpt-5.x.

    Ловит ровно то, что пропустил бы обычный тест на фейках: жёстко вписанное значение
    (`temperature=0`) или проброс переменной напрямую.
    """
    offenders: list[str] = []
    for mod in ADAPTERS:
        path = Path(mod.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func  # интересуют вызовы вида ...completions.parse/create(...)
            if not isinstance(func, ast.Attribute) or func.attr not in ("parse", "create"):
                continue
            for kw in node.keywords:
                if kw.arg != "temperature":
                    continue
                val = kw.value
                ok = (
                    isinstance(val, ast.Call)
                    and isinstance(val.func, ast.Name)
                    and val.func.id == "temperature_for"
                )
                if not ok:
                    offenders.append(f"{path.name}:{val.lineno}")

    assert not offenders, (
        "temperature уходит в API мимо temperature_for() — на gpt-5.x это 400 на каждом "
        f"сообщении: {offenders}"
    )
