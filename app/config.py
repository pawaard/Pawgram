from functools import lru_cache
from pathlib import Path
import sys

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.edition import COMMERCIAL_EDITION


SOURCE_DIR = Path(__file__).resolve().parent.parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR))
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else SOURCE_DIR
BASE_DIR = APP_DIR


class Settings(BaseSettings):
    app_name: str = "Pawgram"
    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    app_secret_key: str = "change-this-in-production"
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    database_path: Path = APP_DIR / "data" / "console.db"
    license_required: bool = False
    license_server_url: str = "http://127.0.0.1:8010"
    license_request_timeout: float = 8.0
    license_check_interval_minutes: int = 30

    model_config = SettingsConfigDict(
        env_file=APP_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_api_id and self.telegram_api_hash)

    @property
    def licensing_enforced(self) -> bool:
        return COMMERCIAL_EDITION or self.license_required


@lru_cache
def get_settings() -> Settings:
    return Settings()
