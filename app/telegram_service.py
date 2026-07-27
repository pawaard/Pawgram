from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import asyncio
import re
from time import perf_counter

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
from telethon.tl.functions.channels import GetParticipantRequest, InviteToChannelRequest, JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, GetFullChatRequest, ImportChatInviteRequest
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
from python_socks import ProxyType
from python_socks.async_.asyncio import Proxy

from app.config import get_settings
from app.database import add_log, add_notification, get_app_setting, get_connection, set_app_setting, utc_now
from app.security import decrypt, encrypt, mask_phone, phone_key


DEFAULT_DAILY_ACTIVITY_QUOTA = 30


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


@dataclass
class ResolvedGroup:
    id: int
    title: str
    username: str | None
    kind: str
    participants_count: int | None
    creator: bool
    admin_rights: bool


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


async def start_login(phone: str, label: str) -> dict:
    api_id, api_hash = _credentials()
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    try:
        result = await client.send_code_request(phone)
        session_string = client.session.save()
        now = utc_now()
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO pending_auth(phone_hash, phone_encrypted, label, session_encrypted,
                                         code_hash_encrypted, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(phone_hash) DO UPDATE SET
                    phone_encrypted=excluded.phone_encrypted,
                    label=excluded.label,
                    session_encrypted=excluded.session_encrypted,
                    code_hash_encrypted=excluded.code_hash_encrypted,
                    created_at=excluded.created_at
                """,
                (
                    phone_key(phone),
                    encrypt(phone),
                    label,
                    encrypt(session_string),
                    encrypt(result.phone_code_hash),
                    now,
                ),
            )
        add_log("info", "session", f"{mask_phone(phone)} için doğrulama kodu istendi")
        return {"ok": True, "phone_masked": mask_phone(phone), "message": "Doğrulama kodu gönderildi."}
    except FloodWaitError as error:
        raise RuntimeError(f"Telegram {error.seconds} saniye bekleme istedi.") from error
    finally:
        await client.disconnect()


async def verify_login(phone: str, code: str, password: str | None) -> dict:
    api_id, api_hash = _credentials()
    with get_connection() as connection:
        pending = connection.execute(
            "SELECT * FROM pending_auth WHERE phone_hash = ?", (phone_key(phone),)
        ).fetchone()
    if not pending:
        raise RuntimeError("Bekleyen doğrulama isteği bulunamadı. Yeniden kod isteyin.")

    client = TelegramClient(StringSession(decrypt(pending["session_encrypted"])), api_id, api_hash)
    await client.connect()
    try:
        try:
            await client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=decrypt(pending["code_hash_encrypted"]),
            )
        except SessionPasswordNeededError:
            if not password:
                return {"ok": False, "password_required": True, "message": "İki aşamalı doğrulama parolası gerekli."}
            await client.sign_in(password=password)

        me = await client.get_me()
        session_string = client.session.save()
        display_name = " ".join(part for part in [me.first_name, me.last_name] if part).strip() or "Telegram hesabı"
        now = utc_now()
        with get_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted, telegram_user_id,
                    display_name, username, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    pending["label"],
                    mask_phone(phone),
                    encrypt(phone),
                    encrypt(session_string),
                    me.id,
                    display_name,
                    me.username,
                    now,
                    now,
                ),
            )
            session_id = cursor.lastrowid
            connection.execute("DELETE FROM pending_auth WHERE phone_hash = ?", (phone_key(phone),))
        add_log("success", "session", f"{mask_phone(phone)} başarıyla bağlandı", session_id=session_id)
        add_notification("success", "Telegram hesabı bağlandı", f"{display_name} kullanıma hazır.", "sessions")
        return {"ok": True, "session_id": session_id, "display_name": display_name}
    finally:
        await client.disconnect()


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


async def _client_for(session_id: int) -> TelegramClient:
    api_id, api_hash = _credentials()
    record = get_session_record(session_id)
    if not record.get("proxy_enabled"):
        message = (
            "Bu Telegram session için sabit proxy atanmamış. Ayarlar > Session proxy "
            "yönetiminden proxy tanımlayın veya Toplu Proxy Ekle ile boş hesaba proxy dağıtın."
        )
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
        await test_session_proxy(session_id)
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
    return {
        "proxy_type": record.get("proxy_type") or "socks5",
        "addr": record["proxy_host"],
        "port": int(record["proxy_port"]),
        "rdns": True,
        "username": username,
        "password": password,
    }


async def test_session_proxy(session_id: int) -> dict:
    record = get_session_record(session_id)
    config = _proxy_config(record)
    if not config:
        raise RuntimeError("Bu session için proxy etkin değil.")
    selected_type = config["proxy_type"]
    candidate_types = [selected_type] + [item for item in ("socks5", "http") if item != selected_type]
    attempt_errors: list[str] = []
    try:
        proxy_types = {"socks5": ProxyType.SOCKS5, "http": ProxyType.HTTP}
        socket = None
        detected_type = selected_type
        latency_ms = 0
        for proxy_type in candidate_types:
            started = perf_counter()
            try:
                proxy = Proxy.create(
                    proxy_types[proxy_type],
                    config["addr"],
                    config["port"],
                    username=config["username"],
                    password=config["password"],
                    rdns=True,
                )
                async with asyncio.timeout(8):
                    socket = await proxy.connect(
                        dest_host="149.154.167.51",
                        dest_port=443,
                        timeout=7,
                    )
                latency_ms = max(1, round((perf_counter() - started) * 1000))
                detected_type = proxy_type
                break
            except Exception as attempt_error:
                attempt_errors.append(
                    f"{proxy_type.upper()}: {str(attempt_error) or attempt_error.__class__.__name__}"
                )
        if socket is None:
            raise RuntimeError(" | ".join(attempt_errors))
        socket.close()
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
        raise RuntimeError("Girilen referans Telegram grubu değil.")
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
            SELECT s.id, COALESCE(u.operation_count, 0) operation_count, u.last_used_at
            FROM telegram_sessions s
            LEFT JOIN session_usage_daily u
              ON u.session_id = s.id AND u.usage_date = ?
            WHERE s.status IN ('active', 'proxy_pending')
               OR (s.status = 'flood_wait' AND s.flood_wait_until <= ?)
               OR (s.status = 'batch_wait' AND s.batch_cooldown_until <= ?)
            ORDER BY s.id ASC
            """,
            (today, utc_now(), utc_now()),
        ).fetchall()
    if requested_session_id:
        requested = next((row for row in sessions if row["id"] == requested_session_id), None)
        if requested and requested["operation_count"] < quota:
            return [requested_session_id]
        if requested:
            tomorrow = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            raise SessionBudgetWaiting(tomorrow)
        return []

    available = [row for row in sessions if row["operation_count"] < quota]
    if not available and sessions:
        tomorrow = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        raise SessionBudgetWaiting(tomorrow)
    if not available:
        return []

    cursor_value = get_app_setting("activity_round_robin_cursor")
    cursor_id = int(cursor_value) if cursor_value and cursor_value.isdigit() else None
    all_ids = [row["id"] for row in sessions]
    start_index = 0
    if cursor_id in all_ids:
        start_index = (all_ids.index(cursor_id) + 1) % len(all_ids)
    circular_ids = all_ids[start_index:] + all_ids[:start_index]
    available_ids = {row["id"] for row in available}
    return [session_id for session_id in circular_ids if session_id in available_ids]


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
    client = await _client_for(session_id)
    try:
        entity = await _resolve_entity(client, reference)
        if not isinstance(entity, (Channel, Chat)):
            raise RuntimeError("Girilen referans bir Telegram grubu veya kanalı değil.")
        result = ResolvedGroup(
            id=entity.id,
            title=getattr(entity, "title", "İsimsiz grup"),
            username=getattr(entity, "username", None),
            kind="megagroup" if getattr(entity, "megagroup", False) else "channel" if isinstance(entity, Channel) else "group",
            participants_count=getattr(entity, "participants_count", None),
            creator=bool(getattr(entity, "creator", False)),
            admin_rights=bool(getattr(entity, "admin_rights", None)),
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
        await client.disconnect()


async def list_groups(session_id: int) -> list[dict]:
    client = await _client_for(session_id)
    groups: list[dict] = []
    try:
        async for dialog in client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                entity = dialog.entity
                groups.append(
                    {
                        "id": utils.get_peer_id(entity),
                        "title": dialog.name,
                        "username": getattr(entity, "username", None),
                        "kind": "group" if dialog.is_group else "channel",
                        "unread_count": dialog.unread_count,
                    }
                )
        return groups
    finally:
        await client.disconnect()


async def _iter_transfer_source_users(
    client: TelegramClient,
    source: Channel | Chat,
    participant_limit: int = 5000,
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
    client = await _client_for(session_id)
    try:
        source = await _resolve_entity(client, job["source_ref"])
        target = await _resolve_entity(client, job["target_ref"])
        if not isinstance(source, (Channel, Chat)) or not isinstance(target, (Channel, Chat)):
            raise RuntimeError("Çekilecek ve gönderilecek alanlar Telegram grubu olmalı.")

        admin_rights = getattr(target, "admin_rights", None)
        can_invite = bool(
            getattr(target, "creator", False)
            or (admin_rights and getattr(admin_rights, "invite_users", False))
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
            participant_limit = min(max(job["max_users"] * 5, 100), 5000)
            async for user, source_message_id in _iter_transfer_source_users(
                client,
                source,
                participant_limit=participant_limit,
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
        await client.disconnect()


async def execute_invite_job(job_id: int) -> None:
    """Add explicitly selected candidates with one fixed, fail-closed session."""
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
        if not job or job["status"] not in {"approved", "paused_quota", "paused_batch", "proxy_error", "flood_wait", "queued_execution"} or not candidates:
            return
        now = utc_now()
        claimed = connection.execute(
            """
            UPDATE transfer_jobs
            SET status='running', execution_started_at=COALESCE(execution_started_at, ?),
                last_error=NULL, updated_at=?
            WHERE id=? AND status IN ('approved', 'paused_quota', 'paused_batch', 'proxy_error', 'flood_wait', 'queued_execution')
            """,
            (now, now, job_id),
        )
        if claimed.rowcount != 1:
            return

    current_session_id = job["session_id"]
    with get_connection() as connection:
        current_session = connection.execute(
            "SELECT status, flood_wait_until, batch_cooldown_until FROM telegram_sessions WHERE id=?",
            (current_session_id,),
        ).fetchone()
        if current_session and current_session["status"] == "batch_wait":
            cooldown_until = current_session.get("batch_cooldown_until")
            if cooldown_until and cooldown_until > utc_now():
                message = (
                    "Bu session 3 başarılı eklemeden sonra 30 dakikalık parti beklemesinde. "
                    f"Yeniden çalışma zamanı: {cooldown_until}"
                )
                connection.execute(
                    "UPDATE transfer_jobs SET status='paused_batch', last_error=?, updated_at=? WHERE id=?",
                    (message, utc_now(), job_id),
                )
                return
            connection.execute(
                """
                UPDATE telegram_sessions
                SET status='active', batch_success_count=0, batch_cooldown_until=NULL, updated_at=?
                WHERE id=?
                """,
                (utc_now(), current_session_id),
            )
        if current_session and current_session["status"] == "flood_wait":
            wait_until = current_session.get("flood_wait_until")
            if wait_until and wait_until <= utc_now():
                connection.execute(
                    "UPDATE telegram_sessions SET status='active', flood_wait_until=NULL, updated_at=? WHERE id=?",
                    (utc_now(), current_session_id),
                )
            else:
                message = (
                    "Mevcut Telegram session spam koruması nedeniyle 24 saat dinleniyor. "
                    "Kısıtlamayı başka hesapla otomatik aşma yapılmaz."
                )
                connection.execute(
                    "UPDATE transfer_jobs SET status='flood_wait', last_error=?, updated_at=? WHERE id=?",
                    (message, utc_now(), job_id),
                )
                return
    client = None

    try:
        try:
            client = await asyncio.wait_for(_client_for(current_session_id), timeout=30)
        except TimeoutError as error:
            raise ProxyUnavailableError(
                "Proxy üzerinden Telegram session bağlantısı 30 saniye içinde kurulamadı; "
                "işlem durduruldu ve ana IP kullanılmadı. Proxy yanıtını, Telegram erişimini ve IP yetkilendirmesini kontrol edin."
            ) from error
        add_log("info", "invite", "Üye ekleme için Telegram session bağlantısı hazır", current_session_id, job_id)

        try:
            target = await asyncio.wait_for(
                _resolve_entity(client, job["target_ref"]),
                timeout=30,
            )
        except TimeoutError as error:
            raise RuntimeError("Hedef grup 30 saniye içinde doğrulanamadı.") from error
        if not isinstance(target, (Channel, Chat)):
            raise RuntimeError("Hedef Telegram grubu olmalıdır.")
        rights = getattr(target, "admin_rights", None)
        if not (getattr(target, "creator", False) or (rights and getattr(rights, "invite_users", False))):
            raise ChatAdminRequiredError(request=None)
        add_log(
            "info",
            "invite",
            f"Hedef grup hazır; {len(candidates)} seçili üye doğrudan ekleme sırasına alındı",
            current_session_id,
            job_id,
        )

        source_input = None
        if any(candidate.get("source_message_id") for candidate in candidates):
            try:
                source = await asyncio.wait_for(
                    _resolve_entity(client, job["source_ref"]),
                    timeout=30,
                )
                source_input = await asyncio.wait_for(
                    client.get_input_entity(source),
                    timeout=15,
                )
            except TimeoutError as error:
                raise RuntimeError("Kaynak mesaj referansı 30 saniye içinde hazırlanamadı.") from error
            add_log(
                "info",
                "invite",
                "Mesaj bağlamlı kullanıcı referansları hazırlandı",
                current_session_id,
                job_id,
            )

        # Aktivite taramaları ile üye ekleme denemeleri farklı güvenlik sayaçlarıdır.
        # Tarama kotası dolmuş olsa bile henüz üye eklememiş bir session'ın
        # aktarım işi engellenmemelidir.
        configured_quota = max(1, int(job["daily_limit"]))

        for candidate in candidates:
            today = datetime.now(UTC).date().isoformat()

            # Günlük sınır aynı hesabı durdurur. Telegram kısıtlarını aşmak için
            # başka bir hesaba otomatik geçiş yapılmaz.
            with get_connection() as connection:
                usage = connection.execute(
                    "SELECT invite_count FROM session_invite_usage_daily WHERE session_id=? AND usage_date=?",
                    (current_session_id, today),
                ).fetchone()
            used = int(usage["invite_count"]) if usage else 0

            if used >= configured_quota:
                message = (
                    f"Session #{current_session_id} için günlük {configured_quota} başarılı üye ekleme sınırı doldu. "
                    "Kalan adaylar korunarak iş duraklatıldı; sınır yenilendiğinde aynı session ile devam edin."
                )
                with get_connection() as connection:
                    connection.execute(
                        "UPDATE transfer_jobs SET status='paused_quota', last_error=?, updated_at=? WHERE id=?",
                        (message, utc_now(), job_id),
                    )
                add_log("warning", "quota", message, current_session_id, job_id)
                add_notification("warning", "Üye ekleme işi duraklatıldı", message, "jobs")
                return

            # Önizlemede kaydedilen access_hash ile doğrudan Telegram'ın
            # "Gruba katılımcı ekle" isteğine geçilir. Kaynak grubun binlerce
            # mesajını burada yeniden taramak hem gereksizdir hem de işi 0/N'de
            # bekletir.
            user: User | InputUser | None = None
            source_message_id = candidate.get("source_message_id")
            access_hash = candidate.get("access_hash")
            add_log(
                "info",
                "invite_candidate",
                f"{candidate['telegram_user_id']}: Telegram kullanıcı referansı doğrulanıyor",
                current_session_id,
                job_id,
            )
            if source_message_id and source_input is not None:
                contextual_user = InputUserFromMessage(
                    source_input,
                    int(source_message_id),
                    candidate["telegram_user_id"],
                )
                try:
                    resolved_users = await asyncio.wait_for(
                        client(GetUsersRequest([contextual_user])),
                        timeout=30,
                    )
                except Exception:
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
                        client(GetUsersRequest([stored_user])),
                        timeout=30,
                    )
                except Exception:
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
                        client.get_entity(candidate["username"]),
                        timeout=20,
                    )
                except Exception:
                    resolved_user = None
                if isinstance(resolved_user, User):
                    user = resolved_user
            elif user is None and access_hash is None:
                try:
                    resolved_user = await asyncio.wait_for(
                        client.get_entity(candidate["telegram_user_id"]),
                        timeout=20,
                    )
                except Exception:
                    resolved_user = None
                if isinstance(resolved_user, User):
                    user = resolved_user

            if not user:
                with get_connection() as connection:
                    connection.execute(
                        "UPDATE job_candidates SET status='failed', reason=?, processed_at=? WHERE id=?",
                        ("Kullanıcı kaynak grupta yeniden çözümlenemedi", utc_now(), candidate["id"]),
                    )
                    connection.execute(
                        "UPDATE transfer_jobs SET processed=processed+1, failed=failed+1, updated_at=? WHERE id=?",
                        (utc_now(), job_id),
                    )
                add_log(
                    "error",
                    "invite_candidate",
                    f"{candidate['telegram_user_id']}: Geçerli Telegram kullanıcı referansı bulunamadı; aday eski önizlemeden gelmiş olabilir",
                    current_session_id,
                    job_id,
                )
                continue

            try:
                await asyncio.wait_for(
                    client(InviteToChannelRequest(target, [user])),
                    timeout=45,
                )
            except TimeoutError:
                status, reason, counter = (
                    "failed",
                    "Telegram üye ekleme isteği 45 saniye içinde yanıt vermedi",
                    "failed",
                )
            except UserAlreadyParticipantError:
                status, reason, counter = "existing", "Kullanıcı hedef grupta zaten bulunuyor", "skipped"
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
            except FloodWaitError as error:
                until = datetime.now(UTC) + timedelta(seconds=error.seconds)
                message = f"Telegram {error.seconds} saniye FloodWait uyguladı; iş aynı session ile zorunlu beklemeye alındı."
                with get_connection() as connection:
                    connection.execute(
                        "UPDATE telegram_sessions SET status='flood_wait', flood_wait_until=?, updated_at=? WHERE id=?",
                        (until.isoformat(), utc_now(), current_session_id),
                    )
                    connection.execute(
                        "UPDATE transfer_jobs SET status='flood_wait', last_error=?, updated_at=? WHERE id=?",
                        (message, utc_now(), job_id),
                    )
                add_log("warning", "flood_wait", message, current_session_id, job_id)
                add_notification("warning", "Üye ekleme işi bekletildi", message, "jobs")
                return
            except PeerFloodError:
                until = datetime.now(UTC) + timedelta(hours=24)
                message = (
                    f"Session #{current_session_id} Telegram spam korumasına takıldı ve 24 saat dinlenmeye alındı. "
                    "Kalan adaylar korunmuştur. Kısıtlamayı başka hesapla otomatik aşma yapılmaz; süre dolunca aynı işi yeniden başlatın."
                )
                with get_connection() as connection:
                    connection.execute(
                        """
                        UPDATE telegram_sessions
                        SET status='flood_wait', flood_wait_until=?, last_error=?, updated_at=?
                        WHERE id=?
                        """,
                        (until.isoformat(), message, utc_now(), current_session_id),
                    )
                    connection.execute(
                        "UPDATE transfer_jobs SET status='flood_wait', last_error=?, updated_at=? WHERE id=?",
                        (message, utc_now(), job_id),
                    )
                add_log("warning", "peer_flood", message, current_session_id, job_id)
                add_notification("warning", "Telegram ekleme kısıtlaması", message, "jobs")
                return
            except ChatAdminRequiredError as error:
                raise RuntimeError(f"Telegram üye ekleme işlemini durdurdu: {type(error).__name__}") from error
            except Exception as error:
                error_detail = str(error).strip()
                reason = f"Üye ekleme başarısız: {type(error).__name__}"
                if error_detail:
                    reason += f" — {error_detail}"
                status, counter = "failed", "failed"
            else:
                status, reason, counter = "invited", "Kullanıcı hedef gruba doğrudan eklendi", "succeeded"

            now = utc_now()
            pause_for_batch = False
            batch_message = ""
            with get_connection() as connection:
                connection.execute(
                    "UPDATE job_candidates SET status=?, reason=?, processed_at=? WHERE id=?",
                    (status, reason, now, candidate["id"]),
                )
                connection.execute(
                    f"UPDATE transfer_jobs SET processed=processed+1, {counter}={counter}+1, updated_at=? WHERE id=?",
                    (now, job_id),
                )
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
                            candidate["telegram_user_id"], candidate["display_name"], candidate["username"],
                            job_id, job["source_id"], job["target_id"], now, now,
                        ),
                    )
                    # Uygulama kotası yalnızca gerçekten hedef gruba eklenen
                    # kullanıcıları sayar. Gizlilik, geçersiz kullanıcı ve
                    # zaten üye gibi başarısız/atlanan sonuçlar kotayı tüketmez.
                    connection.execute(
                        """
                        INSERT INTO session_invite_usage_daily(session_id, usage_date, invite_count, last_used_at)
                        VALUES (?, ?, 1, ?)
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
                    batch_state = connection.execute(
                        "SELECT batch_success_count FROM telegram_sessions WHERE id=?",
                        (current_session_id,),
                    ).fetchone()
                    if int(batch_state["batch_success_count"] or 0) >= 3:
                        cooldown_until = datetime.now(UTC) + timedelta(minutes=30)
                        connection.execute(
                            """
                            UPDATE telegram_sessions
                            SET status='batch_wait', batch_success_count=0,
                                batch_cooldown_until=?, last_error=NULL, updated_at=?
                            WHERE id=?
                            """,
                            (cooldown_until.isoformat(), now, current_session_id),
                        )
                        remaining = connection.execute(
                            """
                            SELECT COUNT(*) count FROM job_candidates
                            WHERE job_id=? AND selected=1 AND status='eligible'
                            """,
                            (job_id,),
                        ).fetchone()["count"]
                        if remaining:
                            batch_message = (
                                f"Session #{current_session_id} bu partide 3 kullanıcı ekledi. "
                                f"Kalan {remaining} aday korunarak 30 dakika beklemeye alındı; "
                                f"yeniden çalışma zamanı: {cooldown_until.isoformat()}"
                            )
                            connection.execute(
                                "UPDATE transfer_jobs SET status='paused_batch', last_error=?, updated_at=? WHERE id=?",
                                (batch_message, now, job_id),
                            )
                            pause_for_batch = True
            if status == "invited":
                add_log(
                    "success",
                    "invite_candidate",
                    f"{candidate['telegram_user_id']}: Kullanıcı hedef gruba doğrudan eklendi",
                    current_session_id,
                    job_id,
                )
            if status in {"existing", "skipped", "failed"}:
                add_log(
                    "warning" if status != "failed" else "error",
                    "invite_candidate",
                    f"{candidate['telegram_user_id']}: {reason}",
                    current_session_id,
                    job_id,
                )
            if pause_for_batch:
                add_log("info", "batch_wait", batch_message, current_session_id, job_id)
                add_notification("info", "Parti beklemesi başladı", batch_message, "jobs")
                return
            
        now = utc_now()
        with get_connection() as connection:
            connection.execute(
                "UPDATE transfer_jobs SET status='completed', execution_finished_at=?, updated_at=? WHERE id=?",
                (now, now, job_id),
            )
        add_log("success", "invite", "Seçili üyeleri hedef gruba ekleme işlemi tamamlandı", current_session_id, job_id)
        add_notification("success", "Üye ekleme işi tamamlandı", job["name"], "jobs")
    except ProxyUnavailableError as error:
        message = str(error)
        with get_connection() as connection:
            connection.execute(
                "UPDATE transfer_jobs SET status='proxy_error', last_error=?, updated_at=? WHERE id=?",
                (message, utc_now(), job_id),
            )
        add_log("error", "proxy", message, current_session_id, job_id)
        add_notification(
            "error",
            "Üye ekleme proxy nedeniyle durduruldu",
            f"Adaylar korunmuştur. {message}",
            "settings",
        )
    except Exception as error:
        message = str(error)
        with get_connection() as connection:
            connection.execute(
                "UPDATE transfer_jobs SET status='failed', last_error=?, execution_finished_at=?, updated_at=? WHERE id=?",
                (message, utc_now(), utc_now(), job_id),
            )
        add_log("error", "invite", message, current_session_id, job_id)
        add_notification("error", "Üye ekleme işi durduruldu", message, "jobs")
    finally:
        if client is not None:
            await client.disconnect()


async def scan_group_activity(scan: dict) -> dict:
    requested_session_id = scan["session_id"]
    session_ids = _activity_session_candidates(requested_session_id)
    if not session_ids:
        raise RuntimeError("Aktivite taraması için kullanılabilir Telegram session bulunamadı.")

    client = None
    selected_session_id = None
    entity = None
    access_errors: list[tuple[int, Exception]] = []
    for session_id in session_ids:
        candidate_client = None
        try:
            candidate_client = await _client_for(session_id)
            candidate_entity = await _resolve_or_request_group_access(
                candidate_client,
                session_id,
                scan["group_ref"],
            )
            client = candidate_client
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
        except Exception as error:
            if candidate_client is not None:
                await candidate_client.disconnect()
            access_errors.append((session_id, error))
            continue
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
                    if message_date > authors[sender_id]["last_message_at"]:
                        authors[sender_id]["last_message_at"] = message_date
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
        await client.disconnect()
