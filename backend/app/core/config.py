from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    APP_NAME: str = "Ajaia Doc Editor"
    ENVIRONMENT: str = "development"
    API_PREFIX: str = "/api"
    DATABASE_URL: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/ajaia_doc_editor"
    )

    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
