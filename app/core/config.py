from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ReasoningEffort = Literal[
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
]


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://integra:integra@localhost:5432/integra"
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5.4-mini-2026-03-17"
    llm_reasoning_effort: ReasoningEffort | None = None
    app_env: str = "development"
    tenant_profiles_directory: str = "examples"
    worker_poll_interval_seconds: float = Field(default=0.5, gt=0)
    worker_lease_seconds: int = Field(default=60, ge=1)
    worker_max_retries: int = Field(default=4, ge=0)
    approval_timeout_seconds: int = Field(default=86_400, ge=1)

    @field_validator("llm_reasoning_effort", mode="before")
    @classmethod
    def empty_reasoning_effort_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


settings = Settings()
