import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse

from license_server.config import SERVER_DIR, get_license_server_settings
from license_server.database import add_audit, get_connection, initialize_database, utc_now
from license_server.schemas import AdminLoginRequest, ActivationRequest, LicenseCreateRequest, LicenseExtendRequest, ValidationRequest
from license_server.signing import sign_payload, verify_token


ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ACTIVATION_ATTEMPTS: dict[str, deque[float]] = defaultdict(deque)


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    settings = get_license_server_settings()
    if not settings.signing_key_path.exists() or not settings.public_key_path.exists():
        raise RuntimeError("Lisans imza anahtarları bulunamadı. Önce generate_keys.py çalıştırın.")
    yield


app = FastAPI(title="Pawgram License Server", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"
    return response


def _admin_session_token() -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + 43200, "nonce": secrets.token_hex(16)}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(
        get_license_server_settings().admin_api_key.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{signature}"


def _verify_admin_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    encoded, signature = token.rsplit(".", 1)
    expected = hmac.new(
        get_license_server_settings().admin_api_key.encode("utf-8"),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded + ("=" * (-len(encoded) % 4))))
        return int(payload["exp"]) > int(time.time())
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def require_admin(request: Request, x_admin_key: str = Header(default="")) -> None:
    expected = get_license_server_settings().admin_api_key
    header_valid = bool(expected and x_admin_key and secrets.compare_digest(x_admin_key, expected))
    cookie_valid = _verify_admin_session(request.cookies.get("pawgram_license_admin"))
    if not header_valid and not cookie_valid:
        raise HTTPException(status_code=401, detail="Yönetici anahtarı geçersiz.")


