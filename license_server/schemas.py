from pydantic import BaseModel, Field


class AdminLoginRequest(BaseModel):
    admin_key: str = Field(min_length=20, max_length=256)


class LicenseCreateRequest(BaseModel):
    customer_label: str = Field(min_length=2, max_length=160)
    duration_days: int = Field(ge=1, le=3650)
    max_devices: int = Field(default=1, ge=1, le=20)


class LicenseExtendRequest(BaseModel):
    duration_days: int = Field(ge=1, le=3650)


class ActivationRequest(BaseModel):
    license_key: str = Field(min_length=20, max_length=64)
    device_id: str = Field(min_length=32, max_length=128)
    installation_id: str = Field(min_length=16, max_length=128)
    app_version: str = Field(default="unknown", max_length=40)


class ValidationRequest(BaseModel):
    lease_token: str = Field(min_length=40, max_length=4096)
    device_id: str = Field(min_length=32, max_length=128)
    app_version: str = Field(default="unknown", max_length=40)
