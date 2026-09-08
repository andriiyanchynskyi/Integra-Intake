from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://integra:integra@localhost:5432/integra"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    app_env: str = "development"


settings = Settings()
