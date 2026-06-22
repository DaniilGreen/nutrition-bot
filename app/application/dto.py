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
    portion_options: dict[str, float] | None = None  # реальные граммы S/M/L пер-блюдо от LLM (FR-21)
    unit_weight_g: float | None = None
    clarify_options: list[str] | None = None  # D-19: варианты-кнопки от LLM (неточный запрос)
    kcal_per_100g: float = 0.0
    protein_per_100g: float = 0.0
    fat_per_100g: float = 0.0
    carb_per_100g: float = 0.0
    total_kcal: float | None = None  # явно названный юзером итог порции (меню/этикетка), FR-22
    total_protein: float | None = None
    total_fat: float | None = None
    total_carb: float | None = None
    confidence: float = 1.0
    estimate_confidence: str | None = None  # high|med|low из разброса сэмплов (D-15)
    from_label: bool = False  # FR-27: КБЖУ прочитаны с фото этикетки → точные, пин source="label"
    serving_g: float | None = None  # FR-27: вес одной порции с этикетки (кнопка «1 порция»)
    label_photo_id: str | None = None  # FR-27: Telegram file_id фото этикетки (храним у продукта)


@dataclass
class ParsedMessage:
    intent: Intent
    items: list[ParsedItem] = field(default_factory=list)
    weight_kg: float | None = None
    correction_op: str | None = None  # delete|replace|adjust|other для intent=correction
    confidence: float = 1.0
