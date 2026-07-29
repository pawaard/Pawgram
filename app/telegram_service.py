import asyncio
import json
import re
import secrets
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter

from python_socks import ProxyType
from python_socks.async_.asyncio import Proxy
from telethon import TelegramClient, utils
from telethon.errors import (
    ChatAdminRequiredError,
    FloodWaitError,
    InviteRequestSentError,
    PeerFloodError,
    SessionPasswordNeededError,
    UserAlreadyParticipantError,
    UserChannelsTooMuchError,
    UserNotParticipantError,
    UserPrivacyRestrictedError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.channels import (
    GetParticipantRequest,
    InviteToChannelRequest,
    JoinChannelRequest,
)
from telethon.tl.functions.messages import (
    AddChatUserRequest,
    CheckChatInviteRequest,
    GetFullChatRequest,
    ImportChatInviteRequest,
)
from telethon.tl.functions.users import GetUsersRequest
from telethon.tl.types import (
    Channel,
    ChannelParticipantsAdmins,
    Chat,
    ChatParticipantAdmin,
    ChatParticipantCreator,
    InputUser,
    InputUserFromMessage,
    User,
)

from app.config import RESOURCE_DIR, get_settings
from app.database import (
    add_log,
    add_notification,
    get_app_setting,
    get_connection,
    set_app_setting,
    utc_now,
)
from app.scheduling import next_job_run, next_working_time
from app.security import decrypt, encrypt, mask_phone, phone_key
from app.session_operation import (
    SessionOperationBusy,
    SessionOperationLease,
    acquire_session_operation,
)

DEFAULT_DAILY_ACTIVITY_QUOTA = 30
PENDING_AUTH_TTL_MINUTES = 15
DEFAULT_LOGIN_PROXY_SETTING = "default_login_proxy_encrypted"
DEFAULT_LOGIN_PROXY_REVISION_SETTING = "default_login_proxy_revision"
CUSTOMER_PROXY_BUNDLE_FILENAME = "customer-proxy.json"


class GroupJoinPending(RuntimeError):
    def __init__(self, session_id: int, group_title: str):
        self.session_id = session_id
        self.group_title = group_title
        super().__init__(
            f"{group_title} için katılım isteği gönderildi. Telegram'dan onaylandıktan sonra tarama otomatik devam edecek."
        )


class SessionBudgetWaiting(RuntimeError):
    def __init__(self, wait_until: datetime):
        self.wait_until = wait_until
        super().__init__(
            "Tüm aktif hesaplar Pawgram'ın güvenli günlük işlem eşiğine ulaştı. İşlem sonraki güvenli pencerede otomatik devam edecek."
        )


class ProxyUnavailableError(RuntimeError):
    pass


class TargetGroupUnavailableError(RuntimeError):
    """Raised when one invite session cannot use the requested target group."""

    def __init__(self, session_id: int, message: str):
        self.session_id = session_id
        super().__init__(message)


def _entity_can_invite_users(entity) -> bool:
    """Return the effective Telegram invite permission for the current session."""
    if getattr(entity, "creator", False):
        return True

    admin_rights = getattr(entity, "admin_rights", None)
    if admin_rights and getattr(admin_rights, "invite_users", False):
        return True

    if any(
        bool(getattr(entity, attribute, False))
        for attribute in ("left", "kicked", "deactivated")
    ):
        return False

    is_basic_group = isinstance(entity, Chat)
    is_megagroup = bool(getattr(entity, "megagroup", False))
    if not (is_basic_group or is_megagroup):
        return False

    personal_restrictions = getattr(entity, "banned_rights", None)
    if personal_restrictions and getattr(personal_restrictions, "invite_users", False):
        return False

    default_restrictions = getattr(entity, "default_banned_rights", None)
    if default_restrictions is not None:
        return not bool(getattr(default_restrictions, "invite_users", False))

    # Legacy basic groups allow members to add users unless Telegram reports a ban.
    return is_basic_group


def _load_default_login_proxy() -> dict | None:
    encrypted = get_app_setting(DEFAULT_LOGIN_PROXY_SETTING)
    if encrypted:
        try:
            value = json.loads(decrypt(encrypted))
        except (TypeError, ValueError, json.JSONDecodeError):
            value = None
        required = {"proxy_type", "host", "port"}
        if isinstance(value, dict) and required.issubset(value):
            return value

    settings = get_settings()
    if not settings.default_proxy_host or not settings.default_proxy_port:
        return None
    config = _proxy_config_from_values(
        settings.default_proxy_type,
        settings.default_proxy_host,
        settings.default_proxy_port,
        settings.default_proxy_username,
        settings.default_proxy_password,
    )
    _save_default_login_proxy(config)
    return {
        "proxy_type": config["proxy_type"],
        "host": config["addr"],
        "port": config["port"],
        "username": config.get("username"),
        "password": config.get("password"),
    }


def default_login_proxy_public() -> dict:
    proxy = _load_default_login_proxy()
    if not proxy:
        return {"configured": False}
    return {
        "configured": True,
        "proxy_type": proxy["proxy_type"],
        "host": proxy["host"],
        "port": proxy["port"],
        "username": proxy.get("username") or "",
        "password_configured": bool(proxy.get("password")),
    }


def _save_default_login_proxy(config: dict) -> None:
    value = {
        "proxy_type": config["proxy_type"],
        "host": config["addr"],
        "port": int(config["port"]),
        "username": config.get("username") or None,
        "password": config.get("password") or None,
    }
    set_app_setting(DEFAULT_LOGIN_PROXY_SETTING, encrypt(json.dumps(value)))


def _load_bundled_customer_proxy() -> dict | None:
    bundle_path = RESOURCE_DIR / CUSTOMER_PROXY_BUNDLE_FILENAME
    if not bundle_path.is_file():
        return None
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Paketlenmiş müşteri proxy yapılandırması okunamadı.") from error
    required = {"revision", "proxy_type", "host", "port"}
    if not isinstance(bundle, dict) or not required.issubset(bundle):
        raise RuntimeError("Paketlenmiş müşteri proxy yapılandırması eksik veya bozuk.")
    return bundle


def sync_customer_release_proxy() -> int:
    """Apply a private package proxy revision without replacing Telegram session data."""
    settings = get_settings()
    bundle = _load_bundled_customer_proxy()
    if bundle:
        revision = str(bundle["revision"]).strip()
        proxy_type = str(bundle["proxy_type"])
        proxy_host = str(bundle["host"])
        proxy_port = int(bundle["port"])
        proxy_username = bundle.get("username")
        proxy_password = bundle.get("password")
    else:
        revision = (settings.default_proxy_revision or "").strip()
        if not settings.customer_release or not revision:
            return 0
        proxy_type = settings.default_proxy_type
        proxy_host = settings.default_proxy_host or ""
        proxy_port = int(settings.default_proxy_port or 0)
        proxy_username = settings.default_proxy_username
        proxy_password = settings.default_proxy_password
    if get_app_setting(DEFAULT_LOGIN_PROXY_REVISION_SETTING) == revision:
        return 0
    if not revision or not proxy_host or not proxy_port:
        raise RuntimeError("Müşteri proxy revizyonunda host ve port zorunludur.")

    config = _proxy_config_from_values(
        proxy_type,
        proxy_host,
        proxy_port,
        proxy_username,
        proxy_password,
    )
    _save_default_login_proxy(config)
    now = utc_now()
    with get_connection() as connection:
        updated = connection.execute(
            """
            UPDATE telegram_sessions
            SET proxy_enabled=1, proxy_type=?, proxy_host=?, proxy_port=?,
                proxy_username_encrypted=?, proxy_password_encrypted=?,
                proxy_last_status=NULL, proxy_latency_ms=NULL,
                proxy_last_error=NULL, proxy_last_test_at=NULL,
                status=CASE
                    WHEN status='flood_wait' AND flood_wait_until IS NOT NULL
                         AND flood_wait_until>? THEN 'flood_wait'
                    WHEN status='batch_wait' AND batch_cooldown_until IS NOT NULL
                         AND batch_cooldown_until>? THEN 'batch_wait'
                    ELSE 'proxy_pending'
                END,
                last_error=CASE
                    WHEN status='flood_wait' AND flood_wait_until IS NOT NULL
                         AND flood_wait_until>? THEN last_error
                    WHEN status='batch_wait' AND batch_cooldown_until IS NOT NULL
                         AND batch_cooldown_until>? THEN last_error
                    ELSE NULL
                END,
                updated_at=?
            WHERE session_encrypted IS NOT NULL
            """,
            (
                config["proxy_type"],
                config["addr"],
                config["port"],
                encrypt(config["username"]) if config.get("username") else None,
                encrypt(config["password"]) if config.get("password") else None,
                now,
                now,
                now,
                now,
                now,
            ),
        ).rowcount
    set_app_setting(DEFAULT_LOGIN_PROXY_REVISION_SETTING, revision)
    add_log(
        "success",
        "proxy",
        f"Müşteri paketindeki proxy revizyonu uygulandı; {updated} session yeniden doğrulama bekliyor.",
    )
    return updated


def save_default_login_proxy(
    proxy_type: str,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
) -> dict:
    current = _load_default_login_proxy()
    if password is None and current:
        password = current.get("password")
    config = _proxy_config_from_values(
        proxy_type,
        host,
        port,
        username,
        password,
    )
    _save_default_login_proxy(config)
    add_log("success", "proxy", "Pawgram varsayılan proxy ayarı şifreli olarak kaydedildi")
    return default_login_proxy_public()


async def test_default_login_proxy() -> dict:
    proxy = _load_default_login_proxy()
    if not proxy:
        raise ProxyUnavailableError("Önce Pawgram varsayılan proxy ayarını kaydedin.")
    config = _proxy_config_from_values(
        proxy["proxy_type"],
        proxy["host"],
        int(proxy["port"]),
        proxy.get("username"),
        proxy.get("password"),
    )
    api_id, api_hash = _credentials()
    client = None
    try:
        client, detected_type, latency_ms = await _connect_telegram_through_proxy(
            api_id,
            api_hash,
            config,
        )
    finally:
        if client is not None:
            await client.disconnect()
    config["proxy_type"] = detected_type
    _save_default_login_proxy(config)
    add_log(
        "success",
        "proxy",
        f"Pawgram varsayılan proxy Telegram bağlantısı doğrulandı ({detected_type.upper()}, {latency_ms} ms)",
    )
    return {
        "ok": True,
        "proxy_type": detected_type,
        "latency_ms": latency_ms,
        "fail_closed": True,
    }


def _proxy_error_detail(error: Exception) -> str:
    detail = str(error).strip() or error.__class__.__name__
    lowered = detail.lower()
    if "0 bytes read" in lowered or "incompleteread" in lowered:
        return (
            "Proxy tüneli açıldı ancak Telegram MTProto cevabı proxy tarafından kesildi. "
            "Bu proxy paketi Telegram trafiğini desteklemiyor veya sağlayıcı Telegram'ı engelliyor."
        )
    if "407" in lowered or "authentication" in lowered or "auth" in lowered:
        return "Proxy kimlik doğrulaması reddedildi; kullanıcı adı ve parolayı kontrol edin."
    if "timed out" in lowered or "timeout" in lowered:
        return "Proxy Telegram bağlantısına zamanında yanıt vermedi."
    return detail


async def _probe_proxy_socket(config: dict) -> int:
    proxy_types = {"socks5": ProxyType.SOCKS5, "http": ProxyType.HTTP}
    proxy_type = config["proxy_type"]
    started = perf_counter()
    socket = None
    try:
        proxy = Proxy.create(
            proxy_types[proxy_type],
            config["addr"],
            config["port"],
            username=config.get("username"),
            password=config.get("password"),
            rdns=True,
        )
        socket = await asyncio.wait_for(
            proxy.connect(
                dest_host="149.154.167.51",
                dest_port=443,
                timeout=6,
            ),
            timeout=7,
        )
        return max(1, round((perf_counter() - started) * 1000))
    finally:
        if socket is not None:
            socket.close()


async def _connect_telegram_through_proxy(
    api_id: int,
    api_hash: str,
    config: dict,
    *,
    session_string: str | None = None,
    require_authorized: bool = False,
) -> tuple[TelegramClient, str, int]:
    selected_type = config["proxy_type"]
    candidate_types = [selected_type] + [
        item for item in ("socks5", "http") if item != selected_type
    ]
    errors: list[str] = []
    for proxy_type in candidate_types:
        candidate = dict(config)
        candidate["proxy_type"] = proxy_type
        client = TelegramClient(
            StringSession(session_string or ""),
            api_id,
            api_hash,
            proxy=candidate,
            timeout=8,
            connection_retries=0,
        )
        started = perf_counter()
        try:
            await _probe_proxy_socket(candidate)
            await asyncio.wait_for(client.connect(), timeout=10)
            if require_authorized and not await asyncio.wait_for(
                client.is_user_authorized(), timeout=12
            ):
                raise RuntimeError("Telegram session yetkisi geçersiz.")
            latency_ms = max(1, round((perf_counter() - started) * 1000))
            return client, proxy_type, latency_ms
        except Exception as error:  # noqa: BLE001 - Telethon/proxy backends vary
            errors.append(f"{proxy_type.upper()}: {_proxy_error_detail(error)}")
            await client.disconnect()
    raise ProxyUnavailableError(" | ".join(errors))


@dataclass
class ResolvedGroup:
    id: int
    title: str
    username: str | None
    kind: str
    participants_count: int | None
    creator: bool
    admin_rights: bool
    can_invite_users: bool
    source_suitable: bool
    target_suitable: bool


def _credentials() -> tuple[int, str]:
    settings = get_settings()
    if settings.telegram_configured:
        return settings.telegram_api_id, settings.telegram_api_hash  # type: ignore[return-value]
    stored_id = get_app_setting("telegram_api_id")
    stored_hash = get_app_setting("telegram_api_hash_encrypted")
    if not stored_id or not stored_hash:
        raise RuntimeError("Telegram API bilgileri Ayarlar ekranından yapılandırılmamış.")
    try:
        return int(stored_id), decrypt(stored_hash)
    except (ValueError, TypeError) as error:
        raise RuntimeError("Kayıtlı Telegram API yapılandırması geçersiz.") from error


async def start_login(
    phone: str,
    label: str,
    proxy_type: str,
    proxy_host: str | None,
    proxy_port: int | None,
    proxy_username: str | None,
    proxy_password: str | None,
    *,
    use_proxy: bool = True,
) -> dict:
    api_id, api_hash = _credentials()
    login_label = mask_phone(phone)
    proxy_config: dict | None = None
    detected_type: str | None = None
    latency_ms: int | None = None
    if use_proxy:
        if not proxy_host or not proxy_port:
            raise RuntimeError("Proxy kullanmak için host ve port bilgilerini girin.")
        saved_proxy = _load_default_login_proxy()
        if (
            not proxy_password
            and saved_proxy
            and proxy_type == saved_proxy.get("proxy_type")
            and proxy_host == saved_proxy.get("host")
            and int(proxy_port) == int(saved_proxy.get("port") or 0)
            and (proxy_username or None) == (saved_proxy.get("username") or None)
        ):
            proxy_password = saved_proxy.get("password")
        proxy_config = _proxy_config_from_values(
            proxy_type,
            proxy_host,
            proxy_port,
            proxy_username,
            proxy_password,
        )
        add_log(
            "info",
            "login_proxy",
            f"{login_label}: gerçek Telegram proxy ön testi başladı ({proxy_type.upper()} {proxy_host}:{proxy_port})",
        )
        try:
            client, detected_type, latency_ms = await _connect_telegram_through_proxy(
                api_id,
                api_hash,
                proxy_config,
            )
        except Exception as error:
            add_log(
                "error",
                "login_proxy",
                f"{login_label}: Telegram proxy ön testi başarısız — {error}",
            )
            raise ProxyUnavailableError(
                "Proxy üzerinden gerçek Telegram bağlantısı kurulamadı; doğrulama başlatılmadı ve ana IP kullanılmadı. "
                "Proxy türünü, kullanıcı/parolayı ve sağlayıcının Telegram erişimine izin verdiğini kontrol edin. "
                f"Ayrıntı: {error}"
            ) from error
        proxy_config["proxy_type"] = detected_type
        add_log(
            "success",
            "login_proxy",
            f"{login_label}: Telegram proxy bağlantısı kuruldu ({detected_type.upper()}, {latency_ms} ms)",
        )
    else:
        add_log("info", "login_direct", f"{login_label}: doğrudan Telegram bağlantısı başlatıldı")
        client = TelegramClient(
            StringSession(),
            api_id,
            api_hash,
            timeout=12,
            connection_retries=0,
        )
        try:
            await asyncio.wait_for(client.connect(), timeout=20)
        except Exception as error:
            await client.disconnect()
            add_log("error", "login_direct", f"{login_label}: doğrudan bağlantı kurulamadı — {error}")
            raise RuntimeError(
                f"Telegram'a doğrudan bağlantı kurulamadı: {str(error).strip() or error.__class__.__name__}"
            ) from error
    try:
        add_log("info", "login_auth", f"{login_label}: doğrulama kodu isteği Telegram'a gönderiliyor")
        result = await asyncio.wait_for(client.send_code_request(phone), timeout=25)
        session_string = client.session.save()
        if proxy_config:
            _save_default_login_proxy(proxy_config)
        now = utc_now()
        with get_connection() as connection:
            stale_before = (
                datetime.now(UTC) - timedelta(minutes=PENDING_AUTH_TTL_MINUTES)
            ).isoformat()
            connection.execute("DELETE FROM pending_auth WHERE created_at < ?", (stale_before,))
            connection.execute(
                """
                INSERT INTO pending_auth(
                    phone_hash, phone_encrypted, label, session_encrypted,
                    code_hash_encrypted, direct_connection_allowed,
                    proxy_type, proxy_host, proxy_port,
                    proxy_username_encrypted, proxy_password_encrypted,
                    proxy_latency_ms, proxy_tested_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(phone_hash) DO UPDATE SET
                    phone_encrypted=excluded.phone_encrypted,
                    label=excluded.label,
                    session_encrypted=excluded.session_encrypted,
                    code_hash_encrypted=excluded.code_hash_encrypted,
                    direct_connection_allowed=excluded.direct_connection_allowed,
                    proxy_type=excluded.proxy_type,
                    proxy_host=excluded.proxy_host,
                    proxy_port=excluded.proxy_port,
                    proxy_username_encrypted=excluded.proxy_username_encrypted,
                    proxy_password_encrypted=excluded.proxy_password_encrypted,
                    proxy_latency_ms=excluded.proxy_latency_ms,
                    proxy_tested_at=excluded.proxy_tested_at,
                    created_at=excluded.created_at
                """,
                (
                    phone_key(phone),
                    encrypt(phone),
                    label,
                    encrypt(session_string),
                    encrypt(result.phone_code_hash),
                    int(not use_proxy),
                    detected_type,
                    proxy_host,
                    proxy_port,
                    encrypt(proxy_username) if proxy_username else None,
                    encrypt(proxy_password) if proxy_password else None,
                    latency_ms,
                    now,
                    now,
                ),
            )
        add_log(
            "success",
            "login_auth",
            f"{login_label}: doğrulama kodu istendi ve geçici oturum şifreli kaydedildi",
        )
        add_log("info", "session", f"{mask_phone(phone)} için doğrulama kodu istendi")
        return {
            "ok": True,
            "phone_masked": mask_phone(phone),
            "message": "Doğrulama kodu proxy üzerinden gönderildi." if use_proxy else "Doğrulama kodu doğrudan bağlantıyla gönderildi.",
            "used_proxy": use_proxy,
            "proxy_type": detected_type,
            "proxy_latency_ms": latency_ms,
        }
    except FloodWaitError as error:
        raise RuntimeError(f"Telegram {error.seconds} saniye bekleme istedi.") from error
    except (TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError) as error:
        if use_proxy:
            raise ProxyUnavailableError(
                "Proxy bağlantısı kod isteme sırasında Telegram tarafından kesildi; ana IP kullanılmadı. "
                f"Telegram uyumlu başka bir residential proxy deneyin. Ayrıntı: {_proxy_error_detail(error)}"
            ) from error
        raise RuntimeError(
            f"Doğrudan Telegram bağlantısı kod isteme sırasında kesildi: {_proxy_error_detail(error)}"
        ) from error
    finally:
        await client.disconnect()


async def verify_login(phone: str, code: str, password: str | None) -> dict:
    api_id, api_hash = _credentials()
    login_label = mask_phone(phone)
    with get_connection() as connection:
        pending = connection.execute(
            "SELECT * FROM pending_auth WHERE phone_hash = ?", (phone_key(phone),)
        ).fetchone()
    if not pending:
        raise RuntimeError("Bekleyen doğrulama isteği bulunamadı. Yeniden kod isteyin.")
    add_log("info", "login_auth", f"{login_label}: bekleyen şifreli doğrulama oturumu yüklendi")

    try:
        created_at = datetime.fromisoformat(pending["created_at"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
    except (TypeError, ValueError) as error:
        raise RuntimeError("Bekleyen doğrulama isteği bozuk. Yeniden kod isteyin.") from error
    if created_at < datetime.now(UTC) - timedelta(minutes=PENDING_AUTH_TTL_MINUTES):
        with get_connection() as connection:
            connection.execute("DELETE FROM pending_auth WHERE phone_hash = ?", (phone_key(phone),))
        raise RuntimeError("Doğrulama isteğinin 15 dakikalık süresi doldu. Yeniden kod isteyin.")

    direct_connection_allowed = bool(pending.get("direct_connection_allowed"))
    if not direct_connection_allowed and (
        not pending.get("proxy_type")
        or not pending.get("proxy_host")
        or not pending.get("proxy_port")
    ):
        with get_connection() as connection:
            connection.execute("DELETE FROM pending_auth WHERE phone_hash = ?", (phone_key(phone),))
        raise RuntimeError(
            "Bu doğrulama isteği proxy zorunluluğundan önce oluşturulmuş. Ana IP kullanılmadı; yeniden kod isteyin."
        )
    pending_proxy = None
    if not direct_connection_allowed:
        pending_proxy = _proxy_config_from_values(
            pending["proxy_type"],
            pending["proxy_host"],
            int(pending["proxy_port"]),
            decrypt(pending["proxy_username_encrypted"])
            if pending.get("proxy_username_encrypted")
            else None,
            decrypt(pending["proxy_password_encrypted"])
            if pending.get("proxy_password_encrypted")
            else None,
        )
    client = TelegramClient(
        StringSession(decrypt(pending["session_encrypted"])),
        api_id,
        api_hash,
        proxy=pending_proxy,
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=20)
        add_log(
            "success",
            "login_auth",
            f"{login_label}: doğrulama bağlantısı {'doğrudan' if direct_connection_allowed else 'proxy üzerinden'} kuruldu",
        )
        try:
            await asyncio.wait_for(
                client.sign_in(
                    phone=phone,
                    code=code,
                    phone_code_hash=decrypt(pending["code_hash_encrypted"]),
                ),
                timeout=25,
            )
        except SessionPasswordNeededError:
            if not password:
                return {"ok": False, "password_required": True, "message": "İki aşamalı doğrulama parolası gerekli."}
            await asyncio.wait_for(client.sign_in(password=password), timeout=25)

        add_log("success", "login_auth", f"{login_label}: Telegram doğrulaması başarılı")

        me = await asyncio.wait_for(client.get_me(), timeout=15)
        session_string = client.session.save()
        display_name = " ".join(part for part in [me.first_name, me.last_name] if part).strip() or "Telegram hesabı"
        now = utc_now()
        proxy_enabled = int(not direct_connection_allowed)
        session_status = "active" if proxy_enabled else "proxy_error"
        proxy_last_status = "success" if proxy_enabled else None
        direct_error = None if proxy_enabled else (
            "Hesap proxy olmadan eklendi. Tarama veya üye ekleme işleminden önce Ayarlar'dan sabit proxy atayın."
        )
        with get_connection() as connection:
            existing = connection.execute(
                "SELECT id FROM telegram_sessions WHERE telegram_user_id=? ORDER BY id LIMIT 1",
                (me.id,),
            ).fetchone()
            if existing:
                session_id = existing["id"]
                connection.execute(
                    """
                    UPDATE telegram_sessions
                    SET label=?, phone_masked=?, phone_encrypted=?, session_encrypted=?,
                        display_name=?, username=?, proxy_enabled=?, proxy_type=?, proxy_host=?,
                        proxy_port=?, proxy_username_encrypted=?, proxy_password_encrypted=?,
                        proxy_last_status=?, proxy_latency_ms=?, proxy_last_test_at=?,
                        proxy_last_error=NULL,
                        status=CASE WHEN status IN ('flood_wait', 'batch_wait') THEN status ELSE ? END,
                        last_error=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        pending["label"], mask_phone(phone), encrypt(phone), encrypt(session_string),
                        display_name, me.username, proxy_enabled, pending["proxy_type"],
                        pending["proxy_host"], pending["proxy_port"],
                        pending.get("proxy_username_encrypted"),
                        pending.get("proxy_password_encrypted"), proxy_last_status,
                        pending.get("proxy_latency_ms"), pending.get("proxy_tested_at") or now,
                        session_status, direct_error, now, session_id,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO telegram_sessions(
                        label, phone_masked, phone_encrypted, session_encrypted, telegram_user_id,
                        display_name, username, status, proxy_enabled, proxy_type, proxy_host,
                        proxy_port, proxy_username_encrypted, proxy_password_encrypted,
                        proxy_last_status, proxy_latency_ms, proxy_last_test_at,
                        last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pending["label"], mask_phone(phone), encrypt(phone), encrypt(session_string),
                        me.id, display_name, me.username, session_status, proxy_enabled,
                        pending["proxy_type"], pending["proxy_host"],
                        pending["proxy_port"], pending.get("proxy_username_encrypted"),
                        pending.get("proxy_password_encrypted"), proxy_last_status,
                        pending.get("proxy_latency_ms"), pending.get("proxy_tested_at") or now,
                        direct_error, now, now,
                    ),
                )
                session_id = cursor.lastrowid
            connection.execute("DELETE FROM pending_auth WHERE phone_hash = ?", (phone_key(phone),))
        add_log("success", "login_auth", f"{login_label}: geçici doğrulama durumu tamamen temizlendi")
        add_log("success", "session", f"{mask_phone(phone)} başarıyla bağlandı", session_id=session_id)
        add_notification("success", "Telegram hesabı bağlandı", f"{display_name} kullanıma hazır.", "sessions")
        return {"ok": True, "session_id": session_id, "display_name": display_name}
    finally:
        await client.disconnect()


def cancel_pending_login(phone: str) -> dict:
    key = phone_key(phone)
    with get_connection() as connection:
        deleted = connection.execute(
            "DELETE FROM pending_auth WHERE phone_hash = ?",
            (key,),
        ).rowcount
    if deleted:
        add_log("info", "session", f"{mask_phone(phone)} için bekleyen doğrulama isteği temizlendi")
    return {"ok": True, "deleted": bool(deleted)}


def get_session_record(session_id: int) -> dict:
    with get_connection() as connection:
        session = connection.execute(
            "SELECT * FROM telegram_sessions WHERE id = ?", (session_id,)
        ).fetchone()
    if not session:
        raise RuntimeError("Telegram hesabı bulunamadı.")
    if not session["session_encrypted"]:
        raise RuntimeError("Telegram session verisi bulunamadı.")
    return session


async def _client_for(
    session_id: int,
    *,
    mutate_session_state: bool = True,
) -> TelegramClient:
    api_id, api_hash = _credentials()
    record = get_session_record(session_id)
    if not record.get("proxy_enabled"):
        message = (
            "Bu Telegram session için sabit proxy atanmamış. Ayarlar > Session proxy "
            "yönetiminden proxy tanımlayın veya Toplu Proxy Ekle ile boş hesaba proxy dağıtın."
        )
        if mutate_session_state:
            with get_connection() as connection:
                connection.execute(
                    "UPDATE telegram_sessions SET status='proxy_error', last_error=?, updated_at=? WHERE id=?",
                    (message, utc_now(), session_id),
                )
            add_log("error", "proxy", message, session_id)
            add_notification(
                "error",
                "Session proxy bekliyor",
                "Hesap çalıştırılmadı. Ayarlar'dan sabit proxy girin veya Toplu Proxy Ekle ile boş hesaba proxy atayın.",
                "settings",
            )
        raise ProxyUnavailableError(message)
    try:
        await test_session_proxy(session_id, persist_result=mutate_session_state)
    except Exception as error:
        raise ProxyUnavailableError(str(error)) from error
    record = get_session_record(session_id)
    client = TelegramClient(
        StringSession(decrypt(record["session_encrypted"])),
        api_id,
        api_hash,
        proxy=_proxy_config(record),
        timeout=12,
        connection_retries=1,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError("Telegram session geçersiz; hesabı yeniden bağlayın.")
    except Exception as error:
        message = (
            "Proxy testi geçse de Telegram bağlantısı kurulamadı. Proxy sağlayıcınızda Telegram "
            f"erişimini ve IP yetkilendirmesini kontrol edin: {error}"
        )
        if mutate_session_state:
            with get_connection() as connection:
                connection.execute(
                    """
                    UPDATE telegram_sessions
                    SET status='proxy_error', proxy_last_status='failed', proxy_last_error=?,
                        last_error=?, updated_at=? WHERE id=?
                    """,
                    (message[:500], message[:500], utc_now(), session_id),
                )
            add_log("error", "proxy", message, session_id)
            add_notification(
                "error",
                "Telegram proxy bağlantısı kurulamadı",
                "Hesap çalıştırılmadı ve ana IP kullanılmadı. Proxy sağlayıcınızda Telegram erişimini ve IP yetkilendirmesini kontrol edin.",
                "settings",
            )
        raise ProxyUnavailableError(message) from error
    return client


def _proxy_config_from_values(
    proxy_type: str,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
) -> dict:
    if proxy_type not in {"socks5", "http"}:
        raise RuntimeError("Desteklenmeyen proxy türü.")
    if not host or not port:
        raise RuntimeError("Session proxy ayarları eksik. Host ve port bilgilerini kontrol edin.")
    return {
        "proxy_type": proxy_type,
        "addr": host,
        "port": int(port),
        "rdns": True,
        "username": username,
        "password": password,
    }


def _proxy_config(record: dict) -> dict | None:
    if not record.get("proxy_enabled"):
        return None
    if not record.get("proxy_host") or not record.get("proxy_port"):
        raise RuntimeError("Session proxy ayarları eksik. Host ve port bilgilerini kontrol edin.")
    username = (
        decrypt(record["proxy_username_encrypted"])
        if record.get("proxy_username_encrypted")
        else None
    )
    password = (
        decrypt(record["proxy_password_encrypted"])
        if record.get("proxy_password_encrypted")
        else None
    )
    return _proxy_config_from_values(
        record.get("proxy_type") or "socks5",
        record["proxy_host"],
        int(record["proxy_port"]),
        username,
        password,
    )


async def _test_proxy_connection(
    config: dict,
    *,
    session_string: str | None = None,
) -> tuple[str, int]:
    api_id, api_hash = _credentials()
    client, detected_type, latency_ms = await _connect_telegram_through_proxy(
        api_id,
        api_hash,
        config,
        session_string=session_string,
        require_authorized=bool(session_string),
    )
    await client.disconnect()
    return detected_type, latency_ms


async def test_session_proxy(
    session_id: int,
    *,
    persist_result: bool = True,
) -> dict:
    record = get_session_record(session_id)
    config = _proxy_config(record)
    if not config:
        raise RuntimeError("Bu session için proxy etkin değil.")
    try:
        selected_type = config["proxy_type"]
        detected_type, latency_ms = await _test_proxy_connection(
            config,
            session_string=decrypt(record["session_encrypted"]),
        )
        if persist_result:
            with get_connection() as connection:
                connection.execute(
                    """
                    UPDATE telegram_sessions
                    SET proxy_type=?, proxy_last_status='success', proxy_latency_ms=?, proxy_last_error=NULL,
                        proxy_last_test_at=?,
                        status=CASE WHEN status IN ('proxy_error', 'proxy_pending') THEN 'active' ELSE status END,
                        last_error=CASE WHEN status IN ('proxy_error', 'proxy_pending') THEN NULL ELSE last_error END,
                        updated_at=?
                    WHERE id=?
                    """,
                    (detected_type, latency_ms, utc_now(), utc_now(), session_id),
                )
        auto_detected = detected_type != selected_type
        detail = f"Proxy bağlantı testi başarılı: {latency_ms} ms ({detected_type.upper()})"
        if auto_detected:
            detail += f"; {selected_type.upper()} yerine {detected_type.upper()} otomatik seçildi"
        if persist_result:
            add_log("success", "proxy", detail, session_id)
        return {
            "ok": True,
            "status": "success",
            "latency_ms": latency_ms,
            "proxy_type": detected_type,
            "auto_detected": auto_detected,
        }
    except Exception as error:
        message = str(error) or error.__class__.__name__
        if persist_result:
            with get_connection() as connection:
                connection.execute(
                    """
                    UPDATE telegram_sessions
                    SET status='proxy_error', proxy_last_status='failed', proxy_latency_ms=NULL,
                        proxy_last_error=?, proxy_last_test_at=?, last_error=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        message[:500],
                        utc_now(),
                        "Proxy çalışmıyor. Host/port, kullanıcı adı/parola, IP yetkilendirmesi ve proxy süresini kontrol edin."[:500],
                        utc_now(),
                        session_id,
                    ),
                )
            add_log("error", "proxy", f"Proxy bağlantı testi başarısız: {message}", session_id)
            add_notification(
                "error",
                "Proxy bağlantısı durduruldu",
                "Session kullanılmadı. Proxy host/port, kullanıcı bilgileri, IP yetkilendirmesi ve paket süresini kontrol edin.",
                "settings",
            )
        raise ProxyUnavailableError(
            "Proxy bağlantısı kurulamadı; ana IP kullanılmadı. Host/port, kullanıcı adı/parola, "
            f"IP yetkilendirmesi ve proxy süresini kontrol edin. Ayrıntı: {message}"
        ) from error


async def _resolve_entity(client: TelegramClient, reference: str):
    clean_reference: str | int = reference.strip()
    if isinstance(clean_reference, str) and clean_reference.lstrip("-").isdigit():
        clean_reference = int(clean_reference)
        # StringSession kalıcı entity önbelleği tutmaz. Dialogları almak ID çözümlemesini güvenilir hale getirir.
        await client.get_dialogs()
    return await client.get_entity(clean_reference)


def _activity_access_error(
    requested_session_id: int | None,
    access_errors: list[tuple[int, Exception]],
) -> RuntimeError:
    if not access_errors:
        return RuntimeError("Seçilen gruba erişim denenirken bilinmeyen bir hata oluştu.")

    session_id, error = access_errors[-1]
    detail = (str(error).strip() or error.__class__.__name__).rstrip(".")
    invite_note = (
        " Hesap grupta değilse özel grup için t.me/+... davet bağlantısını kullanın."
    )
    if requested_session_id:
        return RuntimeError(
            f"Seçilen session gruba erişemedi: {detail}.{invite_note}"
        )

    attempted_ids = ", ".join(str(item[0]) for item in access_errors)
    return RuntimeError(
        "Uygun session'lar seçilen gruba erişemedi "
        f"(denenen session ID: {attempted_ids}). Son hata, session {session_id}: "
        f"{detail}.{invite_note}"
    )


def _private_invite_hash(reference: str) -> str | None:
    value = reference.strip()
    patterns = (
        r"^(?:https?://)?(?:t|telegram)\.me/\+([A-Za-z0-9_-]+)(?:\?.*)?$",
        r"^(?:https?://)?(?:t|telegram)\.me/joinchat/([A-Za-z0-9_-]+)(?:\?.*)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, value, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


async def _resolve_or_request_group_access(
    client: TelegramClient,
    session_id: int,
    reference: str,
):
    invite_hash = _private_invite_hash(reference)
    if invite_hash:
        invite = await client(CheckChatInviteRequest(invite_hash))
        existing_chat = getattr(invite, "chat", None)
        if existing_chat is not None:
            return existing_chat
        group_title = getattr(invite, "title", "Özel grup")
        try:
            updates = await client(ImportChatInviteRequest(invite_hash))
        except InviteRequestSentError as error:
            raise GroupJoinPending(session_id, group_title) from error
        chats = getattr(updates, "chats", None) or []
        if chats:
            return chats[0]
        raise RuntimeError("Katılım tamamlandı ancak Telegram grup bilgisini döndürmedi.")

    try:
        entity = await _resolve_entity(client, reference)
    except Exception as error:
        if reference.strip().lstrip("-").isdigit():
            raise RuntimeError(
                "Hesabın üye olmadığı özel gruba yalnızca grup ID'si ile katılım isteği gönderilemez. t.me/+... davet bağlantısını girin."
            ) from error
        raise
    if not isinstance(entity, (Channel, Chat)):
        raise TypeError("Girilen referans Telegram grubu değil.")
    if isinstance(entity, Channel):
        try:
            await client(GetParticipantRequest(entity, "me"))
        except UserNotParticipantError:
            try:
                await client(JoinChannelRequest(entity))
            except InviteRequestSentError as error:
                raise GroupJoinPending(
                    session_id,
                    getattr(entity, "title", "Telegram grubu"),
                ) from error
            entity = await _resolve_entity(client, reference)
    return entity


def _activity_session_candidates(requested_session_id: int | None) -> list[int]:
    today = datetime.now(UTC).date().isoformat()
    quota = max(1, int(get_app_setting("activity_daily_quota") or DEFAULT_DAILY_ACTIVITY_QUOTA))
    with get_connection() as connection:
        sessions = connection.execute(
            """
            SELECT s.id, s.label, s.session_encrypted, s.status,
                   s.flood_wait_until, s.batch_cooldown_until,
                   s.proxy_enabled, s.proxy_type, s.proxy_host, s.proxy_port,
                   s.proxy_last_status, s.proxy_last_error,
                   COALESCE(u.operation_count, 0) operation_count,
                   u.last_used_at
            FROM telegram_sessions s
            LEFT JOIN session_usage_daily u
              ON u.session_id = s.id AND u.usage_date = ?
            ORDER BY s.id ASC
            """,
            (today,),
        ).fetchall()

    audit: list[tuple[dict, bool, list[str]]] = []
    status_eligible: list[dict] = []
    available: list[dict] = []
    for row in sessions:
        status = str(row.get("status") or "").lower()
        reasons: list[str] = []
        eligible = status in {"active", "proxy_pending", "batch_wait", "flood_wait"}
        if status == "flood_wait":
            reasons.append(
                "invite/FloodWait session durumu aktivite taramasını önceden engellemez; "
                "gerçek tarama isteği Telegram tarafından ayrıca doğrulanır"
            )
        elif status == "batch_wait":
            reasons.append("invite batch beklemesi aktivite taramasını engellemez")
        elif status == "proxy_pending":
            reasons.append("proxy çalışma anında gerçek Telegram bağlantısıyla doğrulanacak")
        elif status == "active":
            reasons.append("status aktivite taramasına uygun")
        else:
            reasons.append(f"durum aktivite taramasına uygun değil: {status or 'boş'}")

        if not row.get("session_encrypted"):
            eligible = False
            reasons.append("Telegram session verisi bulunmuyor")
        if not row.get("proxy_enabled"):
            eligible = False
            reasons.append("session proxy etkin değil; ana IP kullanımı yasak")
        elif not row.get("proxy_host") or not row.get("proxy_port"):
            eligible = False
            reasons.append("session proxy host/port ayarı eksik")
        if str(row.get("proxy_last_status") or "").lower() == "failed":
            eligible = False
            reasons.append(
                "son proxy testi başarısız"
                + (f": {row.get('proxy_last_error')}" if row.get("proxy_last_error") else "")
            )

        if eligible:
            status_eligible.append(row)
            if int(row.get("operation_count") or 0) >= quota:
                reasons.append(
                    f"günlük aktivite kotası dolu: {int(row.get('operation_count') or 0)}/{quota}"
                )
                eligible = False
            else:
                available.append(row)
                reasons.append(
                    f"aktivite kotası uygun: {int(row.get('operation_count') or 0)}/{quota}"
                )
        audit.append((row, eligible, reasons))

    for row, accepted, reasons in audit:
        requested_note = " · tercih edilen session" if row["id"] == requested_session_id else ""
        add_log(
            "info" if accepted else "warning",
            "activity_selector",
            (
                f"Session #{row['id']} ({row.get('label') or 'İsimsiz'}) "
                f"{'KABUL' if accepted else 'RED'}{requested_note} — "
                f"status={row.get('status') or 'boş'}; "
                + "; ".join(reasons)
            ),
            session_id=int(row["id"]),
        )

    available_ids = [int(row["id"]) for row in available]
    if requested_session_id in available_ids:
        add_log(
            "info",
            "activity_selector",
            f"Tercih edilen Session #{requested_session_id} aktivite taraması için seçildi.",
            session_id=requested_session_id,
        )
        return [requested_session_id]

    if not available and status_eligible:
        tomorrow = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        raise SessionBudgetWaiting(tomorrow)
    if not available:
        add_log(
            "error",
            "activity_selector",
            (
                "Aktivite taraması için hiçbir session durum/kota kurallarını geçemedi. "
                f"Tercih edilen session: {requested_session_id or 'otomatik'}."
            ),
        )
        return []

    cursor_value = get_app_setting("activity_round_robin_cursor")
    cursor_id = int(cursor_value) if cursor_value and cursor_value.isdigit() else None
    all_ids = [row["id"] for row in sessions]
    start_index = 0
    if requested_session_id in all_ids:
        start_index = (all_ids.index(requested_session_id) + 1) % len(all_ids)
    elif cursor_id in all_ids:
        start_index = (all_ids.index(cursor_id) + 1) % len(all_ids)
    circular_ids = all_ids[start_index:] + all_ids[:start_index]
    available_id_set = set(available_ids)
    selected_ids = [session_id for session_id in circular_ids if session_id in available_id_set]
    if requested_session_id is not None:
        add_log(
            "warning",
            "activity_selector",
            (
                f"Tercih edilen Session #{requested_session_id} kullanılamadığı için aktivite taraması "
                f"Round-Robin ile devam ediyor: {selected_ids}."
            ),
            session_id=requested_session_id,
        )
    else:
        add_log(
            "info",
            "activity_selector",
            f"Otomatik aktivite Round-Robin sırası: {selected_ids}.",
        )
    return selected_ids


def _record_activity_operation(session_id: int) -> None:
    today = datetime.now(UTC).date().isoformat()
    now = utc_now()
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO session_usage_daily(session_id, usage_date, operation_count, last_used_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(session_id, usage_date) DO UPDATE SET
                operation_count=operation_count + 1,
                last_used_at=excluded.last_used_at
            """,
            (session_id, today, now),
        )


async def resolve_group(session_id: int, reference: str) -> dict:
    lease = await acquire_session_operation(
        session_id,
        "group_resolve",
        f"group:{reference.strip()[:80]}",
        "grup doğrulama",
    )
    client = None
    try:
        client = await _client_for(session_id)
        entity = await _resolve_entity(client, reference)
        if not isinstance(entity, (Channel, Chat)):
            raise TypeError("Girilen referans bir Telegram grubu veya kanalı değil.")
        kind = "megagroup" if getattr(entity, "megagroup", False) else "channel" if isinstance(entity, Channel) else "group"
        admin_rights = getattr(entity, "admin_rights", None)
        can_invite_users = _entity_can_invite_users(entity)
        source_suitable = kind in {"group", "megagroup"}
        result = ResolvedGroup(
            id=entity.id,
            title=getattr(entity, "title", "İsimsiz grup"),
            username=getattr(entity, "username", None),
            kind=kind,
            participants_count=getattr(entity, "participants_count", None),
            creator=bool(getattr(entity, "creator", False)),
            admin_rights=bool(admin_rights),
            can_invite_users=can_invite_users,
            source_suitable=source_suitable,
            target_suitable=source_suitable and can_invite_users,
        )
        add_log("info", "group", f"Grup doğrulandı: {result.title}", session_id=session_id)
        return asdict(result)
    except FloodWaitError as error:
        until = datetime.now(UTC) + timedelta(seconds=error.seconds)
        with get_connection() as connection:
            connection.execute(
                "UPDATE telegram_sessions SET status='flood_wait', flood_wait_until=?, updated_at=? WHERE id=?",
                (until.isoformat(), utc_now(), session_id),
            )
        add_log("warning", "flood_wait", f"Telegram {error.seconds} saniye bekleme istedi", session_id=session_id)
        add_notification("warning", "FloodWait etkin", f"Hesap {error.seconds} saniye zorunlu beklemeye alındı.", "sessions")
        raise RuntimeError(f"Telegram {error.seconds} saniye bekleme istedi; hesap otomatik beklemeye alındı.") from error
    finally:
        try:
            if client is not None:
                await client.disconnect()
        finally:
            await lease.release()


async def list_groups(session_id: int) -> list[dict]:
    lease = await acquire_session_operation(
        session_id,
        "group_list",
        "groups:list",
        "grup listesini yükleme",
    )
    client = None
    groups: list[dict] = []
    try:
        client = await _client_for(session_id)
        async for dialog in client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                entity = dialog.entity
                kind = (
                    "megagroup"
                    if getattr(entity, "megagroup", False)
                    else "group"
                    if dialog.is_group
                    else "channel"
                )
                creator = bool(getattr(entity, "creator", False))
                admin_rights = getattr(entity, "admin_rights", None)
                can_invite_users = _entity_can_invite_users(entity)
                source_suitable = kind in {"group", "megagroup"}
                groups.append(
                    {
                        "id": utils.get_peer_id(entity),
                        "title": dialog.name,
                        "username": getattr(entity, "username", None),
                        "kind": kind,
                        "participants_count": getattr(entity, "participants_count", None),
                        "unread_count": dialog.unread_count,
                        "creator": creator,
                        "admin_rights": bool(admin_rights),
                        "can_invite_users": can_invite_users,
                        "source_suitable": source_suitable,
                        "target_suitable": source_suitable and can_invite_users,
                    }
                )
        return groups
    finally:
        try:
            if client is not None:
                await client.disconnect()
        finally:
            await lease.release()


async def _iter_transfer_source_users(
    client: TelegramClient,
    source: Channel | Chat,
    message_limit: int = 50000,
):
    """Yield users only with a source-message context valid for direct adding."""
    seen_ids: set[int] = set()
    async for message in client.iter_messages(source, limit=message_limit):
        sender_id = getattr(message, "sender_id", None)
        if not sender_id or sender_id in seen_ids:
            continue
        sender = await message.get_sender()
        if not isinstance(sender, User):
            continue
        message_id = getattr(message, "id", None)
        if not message_id:
            continue
        seen_ids.add(sender.id)
        yield sender, message_id


async def preview_job_candidates(job: dict) -> dict:
    session_id = job["session_id"]
    job_id = job["id"]
    lease = await acquire_session_operation(
        session_id,
        "candidate_preview",
        f"preview:{job_id}",
        f"JOB-{job_id} aday taraması",
    )
    client = None
    try:
        client = await _client_for(session_id)
        source = await _resolve_entity(client, job["source_ref"])
        target = await _resolve_entity(client, job["target_ref"])
        if not isinstance(source, (Channel, Chat)) or not isinstance(target, (Channel, Chat)):
            raise TypeError("Çekilecek ve gönderilecek alanlar Telegram grubu olmalı.")

        admin_rights = getattr(target, "admin_rights", None)
        can_invite = _entity_can_invite_users(target)
        if not can_invite:
            raise RuntimeError(
                "Seçilen session hedef grupta üye ekleme yetkisine sahip değil. "
                "Session'ı hedef gruba katın ve grubun genel 'Üye ekle' iznini açın "
                "veya hesabı 'Kullanıcı davet et' yetkili yönetici yapın."
            )

        target_member_ids: set[int] = set()
        try:
            async for target_user in client.iter_participants(target, limit=20000):
                target_member_ids.add(target_user.id)
        except FloodWaitError:
            raise
        except Exception as error:
            raise RuntimeError(
                "Gönderilecek grubun mevcut üyeleri okunamadı. Tekrarları güvenle ayıklamak için hesapta yeterli grup yetkisi olmalı."
            ) from error

        source_admin_ids: set[int] = set()
        try:
            if isinstance(source, Channel):
                async for admin_user in client.iter_participants(
                    source,
                    filter=ChannelParticipantsAdmins,
                ):
                    source_admin_ids.add(admin_user.id)
            else:
                full_chat = await client(GetFullChatRequest(source.id))
                participants = getattr(
                    getattr(full_chat.full_chat, "participants", None),
                    "participants",
                    [],
                )
                source_admin_ids.update(
                    participant.user_id
                    for participant in participants
                    if isinstance(participant, (ChatParticipantAdmin, ChatParticipantCreator))
                )
        except FloodWaitError:
            raise
        except Exception as error:
            raise RuntimeError(
                "Kaynak grubun yönetici listesi güvenli biçimde okunamadı. Yöneticileri hariç tutmadan önizleme yapılmaz."
            ) from error

        with get_connection() as connection:
            previously_used_ids = {
                row["telegram_user_id"]
                for row in connection.execute(
                    "SELECT telegram_user_id FROM member_history"
                ).fetchall()
            }

        rows: list[tuple] = []
        counts = {
            "eligible": 0,
            "existing": 0,
            "bot": 0,
            "deleted": 0,
            "admin": 0,
            "previously_used": 0,
        }
        examined_ids: set[int] = set()
        activity_scan_id: int | None = None

        def add_candidate(
            user_id: int,
            display_name: str,
            username: str | None,
            access_hash: int | None,
            source_message_id: int | None,
            *,
            is_bot: bool = False,
            is_deleted: bool = False,
        ) -> bool:
            if user_id in examined_ids:
                return False
            examined_ids.add(user_id)
            if is_deleted:
                status, reason = "deleted", "Silinmiş Telegram hesabı"
            elif is_bot:
                status, reason = "bot", "Bot hesabı"
            elif user_id in source_admin_ids:
                status, reason = "admin", "Kaynak grubun sahibi veya yöneticisi"
            elif user_id in target_member_ids:
                status, reason = "existing", "Gönderilecek grupta zaten bulunuyor"
            elif user_id in previously_used_ids:
                status, reason = "previously_used", "Daha önce Pawgram iş geçmişine alınmış"
            else:
                status, reason = "eligible", "Önizleme için uygun"
            counts[status] += 1
            if status in {"admin", "bot", "deleted"}:
                return False
            rows.append(
                (
                    job_id,
                    user_id,
                    display_name,
                    username,
                    access_hash,
                    source_message_id,
                    status,
                    reason,
                    utc_now(),
                )
            )
            return counts["eligible"] >= job["max_users"]

        with get_connection() as connection:
            activity_scan = connection.execute(
                """
                SELECT id FROM activity_scans
                WHERE session_id=? AND status IN ('completed', 'scheduled')
                  AND (group_id=? OR LOWER(group_ref)=LOWER(?) OR LOWER(group_title)=LOWER(?))
                ORDER BY last_run_at DESC, id DESC
                LIMIT 1
                """,
                (session_id, job["source_id"], job["source_ref"], job["source_title"]),
            ).fetchone()
            activity_users = []
            if activity_scan:
                activity_scan_id = activity_scan["id"]
                activity_users = connection.execute(
                    """
                    SELECT telegram_user_id, display_name, username, access_hash, source_message_id
                    FROM activity_results
                    WHERE scan_id=? AND source_message_id IS NOT NULL
                    ORDER BY last_message_at DESC, id
                    """,
                    (activity_scan_id,),
                ).fetchall()

        for activity_user in activity_users:
            if add_candidate(
                activity_user["telegram_user_id"],
                activity_user["display_name"],
                activity_user["username"],
                activity_user.get("access_hash"),
                activity_user.get("source_message_id"),
            ):
                break

        if counts["eligible"] < job["max_users"]:
            async for user, source_message_id in _iter_transfer_source_users(
                client,
                source,
            ):
                display_name = " ".join(
                    value for value in [user.first_name, user.last_name] if value
                ).strip() or "İsimsiz kullanıcı"
                if add_candidate(
                    user.id,
                    display_name,
                    user.username,
                    getattr(user, "access_hash", None),
                    source_message_id,
                    is_bot=bool(getattr(user, "bot", False)),
                    is_deleted=bool(getattr(user, "deleted", False)),
                ):
                    break

        now = utc_now()
        with get_connection() as connection:
            connection.execute("DELETE FROM job_candidates WHERE job_id = ?", (job_id,))
            connection.executemany(
                """
                INSERT INTO job_candidates(
                    job_id, telegram_user_id, display_name, username, access_hash,
                    source_message_id, status, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            connection.execute(
                """
                UPDATE transfer_jobs
                SET status='previewed', previewed_at=?, candidate_count=?, updated_at=?
                WHERE id=?
                """,
                (now, counts["eligible"], now, job_id),
            )
        add_log(
            "success",
            "preview",
            f"Aday önizlemesi tamamlandı: {counts['eligible']} uygun, "
            f"{sum(value for key, value in counts.items() if key != 'eligible')} atlandı",
            session_id,
            job_id,
        )

        return {
            **counts,
            "scanned": len(examined_ids),
            "activity_scan_id": activity_scan_id,
            "target_members_checked": len(target_member_ids),
            "source_admins_excluded": len(source_admin_ids),
            "history_users_checked": len(previously_used_ids),
            "permissions": {
                "can_invite_users": can_invite,
                "is_creator": bool(getattr(target, "creator", False)),
                "has_admin_rights": bool(admin_rights),
            },
        }
    except FloodWaitError as error:
        until = datetime.now(UTC) + timedelta(seconds=error.seconds)
        with get_connection() as connection:
            connection.execute(
                "UPDATE telegram_sessions SET status='flood_wait', flood_wait_until=?, updated_at=? WHERE id=?",
                (until.isoformat(), utc_now(), session_id),
            )
        add_log("warning", "flood_wait", f"Önizleme sırasında {error.seconds} saniye bekleme istendi", session_id, job_id)
        add_notification("warning", "Önizleme bekletildi", f"Telegram {error.seconds} saniye FloodWait uyguladı.", "jobs")
        raise RuntimeError(
            f"Telegram {error.seconds} saniye bekleme istedi; hesap zorunlu beklemeye alındı."
        ) from error
    finally:
        try:
            if client is not None:
                await client.disconnect()
        finally:
            await lease.release()


@dataclass
class InviteSessionSelection:
    session_id: int | None
    resume_at: datetime | None = None
    invite_count: int = 0
    batch_success_count: int = 0
    invite_batch_limit: int = 3
    invite_cooldown_minutes: int = 20


@dataclass
class InviteSessionContext:
    session_id: int
    client: TelegramClient
    lease: SessionOperationLease
    target: Channel | Chat
    source_input: object | None
    invite_count: int
    batch_success_count: int
    invite_batch_limit: int
    invite_cooldown_minutes: int


def _session_wait_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        wait_until = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if wait_until.tzinfo is None:
        return wait_until.replace(tzinfo=UTC)
    return wait_until.astimezone(UTC)


def select_next_available_session(
    connection,
    last_session_id: int | None,
    usage_date: str,
    daily_limit: int,
    *,
    preferred_session_id: int | None = None,
    working_start: str = "00:00",
    working_end: str = "23:59",
    excluded_session_ids: set[int] | None = None,
    now: datetime | None = None,
    job_id: int | None = None,
) -> InviteSessionSelection:
    """Select one immediately usable invite session and calculate the next recovery time."""
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    excluded = excluded_session_ids or set()
    rows = connection.execute(
        """
        SELECT s.id, s.label, s.session_encrypted, s.status,
               s.flood_wait_until, s.batch_cooldown_until,
               s.proxy_enabled, s.proxy_last_status, s.batch_success_count,
               s.proxy_last_error, s.invite_batch_limit, s.invite_cooldown_minutes,
               COALESCE(u.invite_count, 0) AS invite_count,
               l.session_id AS locked_session_id,
               l.operation_type, l.operation_label
        FROM telegram_sessions s
        LEFT JOIN session_invite_usage_daily u
          ON u.session_id=s.id AND u.usage_date=?
        LEFT JOIN session_operation_locks l ON l.session_id=s.id
        ORDER BY s.id
        """,
        (usage_date,),
    ).fetchall()

    available: list[dict] = []
    audit: list[tuple[dict, str, bool, list[str]]] = []
    earliest_resume: datetime | None = None
    tomorrow = current_time.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    quota_resume = next_working_time(working_start, working_end, tomorrow)

    def remember_resume(candidate: datetime | None) -> None:
        nonlocal earliest_resume
        if candidate is not None and (earliest_resume is None or candidate < earliest_resume):
            earliest_resume = candidate

    for row in rows:
        session_id = int(row["id"])
        status = str(row.get("status") or "").lower()
        flood_until = _session_wait_time(row.get("flood_wait_until"))
        batch_until = _session_wait_time(row.get("batch_cooldown_until"))
        session_resume: datetime | None = None
        reasons: list[str] = []
        permanently_rejected = False

        if not row.get("session_encrypted"):
            reasons.append("Telegram session verisi bulunmuyor")
            permanently_rejected = True

        if status == "flood_wait":
            if flood_until is not None and flood_until <= current_time:
                connection.execute(
                    """
                    UPDATE telegram_sessions
                    SET status='active', flood_wait_until=NULL, last_error=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (utc_now(), session_id),
                )
                status = "active"
                reasons.append("FloodWait süresi doldu; status active olarak yenilendi")
            else:
                session_resume = flood_until
                reasons.append(
                    f"FloodWait devam ediyor: {row.get('flood_wait_until') or 'bitiş zamanı yok'}"
                )
        elif status == "batch_wait":
            if batch_until is not None and batch_until <= current_time:
                connection.execute(
                    """
                    UPDATE telegram_sessions
                    SET status='active', batch_success_count=0,
                        batch_cooldown_until=NULL, last_error=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (utc_now(), session_id),
                )
                row["batch_success_count"] = 0
                status = "active"
                reasons.append("batch cooldown süresi doldu; status active olarak yenilendi")
            else:
                session_resume = batch_until
                reasons.append(
                    f"batch cooldown devam ediyor: {row.get('batch_cooldown_until') or 'bitiş zamanı yok'}"
                )

        if status not in {"active", "flood_wait", "batch_wait"}:
            reasons.append(f"status invite için uygun değil: {status or 'boş'}")
            permanently_rejected = True
        if status != "active" and session_resume is None:
            reasons.append("otomatik yeniden kullanılabilir olacağı bir zaman bulunmuyor")
            permanently_rejected = True
        if not row.get("proxy_enabled"):
            reasons.append("proxy devre dışı")
            permanently_rejected = True
        elif str(row.get("proxy_last_status") or "").lower() == "failed":
            proxy_error = str(row.get("proxy_last_error") or "proxy testi başarısız")
            reasons.append(f"proxy kullanılamıyor: {proxy_error}")
            permanently_rejected = True
        if int(row.get("invite_count") or 0) >= daily_limit:
            session_resume = max(session_resume, quota_resume) if session_resume else quota_resume
            reasons.append(
                f"günlük davet kotası dolu: {int(row.get('invite_count') or 0)}/{daily_limit}; "
                f"yeniden deneme: {quota_resume.isoformat()}"
            )
        if row.get("locked_session_id") is not None:
            lock_retry = current_time + timedelta(seconds=5)
            session_resume = max(session_resume, lock_retry) if session_resume else lock_retry
            operation = row.get("operation_label") or row.get("operation_type") or "başka bir Telegram işlemi"
            reasons.append(f"session işlem kilidi altında: {operation}")
        if session_id in excluded:
            reasons.append("hedef gruba erişemediği veya üye ekleme yetkisi olmadığı için bu iş turunda hariç tutuldu")
            permanently_rejected = True
        if permanently_rejected:
            audit.append((row, status, False, reasons))
            continue
        if session_resume is not None:
            remember_resume(session_resume)
            audit.append((row, status, False, reasons))
            continue
        reasons.extend(
            [
                f"status uygun: {status}",
                f"günlük davet kotası uygun: {int(row.get('invite_count') or 0)}/{daily_limit}",
                f"proxy uygun: {row.get('proxy_last_status') or 'durum kaydı yok'}",
                "session işlem kilidi yok",
            ]
        )
        available.append(row)
        audit.append((row, status, True, reasons))

    for row, effective_status, accepted, reasons in audit:
        connection.execute(
            """
            INSERT INTO system_logs(level, category, message, session_id, job_id, created_at)
            VALUES (?, 'invite_selector', ?, ?, ?, ?)
            """,
            (
                "info" if accepted else "warning",
                (
                    f"Session #{row['id']} ({row.get('label') or 'İsimsiz'}) "
                    f"{'KABUL' if accepted else 'RED'} — status={effective_status or 'boş'}; "
                    + "; ".join(reasons)
                ),
                int(row["id"]),
                job_id,
                utc_now(),
            ),
        )

    selected = None
    if preferred_session_id is not None:
        selected = next(
            (row for row in available if int(row["id"]) == preferred_session_id),
            None,
        )
    if selected is None and available:
        if last_session_id is None:
            selected = available[0]
        else:
            selected = next(
                (row for row in available if int(row["id"]) > last_session_id),
                available[0],
            )
    if selected is None:
        return InviteSessionSelection(None, earliest_resume)
    connection.execute(
        """
        INSERT INTO system_logs(level, category, message, session_id, job_id, created_at)
        VALUES ('info', 'invite_selector', ?, ?, ?, ?)
        """,
        (
            f"Session #{selected['id']} Round-Robin sırasından invite işi için SEÇİLDİ.",
            int(selected["id"]),
            job_id,
            utc_now(),
        ),
    )
    return InviteSessionSelection(
        session_id=int(selected["id"]),
        invite_count=int(selected.get("invite_count") or 0),
        batch_success_count=int(selected.get("batch_success_count") or 0),
        invite_batch_limit=max(1, int(selected.get("invite_batch_limit") or 3)),
        invite_cooldown_minutes=max(5, int(selected.get("invite_cooldown_minutes") or 20)),
    )


async def _close_invite_session(context: InviteSessionContext | None) -> None:
    if context is None:
        return
    try:
        with suppress(Exception):
            await context.client.disconnect()
    finally:
        await context.lease.release()


async def _open_invite_session(
    selection: InviteSessionSelection,
    job: dict,
    candidates: list[dict],
    job_id: int,
) -> InviteSessionContext:
    if selection.session_id is None:
        raise ValueError("Invite session seçimi bir session ID içermelidir.")
    session_id = selection.session_id
    lease = await acquire_session_operation(
        session_id,
        "invite_job",
        f"job:{job_id}",
        f"JOB-{job_id} üye ekleme",
        wait=False,
    )
    client = None
    try:
        try:
            client = await asyncio.wait_for(_client_for(session_id), timeout=30)
        except TimeoutError as error:
            raise ProxyUnavailableError(
                "Proxy üzerinden Telegram session bağlantısı 30 saniye içinde kurulamadı; "
                "ana IP kullanılmadı."
            ) from error
        target = await asyncio.wait_for(_resolve_entity(client, job["target_ref"]), timeout=30)
        if not isinstance(target, (Channel, Chat)):
            raise TypeError("Hedef Telegram grubu olmalıdır.")
        if not _entity_can_invite_users(target):
            raise TargetGroupUnavailableError(
                session_id,
                f"Session #{session_id} hedef grupta üye ekleme yetkisine sahip değil. "
                "Session'ı hedef gruba katın ve grubun genel 'Üye ekle' iznini açın "
                "veya hesabı 'Kullanıcı davet et' yetkili yönetici yapın."
            )

        source_input = None
        if any(candidate.get("source_message_id") for candidate in candidates):
            source = await asyncio.wait_for(_resolve_entity(client, job["source_ref"]), timeout=30)
            source_input = await asyncio.wait_for(client.get_input_entity(source), timeout=15)

        return InviteSessionContext(
            session_id=session_id,
            client=client,
            lease=lease,
            target=target,
            source_input=source_input,
            invite_count=selection.invite_count,
            batch_success_count=selection.batch_success_count,
            invite_batch_limit=selection.invite_batch_limit,
            invite_cooldown_minutes=selection.invite_cooldown_minutes,
        )
    except Exception:
        if client is not None:
            with suppress(Exception):
                await client.disconnect()
        await lease.release()
        raise


async def _resolve_invite_candidate(
    context: InviteSessionContext,
    candidate: dict,
) -> User | InputUser | None:
    user: User | InputUser | None = None
    source_message_id = candidate.get("source_message_id")
    access_hash = candidate.get("access_hash")
    if source_message_id and context.source_input is not None:
        contextual_user = InputUserFromMessage(
            context.source_input,
            int(source_message_id),
            candidate["telegram_user_id"],
        )
        try:
            resolved_users = await asyncio.wait_for(
                context.client(GetUsersRequest([contextual_user])),
                timeout=30,
            )
        except (FloodWaitError, PeerFloodError):
            raise
        except Exception:  # noqa: BLE001 - fall through to the contextual reference
            resolved_users = []
        resolved_user = next(
            (
                item
                for item in resolved_users
                if isinstance(item, User)
                and item.id == candidate["telegram_user_id"]
                and getattr(item, "access_hash", None) is not None
            ),
            None,
        )
        if resolved_user is not None:
            access_hash = int(resolved_user.access_hash)
            user = InputUser(resolved_user.id, access_hash)
            with get_connection() as connection:
                connection.execute(
                    "UPDATE job_candidates SET access_hash=? WHERE id=?",
                    (access_hash, candidate["id"]),
                )
        else:
            user = contextual_user
    elif access_hash is not None:
        stored_user = InputUser(candidate["telegram_user_id"], int(access_hash))
        try:
            resolved_users = await asyncio.wait_for(
                context.client(GetUsersRequest([stored_user])),
                timeout=30,
            )
        except (FloodWaitError, PeerFloodError):
            raise
        except Exception:  # noqa: BLE001 - try the remaining safe resolution strategies
            resolved_users = []
        resolved_user = next(
            (
                item
                for item in resolved_users
                if isinstance(item, User)
                and item.id == candidate["telegram_user_id"]
                and getattr(item, "access_hash", None) is not None
            ),
            None,
        )
        if resolved_user is not None:
            user = InputUser(resolved_user.id, int(resolved_user.access_hash))
    if user is None and candidate.get("username"):
        try:
            resolved_user = await asyncio.wait_for(
                context.client.get_entity(candidate["username"]),
                timeout=20,
            )
        except (FloodWaitError, PeerFloodError):
            raise
        except Exception:  # noqa: BLE001 - username resolution is optional
            resolved_user = None
        if isinstance(resolved_user, User):
            user = resolved_user
    if user is None and access_hash is None:
        try:
            resolved_user = await asyncio.wait_for(
                context.client.get_entity(candidate["telegram_user_id"]),
                timeout=20,
            )
        except (FloodWaitError, PeerFloodError):
            raise
        except Exception:  # noqa: BLE001 - entity-cache resolution is optional
            resolved_user = None
        if isinstance(resolved_user, User):
            user = resolved_user
    return user


def _mark_invite_flood_wait(
    session_id: int,
    wait_until: datetime,
    message: str,
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE telegram_sessions
            SET status='flood_wait', flood_wait_until=?, last_error=?, updated_at=?
            WHERE id=?
            """,
            (wait_until.isoformat(), message, utc_now(), session_id),
        )


def _mark_invite_proxy_error(session_id: int, message: str) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE telegram_sessions
            SET status='proxy_error', proxy_last_status='failed',
                proxy_last_error=?, last_error=?, updated_at=?
            WHERE id=?
            """,
            (message[:500], message[:500], utc_now(), session_id),
        )


def _schedule_invite_for_available_session(
    job_id: int,
    selection: InviteSessionSelection,
    last_error: str | None = None,
) -> None:
    now = utc_now()
    if selection.resume_at is not None:
        message = (
            "Tüm Telegram session'ları geçici olarak kullanılamıyor. "
            f"Kalan adaylar korunarak iş {selection.resume_at.isoformat()} zamanına planlandı."
        )
        status = "scheduled"
        resume_at = selection.resume_at.isoformat()
    else:
        message = last_error or (
            "Kullanılabilir Telegram session bulunamadı. Kalan adaylar korunmuştur; "
            "devre dışı veya proxy hatalı hesapları kontrol edin."
        )
        status = "proxy_error"
        resume_at = None
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE transfer_jobs
            SET status=?, resume_at=?, last_error=?, updated_at=?
            WHERE id=?
            """,
            (status, resume_at, message, now, job_id),
        )
    add_log("warning", "invite_session", message, job_id=job_id)
    add_notification(
        "warning",
        "Tüm session'lar beklemede" if selection.resume_at is not None else "Davet session'ı bulunamadı",
        message,
        "jobs",
    )


async def execute_invite_job(job_id: int) -> None:
    """Invite selected candidates while rotating only unavailable Telegram sessions."""
    runnable_statuses = {
        "approved",
        "scheduled",
        "paused_quota",
        "paused_batch",
        "proxy_error",
        "flood_wait",
        "queued_execution",
    }
    with get_connection() as connection:
        job = connection.execute("SELECT * FROM transfer_jobs WHERE id=?", (job_id,)).fetchone()
        candidates = connection.execute(
            """
            SELECT * FROM job_candidates
            WHERE job_id=? AND selected=1 AND status='eligible'
            ORDER BY CASE WHEN source_message_id IS NOT NULL THEN 0 ELSE 1 END, id
            """,
            (job_id,),
        ).fetchall()
        if not job or job["status"] not in runnable_statuses or not candidates:
            return
        now = utc_now()
        claimed = connection.execute(
            """
            UPDATE transfer_jobs
            SET status='running', execution_started_at=COALESCE(execution_started_at, ?),
                last_error=NULL, updated_at=?
            WHERE id=? AND status IN (
                'approved', 'scheduled', 'paused_quota', 'paused_batch',
                'proxy_error', 'flood_wait', 'queued_execution'
            )
            """,
            (now, now, job_id),
        )
        if claimed.rowcount != 1:
            return

    next_run = next_job_run(job)
    if next_run > datetime.now(UTC):
        message = f"İş planlanan çalışma penceresinde otomatik başlayacak: {next_run.isoformat()}"
        with get_connection() as connection:
            connection.execute(
                "UPDATE transfer_jobs SET status='scheduled', resume_at=?, last_error=?, updated_at=? WHERE id=?",
                (next_run.isoformat(), message, utc_now(), job_id),
            )
        return

    configured_quota = max(1, int(job["daily_limit"]))
    current_session_id = int(job["session_id"])
    last_session_id = current_session_id
    preferred_session_id: int | None = current_session_id
    context: InviteSessionContext | None = None
    candidate_index = 0
    last_unavailable_error: str | None = None
    pending_handoff_from_session_id: int | None = None
    pending_handoff_reason: str | None = None
    target_unavailable_session_ids: set[int] = set()

    try:
        while candidate_index < len(candidates):
            candidate = candidates[candidate_index]
            working_time = next_working_time(
                str(job.get("working_start") or "00:00"),
                str(job.get("working_end") or "23:59"),
            )
            if working_time > datetime.now(UTC):
                message = f"Çalışma saati sona erdi; iş otomatik devam edecek: {working_time.isoformat()}"
                with get_connection() as connection:
                    connection.execute(
                        "UPDATE transfer_jobs SET status='scheduled', resume_at=?, last_error=?, updated_at=? WHERE id=?",
                        (working_time.isoformat(), message, utc_now(), job_id),
                    )
                add_log("info", "schedule", message, current_session_id, job_id)
                return

            if context is None:
                today = datetime.now(UTC).date().isoformat()
                with get_connection() as connection:
                    selection = select_next_available_session(
                        connection,
                        last_session_id,
                        today,
                        configured_quota,
                        preferred_session_id=preferred_session_id,
                        working_start=str(job.get("working_start") or "00:00"),
                        working_end=str(job.get("working_end") or "23:59"),
                        excluded_session_ids=target_unavailable_session_ids,
                        job_id=job_id,
                    )
                preferred_session_id = None
                if selection.session_id is None:
                    if target_unavailable_session_ids and selection.resume_at is None:
                        rejected = ", ".join(
                            f"#{session_id}" for session_id in sorted(target_unavailable_session_ids)
                        )
                        raise RuntimeError(
                            "Hedef grupta kullanılabilir üye ekleme yetkisine sahip Telegram session "
                            f"bulunamadı. Reddedilen session'lar: {rejected}. "
                            "Sessionlar ekranındaki 'Sessionları gruba hazırla' işlemini çalıştırın; "
                            "grubun genel 'Üye ekle' iznini açın veya hesaplara "
                            "'Kullanıcı davet et' yönetici yetkisi verin."
                        )
                    _schedule_invite_for_available_session(
                        job_id,
                        selection,
                        last_unavailable_error,
                    )
                    return

                selected_session_id = int(selection.session_id)
                try:
                    context = await _open_invite_session(selection, job, candidates, job_id)
                except SessionOperationBusy:
                    last_session_id = selected_session_id
                    continue
                except FloodWaitError as error:
                    wait_until = datetime.now(UTC) + timedelta(seconds=error.seconds)
                    reason = f"Session #{selected_session_id} {error.seconds} saniye FloodWait aldı"
                    message = f"{reason}; sıradaki uygun session aranıyor."
                    _mark_invite_flood_wait(selected_session_id, wait_until, message)
                    add_log("warning", "flood_wait", message, selected_session_id, job_id)
                    add_notification("warning", "Session geçici olarak bekliyor", message, "sessions")
                    pending_handoff_from_session_id = selected_session_id
                    pending_handoff_reason = reason
                    last_session_id = selected_session_id
                    continue
                except PeerFloodError:
                    wait_until = datetime.now(UTC) + timedelta(hours=24)
                    reason = (
                        f"Session #{selected_session_id} Telegram spam korumasına takıldı "
                        "ve 24 saat dinlenmeye alındı"
                    )
                    message = f"{reason}; sıradaki uygun session aranıyor."
                    _mark_invite_flood_wait(selected_session_id, wait_until, message)
                    add_log("warning", "peer_flood", message, selected_session_id, job_id)
                    add_notification("warning", "Telegram ekleme kısıtlaması", message, "sessions")
                    pending_handoff_from_session_id = selected_session_id
                    pending_handoff_reason = reason
                    last_session_id = selected_session_id
                    continue
                except ProxyUnavailableError as error:
                    message = str(error)
                    last_unavailable_error = message
                    _mark_invite_proxy_error(selected_session_id, message)
                    add_log("error", "proxy", message, selected_session_id, job_id)
                    add_notification(
                        "error",
                        "Session proxy nedeniyle kullanılamıyor",
                        f"Session #{selected_session_id} atlandı. {message}",
                        "settings",
                    )
                    last_session_id = selected_session_id
                    continue
                except TargetGroupUnavailableError as error:
                    reason = str(error)
                    last_unavailable_error = reason
                    target_unavailable_session_ids.add(selected_session_id)
                    pending_handoff_from_session_id = selected_session_id
                    pending_handoff_reason = reason
                    add_log(
                        "warning",
                        "invite_target_access",
                        f"{reason} Sıradaki uygun session aranıyor.",
                        selected_session_id,
                        job_id,
                    )
                    add_notification(
                        "warning",
                        "Session hedef grup için atlandı",
                        f"{reason} Sıradaki uygun session aranıyor.",
                        "sessions",
                    )
                    last_session_id = selected_session_id
                    continue

                current_session_id = selected_session_id
                last_session_id = selected_session_id
                with get_connection() as connection:
                    connection.execute(
                        """
                        UPDATE transfer_jobs
                        SET session_id=?, status='running', resume_at=NULL,
                            last_error=NULL, updated_at=?
                        WHERE id=?
                        """,
                        (current_session_id, utc_now(), job_id),
                    )
                if pending_handoff_from_session_id is not None:
                    resume_message = (
                        f"{pending_handoff_reason or f'Session #{pending_handoff_from_session_id} kullanılamıyor'}. "
                        f"İş Session #{current_session_id} ile hemen devam ediyor."
                    )
                    add_log(
                        "success",
                        "invite_handoff",
                        resume_message,
                        current_session_id,
                        job_id,
                    )
                    add_notification(
                        "info",
                        "Davet session'ı değiştirildi",
                        resume_message,
                        "jobs",
                    )
                    pending_handoff_from_session_id = None
                    pending_handoff_reason = None
                add_log(
                    "info",
                    "invite",
                    "Üye ekleme için Telegram session bağlantısı hazır",
                    current_session_id,
                    job_id,
                )
                add_log(
                    "info",
                    "invite",
                    f"Hedef grup hazır; {len(candidates) - candidate_index} seçili üye sırada",
                    current_session_id,
                    job_id,
                )
                if context.source_input is not None:
                    add_log(
                        "info",
                        "invite",
                        "Mesaj bağlamlı kullanıcı referansları hazırlandı",
                        current_session_id,
                        job_id,
                    )

            if context.invite_count >= configured_quota:
                reason = f"Session #{current_session_id} günlük {configured_quota} başarılı davet sınırına ulaştı"
                message = f"{reason}; sıradaki uygun session aranıyor."
                add_log("warning", "quota", message, current_session_id, job_id)
                add_notification("warning", "Session günlük limite ulaştı", message, "sessions")
                pending_handoff_from_session_id = current_session_id
                pending_handoff_reason = reason
                await _close_invite_session(context)
                context = None
                continue

            add_log(
                "info",
                "invite_candidate",
                f"{candidate['telegram_user_id']}: Telegram kullanıcı referansı doğrulanıyor",
                current_session_id,
                job_id,
            )
            try:
                user = await _resolve_invite_candidate(context, candidate)
                if user is None:
                    status, reason, counter = (
                        "failed",
                        "Kullanıcı kaynak grupta yeniden çözümlenemedi",
                        "failed",
                    )
                else:
                    request = (
                        InviteToChannelRequest(context.target, [user])
                        if isinstance(context.target, Channel)
                        else AddChatUserRequest(context.target.id, user, fwd_limit=0)
                    )
                    await asyncio.wait_for(context.client(request), timeout=45)
                    status, reason, counter = (
                        "invited",
                        "Kullanıcı hedef gruba doğrudan eklendi",
                        "succeeded",
                    )
            except FloodWaitError as error:
                wait_until = datetime.now(UTC) + timedelta(seconds=error.seconds)
                reason = f"Session #{current_session_id} {error.seconds} saniye FloodWait aldı"
                message = f"{reason}; aday korunarak sıradaki uygun session aranıyor."
                _mark_invite_flood_wait(current_session_id, wait_until, message)
                add_log("warning", "flood_wait", message, current_session_id, job_id)
                add_notification("warning", "Session geçici olarak bekliyor", message, "sessions")
                pending_handoff_from_session_id = current_session_id
                pending_handoff_reason = reason
                await _close_invite_session(context)
                context = None
                continue
            except PeerFloodError:
                wait_until = datetime.now(UTC) + timedelta(hours=24)
                reason = (
                    f"Session #{current_session_id} Telegram spam korumasına takıldı "
                    "ve 24 saat dinlenmeye alındı"
                )
                message = f"{reason}; aday korunarak sıradaki uygun session aranıyor."
                _mark_invite_flood_wait(current_session_id, wait_until, message)
                add_log("warning", "peer_flood", message, current_session_id, job_id)
                add_notification("warning", "Telegram ekleme kısıtlaması", message, "sessions")
                pending_handoff_from_session_id = current_session_id
                pending_handoff_reason = reason
                await _close_invite_session(context)
                context = None
                continue
            except TimeoutError:
                status, reason, counter = (
                    "failed",
                    "Telegram üye ekleme isteği 45 saniye içinde yanıt vermedi",
                    "failed",
                )
            except UserAlreadyParticipantError:
                status, reason, counter = (
                    "existing",
                    "Kullanıcı hedef grupta zaten bulunuyor",
                    "skipped",
                )
            except UserPrivacyRestrictedError:
                status, reason, counter = (
                    "skipped",
                    "Kullanıcının gizlilik ayarları gruba doğrudan eklenmesine izin vermiyor",
                    "skipped",
                )
            except UserChannelsTooMuchError:
                status, reason, counter = (
                    "skipped",
                    "Kullanıcı Telegram grup/kanal sınırına ulaşmış",
                    "skipped",
                )
            except ChatAdminRequiredError:
                reason = (
                    f"Session #{current_session_id} hedef grupta üye ekleme yetkisini kaybetti"
                )
                target_unavailable_session_ids.add(current_session_id)
                pending_handoff_from_session_id = current_session_id
                pending_handoff_reason = reason
                last_unavailable_error = reason
                add_log(
                    "warning",
                    "invite_target_access",
                    f"{reason}; aday korunarak sıradaki uygun session aranıyor.",
                    current_session_id,
                    job_id,
                )
                add_notification(
                    "warning",
                    "Session hedef grup için atlandı",
                    f"{reason}; aday korunarak sıradaki uygun session aranıyor.",
                    "sessions",
                )
                await _close_invite_session(context)
                context = None
                continue
            except Exception as error:  # noqa: BLE001 - isolate one candidate and continue
                error_detail = str(error).strip()
                reason = f"Üye ekleme başarısız: {type(error).__name__}"
                if error_detail:
                    reason += f" — {error_detail}"
                status, counter = "failed", "failed"

            today = datetime.now(UTC).date().isoformat()
            now = utc_now()
            batch_wait_started = False
            with get_connection() as connection:
                connection.execute(
                    "UPDATE job_candidates SET status=?, reason=?, processed_at=? WHERE id=?",
                    (status, reason, now, candidate["id"]),
                )
                counter_query = {
                    "succeeded": "UPDATE transfer_jobs SET processed=processed+1, succeeded=succeeded+1, updated_at=? WHERE id=?",
                    "skipped": "UPDATE transfer_jobs SET processed=processed+1, skipped=skipped+1, updated_at=? WHERE id=?",
                    "failed": "UPDATE transfer_jobs SET processed=processed+1, failed=failed+1, updated_at=? WHERE id=?",
                }[counter]
                connection.execute(counter_query, (now, job_id))
                if status == "invited":
                    connection.execute(
                        """
                        INSERT INTO member_history(
                            telegram_user_id, display_name, username, first_job_id,
                            source_group_id, target_group_id, status, first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'invited', ?, ?)
                        ON CONFLICT(telegram_user_id) DO UPDATE SET
                            display_name=excluded.display_name, username=excluded.username,
                            target_group_id=excluded.target_group_id, status='invited',
                            last_seen_at=excluded.last_seen_at
                        """,
                        (
                            candidate["telegram_user_id"],
                            candidate["display_name"],
                            candidate["username"],
                            job_id,
                            job["source_id"],
                            job["target_id"],
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO session_invite_usage_daily(
                            session_id, usage_date, invite_count, last_used_at
                        ) VALUES (?, ?, 1, ?)
                        ON CONFLICT(session_id, usage_date) DO UPDATE SET
                            invite_count=invite_count+1, last_used_at=excluded.last_used_at
                        """,
                        (current_session_id, today, now),
                    )
                    connection.execute(
                        """
                        UPDATE telegram_sessions
                        SET batch_success_count=COALESCE(batch_success_count, 0)+1, updated_at=?
                        WHERE id=?
                        """,
                        (now, current_session_id),
                    )
                    context.invite_count += 1
                    context.batch_success_count += 1
                    if context.batch_success_count >= context.invite_batch_limit:
                        cooldown_until = datetime.now(UTC) + timedelta(
                            minutes=context.invite_cooldown_minutes
                        )
                        connection.execute(
                            """
                            UPDATE telegram_sessions
                            SET status='batch_wait', batch_success_count=0,
                                batch_cooldown_until=?, last_error=NULL, updated_at=?
                            WHERE id=?
                            """,
                            (cooldown_until.isoformat(), now, current_session_id),
                        )
                        context.batch_success_count = 0
                        batch_wait_started = True

            if status == "invited":
                add_log(
                    "success",
                    "invite_candidate",
                    f"{candidate['telegram_user_id']}: Kullanıcı hedef gruba doğrudan eklendi",
                    current_session_id,
                    job_id,
                )
            else:
                add_log(
                    "warning" if status != "failed" else "error",
                    "invite_candidate",
                    f"{candidate['telegram_user_id']}: {reason}",
                    current_session_id,
                    job_id,
                )

            candidate_index += 1
            remaining = len(candidates) - candidate_index
            handoff_message = None
            handoff_category = None
            if batch_wait_started and remaining:
                pending_handoff_reason = (
                    f"Session #{current_session_id} {context.invite_batch_limit} başarılı davetlik parti limitine ulaştı "
                    f"ve {context.invite_cooldown_minutes} dakika dinlenmeye alındı"
                )
                handoff_message = (
                    f"{pending_handoff_reason}. Kalan {remaining} aday korunarak sıradaki uygun session aranıyor."
                )
                handoff_category = "batch_wait"
                pending_handoff_from_session_id = current_session_id
            elif context.invite_count >= configured_quota and remaining:
                pending_handoff_reason = (
                    f"Session #{current_session_id} günlük {configured_quota} başarılı davet sınırına ulaştı"
                )
                handoff_message = (
                    f"{pending_handoff_reason}. Kalan {remaining} aday korunarak sıradaki uygun session aranıyor."
                )
                handoff_category = "quota"
                pending_handoff_from_session_id = current_session_id

            if handoff_message is not None and handoff_category is not None:
                add_log("info", handoff_category, handoff_message, current_session_id, job_id)
                add_notification("info", "Davet session'ı değiştiriliyor", handoff_message, "jobs")
                await _close_invite_session(context)
                context = None
                continue

            if remaining:
                minimum_delay = max(0, int(job.get("min_delay_seconds") or 0))
                maximum_delay = max(minimum_delay, int(job.get("max_delay_seconds") or 0))
                if maximum_delay > 0:
                    wait_seconds = secrets.SystemRandom().uniform(minimum_delay, maximum_delay)
                    add_log(
                        "info",
                        "invite_delay",
                        f"Sıradaki seçili adaydan önce {round(wait_seconds)} saniye bekleniyor",
                        current_session_id,
                        job_id,
                    )
                    await asyncio.sleep(wait_seconds)

        now = utc_now()
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE transfer_jobs
                SET status='completed', resume_at=NULL,
                    execution_finished_at=?, last_error=NULL, updated_at=?
                WHERE id=?
                """,
                (now, now, job_id),
            )
        add_log(
            "success",
            "invite",
            "Seçili üyeleri hedef gruba ekleme işlemi tamamlandı",
            current_session_id,
            job_id,
        )
        add_notification("success", "Üye ekleme işi tamamlandı", job["name"], "jobs")
    except Exception as error:  # noqa: BLE001 - job boundary records unexpected failures
        message = str(error)
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE transfer_jobs
                SET status='failed', last_error=?, execution_finished_at=?, updated_at=?
                WHERE id=?
                """,
                (message, utc_now(), utc_now(), job_id),
            )
        add_log("error", "invite", message, current_session_id, job_id)
        add_notification("error", "Üye ekleme işi durduruldu", message, "jobs")
    finally:
        await _close_invite_session(context)


async def scan_group_activity(scan: dict) -> dict:
    requested_session_id = scan["session_id"]
    session_ids = _activity_session_candidates(requested_session_id)
    if not session_ids:
        raise RuntimeError("Aktivite taraması için kullanılabilir Telegram session bulunamadı.")

    client = None
    lease = None
    selected_session_id = None
    entity = None
    access_errors: list[tuple[int, Exception]] = []
    for session_id in session_ids:
        candidate_client = None
        candidate_lease = None
        try:
            candidate_lease = await acquire_session_operation(
                session_id,
                "activity_scan",
                f"scan:{scan.get('id', 'pending')}",
                f"{scan.get('name') or 'Aktivite'} taraması",
            )
            candidate_client = await _client_for(session_id)
            candidate_entity = await _resolve_or_request_group_access(
                candidate_client,
                session_id,
                scan["group_ref"],
            )
            client = candidate_client
            lease = candidate_lease
            candidate_lease = None
            entity = candidate_entity
            selected_session_id = session_id
            if requested_session_id is None:
                set_app_setting("activity_round_robin_cursor", str(session_id))
            break
        except GroupJoinPending:
            if candidate_client is not None:
                await candidate_client.disconnect()
            raise
        except FloodWaitError:
            if candidate_client is not None:
                await candidate_client.disconnect()
            raise
        except Exception as error:  # noqa: BLE001 - automatic selection must try the next session
            if candidate_client is not None:
                await candidate_client.disconnect()
            access_errors.append((session_id, error))
            continue
        finally:
            if candidate_lease is not None:
                await candidate_lease.release()
    if client is None or entity is None or selected_session_id is None:
        access_error = _activity_access_error(requested_session_id, access_errors)
        raise access_error from (access_errors[-1][1] if access_errors else None)

    cutoff = datetime.now(UTC) - timedelta(hours=scan["window_hours"])
    authors: dict[int, dict] = {}
    message_count = 0
    try:
        _record_activity_operation(selected_session_id)
        async for message in client.iter_messages(entity, limit=50000):
            if not message.date:
                continue
            message_date = message.date.astimezone(UTC)
            if message_date < cutoff:
                break
            message_count += 1
            sender_id = message.sender_id
            if not sender_id or sender_id in authors:
                if sender_id in authors:
                    authors[sender_id]["message_count"] += 1
                    authors[sender_id]["last_message_at"] = max(authors[sender_id]["last_message_at"], message_date)
                continue
            sender = await message.get_sender()
            if not isinstance(sender, User) or getattr(sender, "bot", False) or getattr(sender, "deleted", False):
                continue
            display_name = " ".join(
                value for value in [sender.first_name, sender.last_name] if value
            ).strip() or "İsimsiz kullanıcı"
            authors[sender_id] = {
                "telegram_user_id": sender_id,
                "display_name": display_name,
                "username": sender.username,
                "access_hash": getattr(sender, "access_hash", None),
                "source_message_id": message.id,
                "message_count": 1,
                "last_message_at": message_date,
            }
        return {
            "session_id": selected_session_id,
            "group_id": entity.id,
            "group_title": getattr(entity, "title", "İsimsiz grup"),
            "message_count": message_count,
            "authors": list(authors.values()),
        }
    except FloodWaitError as error:
        until = datetime.now(UTC) + timedelta(seconds=error.seconds)
        with get_connection() as connection:
            connection.execute(
                "UPDATE telegram_sessions SET status='flood_wait', flood_wait_until=?, updated_at=? WHERE id=?",
                (until.isoformat(), utc_now(), selected_session_id),
            )
        add_log(
            "warning",
            "flood_wait",
            f"Aktivite taraması {error.seconds} saniye zorunlu beklemeye alındı",
            selected_session_id,
        )
        add_notification(
            "warning",
            "Aktivite taraması bekliyor",
            f"Telegram {error.seconds} saniye FloodWait uyguladı; aynı tarama başka session'a aktarılmadı.",
            "activity",
        )
        raise
    finally:
        try:
            await client.disconnect()
        finally:
            if lease is not None:
                await lease.release()
