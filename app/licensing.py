import asyncio
import base64
import hashlib
import json
import platform
import secrets
import time
from datetime import UTC, datetime

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.config import APP_DIR, SOURCE_DIR, get_settings
from app.database import add_log, get_app_setting, set_app_setting
from app.license_key import LICENSE_PUBLIC_KEY_PEM

LICENSE_RUNTIME_BLOCK_STATUS = "license_runtime_block_status"
LICENSE_RUNTIME_BLOCK_MESSAGE = "license_runtime_block_message"


def _clear_runtime_block() -> None:
    set_app_setting(LICENSE_RUNTIME_BLOCK_STATUS, "")
    set_app_setting(LICENSE_RUNTIME_BLOCK_MESSAGE, "")


def _set_runtime_block(status: str, message: str) -> None:
    set_app_setting(LICENSE_RUNTIME_BLOCK_STATUS, status)
    set_app_setting(LICENSE_RUNTIME_BLOCK_MESSAGE, message)


def _server_error_message(payload: object, fallback: str) -> str:
    if not isinstance(payload, dict):
        return fallback
    detail = payload.get("detail")
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    return fallback


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


def _installation_id() -> str:
    path = APP_DIR / "data" / "installation_id"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(24)
    path.write_text(value, encoding="utf-8")
    return value


def _machine_material() -> str:
    if platform.system() == "Windows":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
            ) as key:
                return str(winreg.QueryValueEx(key, "MachineGuid")[0])
        except OSError:
            pass
    return f"{platform.node()}|{platform.machine()}|{platform.system()}"


def device_id() -> str:
    return hashlib.sha256(f"pawgram:v1:{_machine_material()}".encode()).hexdigest()


def app_version() -> str:
    for path in (APP_DIR / "VERSION", SOURCE_DIR / "VERSION"):
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return "0.1.0"


def _verify_lease(token: str) -> dict:
    encoded, signature = token.split(".", 1)
    public_key = serialization.load_pem_public_key(
        LICENSE_PUBLIC_KEY_PEM
    )
    if not isinstance(public_key, Ed25519PublicKey):
        raise TypeError("Lisans doğrulama anahtarı Ed25519 biçiminde değil.")
    try:
        public_key.verify(_decode(signature), encoded.encode("ascii"))
    except InvalidSignature as error:
        raise ValueError("Lisans imzası geçersiz.") from error
    claims = json.loads(_decode(encoded))
    if claims.get("product") != "pawgram":
        raise ValueError("Lisans farklı bir ürüne ait.")
    if claims.get("device_id") != device_id():
        raise ValueError("Lisans bu cihazla eşleşmiyor.")
    return claims


def local_license_status() -> dict:
    settings = get_settings()
    if not settings.licensing_enforced:
        return {
            "required": False,
            "valid": True,
            "status": "personal",
            "message": "Kişisel kullanım modu",
        }
    runtime_block = get_app_setting(LICENSE_RUNTIME_BLOCK_STATUS)
    if settings.licensing_online_required and runtime_block:
        return {
            "required": True,
            "valid": False,
            "status": runtime_block,
            "message": get_app_setting(LICENSE_RUNTIME_BLOCK_MESSAGE)
            or "Lisans çevrim içi olarak doğrulanamadı.",
        }
    token = get_app_setting("license_lease_token")
    if not token:
        return {
            "required": True,
            "valid": False,
            "status": "not_activated",
            "message": "Pawgram lisansı etkinleştirilmedi.",
        }
    try:
        claims = _verify_lease(token)
        now = int(time.time())
        unlimited = claims.get("unlimited") is True
        last_server_time = get_app_setting("license_last_server_time")
        if last_server_time:
            trusted_time = datetime.fromisoformat(last_server_time)
            if trusted_time.tzinfo is None:
                trusted_time = trusted_time.replace(tzinfo=UTC)
            if datetime.now(UTC).timestamp() < trusted_time.timestamp() - 300:
                return {
                    "required": True,
                    "valid": False,
                    "status": "clock_invalid",
                    "message": "Sistem tarihi güvenilir sunucu zamanından geride. Tarih ve saati düzeltin.",
                }
        if int(claims["exp"]) <= now:
            return {
                "required": True,
                "valid": False,
                "status": "lease_expired",
                "message": "Lisansın çevrimdışı kullanım süresi doldu. İnternet bağlantısı gerekli.",
            }
        if not unlimited and int(claims["license_exp"]) <= now:
            return {
                "required": True,
                "valid": False,
                "status": "expired",
                "message": "Lisans süresi dolmuş.",
            }
        return {
            "required": True,
            "valid": True,
            "status": "active",
            "message": "Lisans aktif (sınırsız)." if unlimited else "Lisans aktif.",
            "lease_token": token,
            "lease_expires_at": datetime.fromtimestamp(int(claims["exp"]), UTC).isoformat(),
            "license_expires_at": (
                None
                if unlimited
                else datetime.fromtimestamp(int(claims["license_exp"]), UTC).isoformat()
            ),
            "unlimited": unlimited,
            "customer_label": get_app_setting("license_customer_label") or "",
        }
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, OSError):
        return {
            "required": True,
            "valid": False,
            "status": "invalid",
            "message": "Yerel lisans belgesi doğrulanamadı.",
        }


