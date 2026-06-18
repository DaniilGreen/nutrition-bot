"""DTO результата разбора сообщения. Контракт между NLU и сервисами.

Числовые поля *_per_100g — первичная оценка плотности от LLM. Используются
ТОЛЬКО когда продукт впервые встречается и создаётся в каталоге. Для уже
известного продукта плотность берётся из БД (детерминизм).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Intent(str, Enum):
    MEAL = "meal"
    WEIGHT = "weight"
    QUERY = "query"
    CORRECTION = "correction"
    UNKNOWN = "unknown"


class Unit(str, Enum):
    GRAM = "g"
    ML = "ml"
    PIECE = "piece"
    PORTION = "portion"
    UNKNOWN = "unknown"


@dataclass
class ParsedItem:
    name: str
    canonical_key: str
    unit: Unit
    quantity: float | None = None
    grams: float | None = None
    portion_hint: str | None = None
    unit_weight_g: float | None = None
    kcal_per_100g: float = 0.0
    protein_per_100g: float = 0.0
    fat_per_100g: float = 0.0
    carb_per_100g: float = 0.0
    confidence: float = 1.0


@dataclass
class ParsedMessage:
    intent: Intent
    items: list[ParsedItem] = field(default_factory=list)
    weight_kg: float | None = None
    correction_op: str | None = None  # delete|replace|adjust|other для intent=correction
    confidence: float = 1.0