def normalize_code(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


def code_hash(value: str) -> str:
    return hashlib.sha256(normalize_code(value).encode("ascii")).hexdigest()


def create_code() -> str:
    return "PAWG-" + "-".join(
        "".join(secrets.choice(ALPHABET) for _ in range(5)) for _ in range(4)
    )


def rate_limit_activation(request: Request) -> None:
    address = request.client.host if request.client else "unknown"
    now = time.monotonic()
    attempts = ACTIVATION_ATTEMPTS[address]
    while attempts and attempts[0] < now - 60:
        attempts.popleft()
    if len(attempts) >= 10:
        raise HTTPException(status_code=429, detail="Çok fazla deneme yapıldı. Bir dakika bekleyin.")
    attempts.append(now)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def license_is_usable(record) -> None:
    if not record or record["status"] != "active":
        raise HTTPException(status_code=403, detail="Lisans geçersiz veya iptal edilmiş.")
    if not record["expires_at"]:
        raise HTTPException(status_code=403, detail="Lisans henüz başlatılmamış.")
    if parse_time(record["expires_at"]) <= datetime.now(UTC):
        raise HTTPException(status_code=403, detail="Lisans süresi dolmuş.")


def issue_lease(license_record, activation_id: int, device_id: str) -> tuple[str, str]:
    settings = get_license_server_settings()
    license_expiry = parse_time(license_record["expires_at"])
    lease_expiry = min(datetime.now(UTC) + timedelta(hours=settings.lease_hours), license_expiry)
    payload = {
        "v": 1,
        "product": "pawgram",
        "license_id": license_record["id"],
        "activation_id": activation_id,
        "device_id": device_id,
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int(lease_expiry.timestamp()),
        "license_exp": int(license_expiry.timestamp()),
        "nonce": secrets.token_hex(12),
    }
    return sign_payload(payload), lease_expiry.isoformat()


@app.get("/")
def admin_page():
    return FileResponse(SERVER_DIR / "admin.html")


@app.get("/health")
def health():
    return {"ok": True, "service": "pawgram-license-server"}


@app.post("/v1/admin/login")
def admin_login(payload: AdminLoginRequest, response: Response):
    settings = get_license_server_settings()
    if not secrets.compare_digest(payload.admin_key, settings.admin_api_key):
        raise HTTPException(status_code=401, detail="Yönetici anahtarı geçersiz.")
    response.set_cookie(
        "pawgram_license_admin",
        _admin_session_token(),
        httponly=True,
        samesite="strict",
        secure=settings.cookie_secure,
        max_age=43200,
    )
    return {"ok": True}


@app.post("/v1/admin/logout")
def admin_logout(response: Response):
    response.delete_cookie("pawgram_license_admin")
    return {"ok": True}


@app.post("/v1/admin/licenses", dependencies=[Depends(require_admin)])
def create_license(payload: LicenseCreateRequest):
    now = datetime.now(UTC)
    for _ in range(10):
        code = create_code()
        try:
            with get_connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO licenses(
                        code_hash, code_hint, customer_label, status, duration_days,
                        starts_at, expires_at, max_devices, created_at, updated_at
                    ) VALUES (?, ?, ?, 'active', ?, NULL, NULL, ?, ?, ?)
                    """,
                    (
                        code_hash(code), code[-5:], payload.customer_label.strip(), payload.duration_days,
                        payload.max_devices, now.isoformat(), now.isoformat(),
                    ),
                )
                license_id = cursor.lastrowid
            add_audit("license_created", license_id, None, f"{payload.duration_days} days")
            return {
                "id": license_id,
                "license_key": code,
                "customer_label": payload.customer_label,
                "duration_days": payload.duration_days,
                "expires_at": None,
                "max_devices": payload.max_devices,
                "warning": "Bu lisans kodu yalnızca şimdi açık biçimde gösterilir.",
            }
        except Exception as error:
            if "UNIQUE constraint" not in str(error):
                raise
    raise HTTPException(status_code=500, detail="Benzersiz lisans kodu üretilemedi.")


@app.get("/v1/admin/licenses", dependencies=[Depends(require_admin)])
def list_licenses():
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT l.*, COUNT(CASE WHEN a.status='active' THEN 1 END) activation_count
            FROM licenses l
            LEFT JOIN activations a ON a.license_id=l.id
            GROUP BY l.id
            ORDER BY l.id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/v1/admin/licenses/{license_id}/revoke", dependencies=[Depends(require_admin)])
def revoke_license(license_id: int):
    with get_connection() as connection:
        cursor = connection.execute(
            "UPDATE licenses SET status='revoked', updated_at=? WHERE id=?",
            (utc_now(), license_id),
        )
    if not cursor.rowcount:
        raise HTTPException(status_code=404, detail="Lisans bulunamadı.")
    add_audit("license_revoked", license_id, None)
    return {"ok": True}


@app.post("/v1/admin/licenses/{license_id}/extend", dependencies=[Depends(require_admin)])
def extend_license(license_id: int, payload: LicenseExtendRequest):
    with get_connection() as connection:
        record = connection.execute("SELECT * FROM licenses WHERE id=?", (license_id,)).fetchone()
        if not record:
            raise HTTPException(status_code=404, detail="Lisans bulunamadı.")
        if record["expires_at"]:
            current_expiry = parse_time(record["expires_at"])
            base = max(current_expiry, datetime.now(UTC))
            new_expiry = base + timedelta(days=payload.duration_days)
            connection.execute(
                "UPDATE licenses SET expires_at=?, status='active', updated_at=? WHERE id=?",
                (new_expiry.isoformat(), utc_now(), license_id),
            )
        else:
            new_expiry = None
            connection.execute(
                "UPDATE licenses SET duration_days=duration_days+?, status='active', updated_at=? WHERE id=?",
                (payload.duration_days, utc_now(), license_id),
            )
    add_audit("license_extended", license_id, None, f"{payload.duration_days} days")
    return {"ok": True, "expires_at": new_expiry.isoformat() if new_expiry else None}


@app.post("/v1/admin/licenses/{license_id}/reset-devices", dependencies=[Depends(require_admin)])
def reset_devices(license_id: int):
    with get_connection() as connection:
        exists = connection.execute("SELECT id FROM licenses WHERE id=?", (license_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Lisans bulunamadı.")
        connection.execute(
            "UPDATE activations SET status='reset' WHERE license_id=? AND status='active'",
            (license_id,),
        )
    add_audit("devices_reset", license_id, None)
    return {"ok": True}


@app.post("/v1/activate")
def activate(payload: ActivationRequest, request: Request):
    rate_limit_activation(request)
    normalized_hash = code_hash(payload.license_key)
    with get_connection() as connection:
        license_record = connection.execute(
            "SELECT * FROM licenses WHERE code_hash=?", (normalized_hash,)
        ).fetchone()
        if license_record and license_record["status"] == "active" and not license_record["expires_at"]:
            starts_at = datetime.now(UTC)
            expires_at = starts_at + timedelta(days=license_record["duration_days"])
            connection.execute(
                "UPDATE licenses SET starts_at=?, expires_at=?, updated_at=? WHERE id=?",
                (starts_at.isoformat(), expires_at.isoformat(), utc_now(), license_record["id"]),
            )
            license_record = connection.execute(
                "SELECT * FROM licenses WHERE id=?", (license_record["id"],)
            ).fetchone()
        license_is_usable(license_record)
        activation = connection.execute(
            "SELECT * FROM activations WHERE license_id=? AND device_id=?",
            (license_record["id"], payload.device_id),
        ).fetchone()
        if not activation or activation["status"] != "active":
            active_count = connection.execute(
                "SELECT COUNT(*) count FROM activations WHERE license_id=? AND status='active'",
                (license_record["id"],),
            ).fetchone()["count"]
            if active_count >= license_record["max_devices"]:
                raise HTTPException(status_code=409, detail="Bu lisansın cihaz sınırına ulaşıldı.")
            if activation:
                activation_id = activation["id"]
                connection.execute(
                    """
                    UPDATE activations
                    SET installation_id=?, app_version=?, status='active', activated_at=?, last_seen_at=?
                    WHERE id=?
                    """,
                    (payload.installation_id, payload.app_version, utc_now(), utc_now(), activation_id),
                )
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO activations(
                        license_id, device_id, installation_id, app_version, status,
                        activated_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        license_record["id"], payload.device_id, payload.installation_id,
                        payload.app_version, utc_now(), utc_now(),
                    ),
                )
                activation_id = cursor.lastrowid
        else:
            activation_id = activation["id"]
            connection.execute(
                "UPDATE activations SET installation_id=?, app_version=?, last_seen_at=? WHERE id=?",
                (payload.installation_id, payload.app_version, utc_now(), activation_id),
            )
    token, lease_expiry = issue_lease(license_record, activation_id, payload.device_id)
    add_audit("activated", license_record["id"], payload.device_id)
    return {
        "valid": True,
        "lease_token": token,
        "lease_expires_at": lease_expiry,
        "license_expires_at": license_record["expires_at"],
        "customer_label": license_record["customer_label"],
        "server_time": utc_now(),
    }


@app.post("/v1/validate")
def validate(payload: ValidationRequest):
    try:
        claims = verify_token(payload.lease_token)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=403, detail="Lisans belgesi doğrulanamadı.")
    if claims.get("product") != "pawgram" or claims.get("device_id") != payload.device_id:
        raise HTTPException(status_code=403, detail="Lisans bu cihazla eşleşmiyor.")
    with get_connection() as connection:
        license_record = connection.execute(
            "SELECT * FROM licenses WHERE id=?", (claims.get("license_id"),)
        ).fetchone()
        license_is_usable(license_record)
        activation = connection.execute(
            "SELECT * FROM activations WHERE id=? AND license_id=? AND device_id=? AND status='active'",
            (claims.get("activation_id"), license_record["id"], payload.device_id),
        ).fetchone()
        if not activation:
            raise HTTPException(status_code=403, detail="Cihaz aktivasyonu iptal edilmiş.")
        connection.execute(
            "UPDATE activations SET app_version=?, last_seen_at=? WHERE id=?",
            (payload.app_version, utc_now(), activation["id"]),
        )
    token, lease_expiry = issue_lease(license_record, activation["id"], payload.device_id)
    return {
        "valid": True,
        "lease_token": token,
        "lease_expires_at": lease_expiry,
        "license_expires_at": license_record["expires_at"],
        "customer_label": license_record["customer_label"],
        "server_time": utc_now(),
    }
