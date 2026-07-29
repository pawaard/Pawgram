from pydantic import BaseModel, Field, field_validator

from app.scheduling import normalize_datetime


class LoginStartRequest(BaseModel):
    phone: str = Field(min_length=7, max_length=24)
    label: str = Field(default="Telegram hesabı", min_length=2, max_length=80)
    use_proxy: bool = True
    proxy_type: str = Field(default="socks5", pattern=r"^(socks5|http)$")
    proxy_host: str | None = Field(default=None, max_length=255)
    proxy_port: int | None = Field(default=None, ge=1, le=65535)
    proxy_username: str | None = Field(default=None, max_length=255)
    proxy_password: str | None = Field(default=None, max_length=512)

    @field_validator("proxy_host")
    @classmethod
    def normalize_proxy_host(cls, value: str | None) -> str | None:
        return value.strip() if value else None


class LoginVerifyRequest(BaseModel):
    phone: str = Field(min_length=7, max_length=24)
    code: str = Field(min_length=3, max_length=12)
    password: str | None = Field(default=None, max_length=256)


class LoginCancelRequest(BaseModel):
    phone: str = Field(min_length=7, max_length=24)


class GroupResolveRequest(BaseModel):
    session_id: int
    reference: str = Field(min_length=2, max_length=256)


class GroupAccessBatchRequest(BaseModel):
    group_ref: str = Field(min_length=2, max_length=256)
    purpose: str = Field(default="source", pattern=r"^(source|target)$")
    session_ids: list[int] = Field(min_length=1, max_length=500)
    min_delay_seconds: int = Field(default=15, ge=0, le=3600)
    max_delay_seconds: int = Field(default=30, ge=0, le=3600)

    @field_validator("group_ref")
    @classmethod
    def normalize_group_ref(cls, value: str) -> str:
        return value.strip()

    @field_validator("session_ids")
    @classmethod
    def normalize_session_ids(cls, value: list[int]) -> list[int]:
        if any(session_id <= 0 for session_id in value):
            raise ValueError("Session ID değerleri pozitif olmalıdır.")
        return list(dict.fromkeys(value))


class SessionHealthBatchRequest(BaseModel):
    session_ids: list[int] = Field(min_length=1, max_length=500)
    source_ref: str | None = Field(default=None, max_length=256)
    target_ref: str | None = Field(default=None, max_length=256)

    @field_validator("session_ids")
    @classmethod
    def normalize_session_ids(cls, value: list[int]) -> list[int]:
        if any(session_id <= 0 for session_id in value):
            raise ValueError("Session ID değerleri pozitif olmalıdır.")
        return list(dict.fromkeys(value))

    @field_validator("source_ref", "target_ref")
    @classmethod
    def normalize_optional_group_ref(cls, value: str | None) -> str | None:
        normalized = value.strip() if value else ""
        return normalized or None


class TelegramSettingsRequest(BaseModel):
    api_id: int = Field(gt=0)
    api_hash: str = Field(min_length=20, max_length=128)


class RotationSettingsRequest(BaseModel):
    daily_quota: int = Field(default=30, ge=1, le=1000)


class HeartbeatSettingsRequest(BaseModel):
    enabled: bool = False
    interval_minutes: int = Field(default=60, ge=1, le=10080)
    group_id: str = Field(default="", max_length=64)
    message_template: str = Field(default="Merhabaa", min_length=1, max_length=4096)

    @field_validator("group_id")
    @classmethod
    def normalize_group_id(cls, value: str) -> str:
        normalized = value.strip()
        if normalized and not normalized.lstrip("-").isdigit():
            raise ValueError("Heartbeat Group ID yalnızca sayısal Telegram grup ID'si olmalıdır.")
        return normalized

    @field_validator("message_template")
    @classmethod
    def normalize_message_template(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Heartbeat mesajı boş olamaz.")
        return normalized


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


class SessionInvitePolicyRequest(BaseModel):
    batch_limit: int = Field(default=3, ge=1, le=20)
    cooldown_minutes: int = Field(default=20, ge=5, le=240)


class DefaultProxySettingsRequest(BaseModel):
    proxy_type: str = Field(default="socks5", pattern=r"^(socks5|http)$")
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=512)

    @field_validator("host")
    @classmethod
    def normalize_host(cls, value: str) -> str:
        return value.strip()


class ProxyBulkImportRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2_000_000)
    default_proxy_type: str = Field(default="socks5", pattern=r"^(socks5|http)$")


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


class ActivityTransferRequest(BaseModel):
    target_ref: str = Field(min_length=2, max_length=256)
    max_users: int = Field(default=100, ge=1, le=1000)
    min_delay_seconds: int = Field(default=20, ge=0, le=3600)
    max_delay_seconds: int = Field(default=40, ge=0, le=7200)
    daily_limit: int = Field(default=50, ge=1, le=1000)

    @field_validator("target_ref")
    @classmethod
    def normalize_target_ref(cls, value: str) -> str:
        return value.strip()


class JobCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    session_id: int
    source_ref: str = Field(min_length=2, max_length=256)
    target_ref: str = Field(min_length=2, max_length=256)
    max_users: int = Field(default=25, ge=1, le=1000)
    min_delay_seconds: int = Field(default=20, ge=0, le=3600)
    max_delay_seconds: int = Field(default=40, ge=0, le=7200)
    daily_limit: int = Field(default=50, ge=1, le=1000)
    dry_run: bool = True
    scheduled_at: str | None = Field(default=None, max_length=40)
    working_start: str = Field(default="09:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    working_end: str = Field(default="22:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    @field_validator("target_ref")
    @classmethod
    def source_and_target_are_checked_later(cls, value: str) -> str:
        return value.strip()

    @field_validator("scheduled_at")
    @classmethod
    def normalize_schedule(cls, value: str | None) -> str | None:
        if not value:
            return None
        try:
            return normalize_datetime(value)
        except ValueError as error:
            raise ValueError("Planlanan başlangıç geçerli bir tarih ve saat olmalıdır.") from error


class CandidateSelectionRequest(BaseModel):
    candidate_ids: list[int] = Field(default_factory=list, max_length=1000)


class LicenseActivationRequest(BaseModel):
    license_key: str = Field(min_length=20, max_length=64)
