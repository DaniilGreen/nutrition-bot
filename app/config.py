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
    openai_model: str = "gpt-4o-mini"
    openai_stt_model: str = "whisper-1"
    use_fake_nlu: bool = False

    default_timezone: str = "Europe/Moscow"
    default_goal_kcal: float = 2000
    default_goal_protein: float = 150
    default_goal_fat: float = 65
    default_goal_carb: float = 250
    confidence_auto_threshold: float = 0.6

    reminder_enabled: bool = True
    reminder_hour: int = 21  # локальный час вечернего итога

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
