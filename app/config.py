"""Конфигурация через pydantic-settings (читает .env / переменные окружения)."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.goals import Goal


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    database_url: str = "postgresql+asyncpg://burnfat:burnfat@localhost:5432/burnfat"

    llm_provider: str = "openai"
    openai_api_key: str = ""
    # gpt-4.1 даёт 30k TPM (Tier 1) — на этом лимите ~70% запросов падали с 429, бот был
    # нерабочим. У gpt-5.1 те же деньги, но 500k TPM. По eval на честной метрике 4.1 точнее
    # (0.920 против 0.812), НО это качество на ОТВЕЧЕННЫЙ запрос: с учётом доступности
    # 0.920×0.3 против 0.812×1.0 — выигрывает 5.1. Порог уточнений в промпте — следующим шагом.
    openai_model: str = "gpt-5.1"
    openai_stt_model: str = "whisper-1"
    use_fake_nlu: bool = False
    # Сэмплов self-consistency (D-15). Резали до 3 из-за лимита 30k TPM на gpt-4.1; на gpt-5.1
    # потолок 500k, а сэмплы идут параллельно (asyncio.gather) — задержка от k почти не растёт.
    # Медиана по семи устойчивее к выбросу, это прямой вклад в точность (приоритет №1), а платим
    # только на приёмах с оценкой: вес/статистика/коррекции/заявленные ккал идут одним вызовом.
    sample_k: int = 7
    openai_max_retries: int = 4  # ретраи SDK на 429/сеть — переждать окно TPM-лимита (баг #16)

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
