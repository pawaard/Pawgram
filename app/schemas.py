from pydantic import BaseModel, Field, field_validator


class LoginStartRequest(BaseModel):
    phone: str = Field(min_length=7, max_length=24)
    label: str = Field(default="Telegram hesabı", min_length=2, max_length=80)


class LoginVerifyRequest(BaseModel):
    phone: str = Field(min_length=7, max_length=24)
    code: str = Field(min_length=3, max_length=12)
    password: str | None = Field(default=None, max_length=256)


class GroupResolveRequest(BaseModel):
    session_id: int
    reference: str = Field(min_length=2, max_length=256)


class TelegramSettingsRequest(BaseModel):
    api_id: int = Field(gt=0)
    api_hash: str = Field(min_length=20, max_length=128)


class RotationSettingsRequest(BaseModel):
    daily_quota: int = Field(default=30, ge=1, le=1000)


class ProxySettingsRequest(BaseModel):
    enabled: bool = False
    proxy_type: str = Field(default="socks5", pattern=r"^(socks5|http)$")
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=512)

    @field_validator("host")
    @classmethod
    def normalize_host(cls, value: str | None) -> str | None:
        return value.strip() if value else None


class AdminPasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class ActivityScanRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    session_id: int | None = None
    group_ref: str = Field(min_length=2, max_length=256)
    window_hours: int
    recurring: bool = False
    interval_minutes: int = Field(default=1440, ge=60, le=43200)

    @field_validator("window_hours")
    @classmethod
    def allowed_activity_window(cls, value: int) -> int:
        if value not in {24, 72, 168, 720}:
            raise ValueError("Aktivite aralığı 24 saat, 3 gün, 7 gün veya 30 gün olmalı.")
        return value


class JobCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    session_id: int
    source_ref: str = Field(min_length=2, max_length=256)
    target_ref: str = Field(min_length=2, max_length=256)
    max_users: int = Field(default=25, ge=1, le=1000)
    min_delay_seconds: int = Field(default=45, ge=10, le=3600)
    max_delay_seconds: int = Field(default=90, ge=10, le=7200)
    daily_limit: int = Field(default=50, ge=1, le=1000)
    dry_run: bool = True
    scheduled_at: str | None = Field(default=None, max_length=40)
    working_start: str = Field(default="09:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    working_end: str = Field(default="22:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    @field_validator("target_ref")
    @classmethod
    def source_and_target_are_checked_later(cls, value: str) -> str:
        return value.strip()


class CandidateSelectionRequest(BaseModel):
    candidate_ids: list[int] = Field(default_factory=list, max_length=1000)