def _safe_public_status(status: dict) -> dict:
    return {key: value for key, value in status.items() if key != "lease_token"}


def _server_url() -> str:
    url = get_settings().effective_license_server_url.rstrip("/")
    if not url.startswith("https://") and not url.startswith("http://127.0.0.1") and not url.startswith("http://localhost"):
        raise RuntimeError("Ticari lisans sunucusu HTTPS kullanmalıdır.")
    return url


async def activate_license(license_key: str) -> dict:
    settings = get_settings()
    if not settings.licensing_enforced:
        return _safe_public_status(local_license_status())
    payload = {
        "license_key": license_key.strip(),
        "device_id": device_id(),
        "installation_id": _installation_id(),
        "app_version": app_version(),
    }
    try:
        async with httpx.AsyncClient(timeout=settings.license_request_timeout) as client:
            response = await client.post(f"{_server_url()}/v1/activate", json=payload)
        data = response.json()
        if response.status_code >= 400:
            raise ValueError(_server_error_message(data, "Lisans etkinleştirilemedi."))
        if not isinstance(data, dict):
            raise ValueError(  # noqa: TRY004 - remote response value is invalid
                "Lisans sunucusu geçersiz yanıt verdi."
            )
        claims = _verify_lease(data["lease_token"])
        if int(claims["exp"]) <= int(time.time()):
            raise ValueError("Sunucu süresi dolmuş lisans belgesi gönderdi.")
        set_app_setting("license_lease_token", data["lease_token"])
        set_app_setting("license_customer_label", data.get("customer_label", ""))
        set_app_setting("license_last_server_time", data["server_time"])
        _clear_runtime_block()
        add_log("success", "license", "Pawgram lisansı bu cihazda etkinleştirildi")
        status = local_license_status()
        status["offline"] = False
        return _safe_public_status(status)
    except httpx.HTTPError as error:
        raise ValueError("Lisans sunucusuna ulaşılamadı. İnternet bağlantısını kontrol edin.") from error


async def refresh_license() -> dict:
    local = local_license_status()
    token = get_app_setting("license_lease_token")
    if not local["required"] or not token:
        return _safe_public_status(local)
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=settings.license_request_timeout) as client:
            response = await client.post(
                f"{_server_url()}/v1/validate",
                json={
                    "lease_token": token,
                    "device_id": device_id(),
                    "app_version": app_version(),
                },
            )
        data = response.json()
        if response.status_code >= 400:
            message = _server_error_message(data, "Lisans sunucu tarafından reddedildi.")
            set_app_setting("license_lease_token", "")
            _set_runtime_block("revoked", message)
            return {
                "required": True,
                "valid": False,
                "status": "revoked",
                "message": message,
                "offline": False,
            }
        if not isinstance(data, dict):
            raise ValueError(  # noqa: TRY004 - remote response value is invalid
                "Lisans sunucusu geçersiz yanıt verdi."
            )
        _verify_lease(data["lease_token"])
        set_app_setting("license_lease_token", data["lease_token"])
        set_app_setting("license_customer_label", data.get("customer_label", ""))
        set_app_setting("license_last_server_time", data["server_time"])
        _clear_runtime_block()
        refreshed = local_license_status()
        refreshed["offline"] = False
        return _safe_public_status(refreshed)
    except (httpx.HTTPError, RuntimeError, ValueError):
        if settings.licensing_online_required:
            message = (
                "Lisans sunucusuna bağlanılamadı. Pawgram'ı kullanmak için internet bağlantısı ve "
                "lisans sunucusu erişimi gereklidir."
            )
            _set_runtime_block("server_unreachable", message)
            return {
                "required": True,
                "valid": False,
                "status": "server_unreachable",
                "message": message,
                "offline": False,
            }
        fallback = local_license_status()
        fallback["offline"] = bool(fallback["valid"])
        if fallback["valid"]:
            fallback["message"] = "Sunucuya ulaşılamadı; imzalı çevrimdışı süre kullanılıyor."
        return _safe_public_status(fallback)


async def license_refresh_loop() -> None:
    while True:
        settings = get_settings()
        if settings.licensing_enforced:
            await refresh_license()
        await asyncio.sleep(settings.license_refresh_interval_seconds)
