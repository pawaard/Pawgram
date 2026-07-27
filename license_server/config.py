from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


SERVER_DIR = Path(__file__).resolve().parent


class LicenseServerSettings(BaseSettings):
    admin_api_key: str
    database_path: Path = SERVER_DIR / "data" / "licenses.db"
    signing_key_path: Path = SERVER_DIR / "data" / "signing_key.pem"
    public_key_path: Path = SERVER_DIR / "public_key.pem"
    lease_hours: int = 24
    host: str = "127.0.0.1"
    port: int = 8010
    cookie_secure: bool = False

    model_config = SettingsConfigDict(
        env_prefix="LICENSE_",
        env_file=SERVER_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_license_server_settings() -> LicenseServerSettings:
    return LicenseServerSettings()
