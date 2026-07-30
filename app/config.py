import sys
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.edition import COMMERCIAL_EDITION, COMMERCIAL_LICENSE_SERVER_URL

SOURCE_DIR = Path(__file__).resolve().parent.parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR))
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else SOURCE_DIR


class Settings(BaseSettings):
    app_name: str = "Pawgram"
    app_env: str = "development"
    app_port: int = Field(default=8000, ge=1, le=65535)
    customer_release: bool = False
    app_secret_key: str = "change-this-in-production"
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    default_proxy_type: str = Field(default="socks5", pattern=r"^(socks5|http)$")
    default_proxy_host: str | None = None
    default_proxy_port: int | None = Field(default=None, ge=1, le=65535)
    default_proxy_username: str | None = None
    default_proxy_password: str | None = None
    default_proxy_revision: str | None = None
    database_path: Path = APP_DIR / "data" / "console.db"
    license_required: bool = False
    license_server_url: str = "http://127.0.0.1:8010"
    license_request_timeout: float = Field(default=8.0, gt=0, le=60)
    license_check_interval_minutes: int = Field(default=30, ge=5, le=1440)

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

    @property
    def effective_license_server_url(self) -> str:
        return COMMERCIAL_LICENSE_SERVER_URL if COMMERCIAL_EDITION else self.license_server_url

    @property
    def licensing_online_required(self) -> bool:
        return COMMERCIAL_EDITION

    @property
    def license_refresh_interval_seconds(self) -> int:
        if self.licensing_online_required:
            return 60
        return max(300, self.license_check_interval_minutes * 60)


@lru_cache
def get_settings() -> Settings:
    return Settings()
