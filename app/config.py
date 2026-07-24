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
    openai_model: str = "gpt-4.1"
    openai_stt_model: str = "whisper-1"
    use_fake_nlu: bool = False
    sample_k: int = 3  # сэмплов self-consistency (D-15); 5→3 ради ↓токенов и реже 429 (TPM 30k)
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
