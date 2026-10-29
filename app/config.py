"""Конфигурация через pydantic-settings (читает .env / переменные окружения)."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.goals import Goal

# Рекомендация Anthropic по умолчанию; модель на маршрут владелец выберет по eval (D-41).
CLAUDE_DEFAULT_MODEL = "claude-opus-5-5"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    database_url: str = "postgresql+asyncpg://burnfat:burnfat@localhost:5432/burnfat"
    # Адрес мини-аппа. Не секрет (публичный домен), поэтому с дефолтом: без него
    # кнопка меню осталась бы с зафиксированным вручную URL, и отпечаток статики
    # в неё не попадал бы — то есть защита от кэша работала бы вполсилы.
    miniapp_url: str = "https://nutrition-calorie.greenjola.win"

    # NFR-6, D-41: разбор, фото и ассистент — на OpenAI или Claude. По умолчанию openai: пока
    # владелец не переключил, не меняется ничего. Голос — всегда Whisper (у Claude его нет).
    llm_provider: Literal["openai", "anthropic"] = "openai"
    # SecretStr: ключ не попадёт в repr настроек и в лог, если их кто-то напечатает.
    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: str = ""
    # gpt-4.1 даёт 30k TPM (Tier 1) — на этом лимите ~70% запросов падали с 429, бот был
    # нерабочим. У gpt-5.1 те же деньги, но 500k TPM. По eval на честной метрике 4.1 точнее
    # (0.920 против 0.812), НО это качество на ОТВЕЧЕННЫЙ запрос: с учётом доступности
    # 0.920×0.3 против 0.812×1.0 — выигрывает 5.1. Порог уточнений в промпте — следующим шагом.
    openai_model: str = "gpt-5.1"
    openai_stt_model: str = "whisper-1"
    # Модели по маршрутам (NFR-6). Пусто — умолчание провайдера, см. `model_for`. Модель чужого
    # провайдера — отказ на старте, а не 404 на первом сообщении.
    # ИИ-ассистент вкладки AI (OQ-M3). У OpenAI модель та же, что в разборе еды: держать
    # две — значит держать два набора цен и два поведения.
    nlu_model: str = ""
    coach_model: str = ""
    # Жёсткий месячный потолок в долларах на всю инсталляцию, не на пользователя:
    # платит владелец, и его интересует счёт целиком. Проверяется ДО запроса.
    # Считает ВСЕ вызовы модели (разбор, голос, фото, ассистент) — решение владельца
    # 16.09.2026. Отказывают при превышении только ассистент и фото блюда: запись
    # едой текстом и голосом не блокируется никогда, полнота учёта важнее счёта.
    ai_month_limit_usd: float = 15.0
    use_fake_nlu: bool = False
    # Сэмплов self-consistency (D-15), только для новых продуктов (INV-12). 28.09.2026 парный
    # замер на одних и тех же ответах gpt-5.1 (28 строк golden с сэмплами): медиана трёх и семи
    # не поменяла ни одного вердикта «в допуске»; медианная ошибка 2.9% против 1.5%. Решение
    # владельца — 3: четыре вызова из шести на каждом новом продукте не окупают полтора пункта.
    sample_k: int = 3
    # Повторы SDK — только там, где нет своего цикла (фото, голос, ассистент). У разбора цикл
    # свой (AI-PRINCIPLES §1), и SDK-повторы вкладывались в него: до 15 запросов на вызов.
    openai_max_retries: int = 2
    openai_timeout_s: float = 60.0
    # Фото, этикетки и бланки — своей моделью: смена модели разбора ради экономии не должна
    # молча уводить за собой чтение цифр с бланка, где цена ошибки выше.
    vision_model: str = ""
    # Глубина рассуждений Claude по маршрутам (D-41): у Opus 5.5 рассуждения не выключаются,
    # и effort — главный рычаг цены и задержки. Извлечение — low, ответ с инструментами — medium.
    nlu_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    vision_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    coach_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # Выключить рассуждения на разборе и фото там, где семейство позволяет (Sonnet/Haiku 5.5).
    # По умолчанию нет: Anthropic советует сначала low с рассуждениями; флаг — для сравнения в eval.
    claude_thinking_off: bool = False

    default_timezone: str = "Europe/Moscow"
    default_goal_kcal: float = 2000
    default_goal_protein: float = 150
    default_goal_fat: float = 65
    default_goal_carb: float = 250

    reminder_enabled: bool = True
    reminder_hour: int = 23  # локальное время вечернего итога — 23:59, у самого конца дня
    reminder_minute: int = 59

    log_level: str = "INFO"
    db_echo: bool = False

    def model_for(self, route: Literal["nlu", "vision", "coach"]) -> str:
        """Модель маршрута с учётом провайдера (NFR-6).

        Умолчания OpenAI — ровно прежние (разбор — OPENAI_MODEL, фото и ассистент — gpt-5.1),
        чтобы до переключения не поменялось ничего. ValueError — модель чужого провайдера:
        VISION_MODEL=gpt-5.1, забытый в .env при переключении, иначе ушёл бы в Anthropic 404."""
        explicit = {"nlu": self.nlu_model, "vision": self.vision_model,
                    "coach": self.coach_model}[route]
        if self.llm_provider == "anthropic":
            model = explicit or CLAUDE_DEFAULT_MODEL
        else:
            model = explicit or (self.openai_model if route == "nlu" else "gpt-5.1")
        if model.startswith("claude") != (self.llm_provider == "anthropic"):
            raise ValueError(
                f"{route.upper()}_MODEL={model} — не модель провайдера {self.llm_provider}"
            )
        return model

    def default_goal(self) -> Goal:
        return Goal(
            daily_kcal=self.default_goal_kcal,
            protein_g=self.default_goal_protein,
            fat_g=self.default_goal_fat,
            carb_g=self.default_goal_carb,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
