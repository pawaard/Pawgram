from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import asyncio
import random
import re
from time import perf_counter

from telethon import TelegramClient
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
from telethon.tl.types import (
    Channel,
    ChannelParticipantsAdmins,
    Chat,
    ChatParticipantAdmin,
    ChatParticipantCreator,
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
    client = TelegramClient(
        StringSession(decrypt(record["session_encrypted"])),
        api_id,
        api_hash,
        proxy=_proxy_config(record),
        timeout=12,
        connection_retries=1,
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Telegram session geçersiz; hesabı yeniden bağlayın.")
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
    try:
        proxy_types = {"socks5": ProxyType.SOCKS5, "http": ProxyType.HTTP}
        started = perf_counter()
        proxy = Proxy.create(
            proxy_types[config["proxy_type"]],
            config["addr"],
            config["port"],
            username=config["username"],
            password=config["password"],
            rdns=True,
        )
        socket = await proxy.connect(
            dest_host="149.154.167.51",
            dest_port=443,
            timeout=12,
        )
        latency_ms = max(1, round((perf_counter() - started) * 1000))
        socket.close()
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE telegram_sessions
                SET proxy_last_status='success', proxy_latency_ms=?, proxy_last_error=NULL,
                    proxy_last_test_at=?, updated_at=?
                WHERE id=?
                """,
                (latency_ms, utc_now(), utc_now(), session_id),
            )
        add_log("success", "proxy", f"Proxy bağlantı testi başarılı: {latency_ms} ms", session_id)
        return {"ok": True, "status": "success", "latency_ms": latency_ms}
    except Exception as error:
        message = str(error) or error.__class__.__name__
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE telegram_sessions
                SET proxy_last_status='failed', proxy_latency_ms=NULL, proxy_last_error=?,
                    proxy_last_test_at=?, updated_at=?
                WHERE id=?
                """,
                (message[:500], utc_now(), utc_now(), session_id),
            )
        add_log("error", "proxy", f"Proxy bağlantı testi başarısız: {message}", session_id)
        raise RuntimeError(f"Proxy bağlantısı kurulamadı: {message}") from error


async def _resolve_entity(client: TelegramClient, reference: str):
    clean_reference: str | int = reference.strip()
    if isinstance(clean_reference, str) and clean_reference.lstrip("-").isdigit():
        clean_reference = int(clean_reference)
        # StringSession kalıcı entity önbelleği tutmaz. Dialogları almak ID çözümlemesini güvenilir hale getirir.
        await client.get_dialogs()
    return await client.get_entity(clean_reference)


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
            WHERE s.status = 'active'
               OR (s.status = 'flood_wait' AND s.flood_wait_until <= ?)
            ORDER BY s.id ASC
            """,
            (today, utc_now()),
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
    selected_id = next(session_id for session_id in circular_ids if session_id in available_ids)
    set_app_setting("activity_round_robin_cursor", str(selected_id))
    return [selected_id]


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
                        "id": entity.id,
                        "title": dialog.name,
                        "username": getattr(entity, "username", None),
                        "kind": "group" if dialog.is_group else "channel",
                        "unread_count": dialog.unread_count,
                    }
                )
        return groups
    finally:
        await client.disconnect()


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
        scan_limit = min(max(job["max_users"] * 5, 100), 5000)
        async for user in client.iter_participants(source, limit=scan_limit):
            if not isinstance(user, User):
                continue
            display_name = " ".join(
                value for value in [user.first_name, user.last_name] if value
            ).strip() or "İsimsiz kullanıcı"
            if getattr(user, "deleted", False):
                status, reason = "deleted", "Silinmiş Telegram hesabı"
            elif getattr(user, "bot", False):
                status, reason = "bot", "Bot hesabı"
            elif user.id in source_admin_ids:
                status, reason = "admin", "Kaynak grubun sahibi veya yöneticisi"
            elif user.id in target_member_ids:
                status, reason = "existing", "Gönderilecek grupta zaten bulunuyor"
            elif user.id in previously_used_ids:
                status, reason = "previously_used", "Daha önce Pawgram iş geçmişine alınmış"
            else:
                status, reason = "eligible", "Önizleme için uygun"
            counts[status] += 1
            rows.append(
                (
                    job_id,
                    user.id,
                    display_name,
                    user.username,
                    status,
                    reason,
                    utc_now(),
                )
            )
            if counts["eligible"] >= job["max_users"]:
                break

        now = utc_now()
        with get_connection() as connection:
            connection.execute("DELETE FROM job_candidates WHERE job_id = ?", (job_id,))
            connection.executemany(
                """
                INSERT INTO job_candidates(
                    job_id, telegram_user_id, display_name, username, status, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
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
            f"Aday önizlemesi tamamlandı: {counts['eligible']} uygun, {len(rows) - counts['eligible']} atlandı",
            session_id,
            job_id,
        )
        return {
            **counts,
            "scanned": len(rows),
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
    """Invite explicitly selected candidates. 
    
    If the current session reaches its daily quota, it automatically switches 
    to the next available active session and continues without stopping.
    """
    with get_connection() as connection:
        job = connection.execute("SELECT * FROM transfer_jobs WHERE id=?", (job_id,)).fetchone()
        candidates = connection.execute(
            """
            SELECT * FROM job_candidates
            WHERE job_id=? AND selected=1 AND status='eligible'
            ORDER BY id
            """,
            (job_id,),
        ).fetchall()
        if not job or job["status"] not in {"approved", "paused_quota"} or not candidates:
            return
        now = utc_now()
        connection.execute(
            """
            UPDATE transfer_jobs
            SET status='running', execution_started_at=COALESCE(execution_started_at, ?),
                last_error=NULL, updated_at=?
            WHERE id=?
            """,
            (now, now, job_id),
        )

    current_session_id = job["session_id"]
    client = await _client_for(current_session_id)
    
    try:
        source = await _resolve_entity(client, job["source_ref"])
        target = await _resolve_entity(client, job["target_ref"])
        if not isinstance(source, (Channel, Chat)) or not isinstance(target, (Channel, Chat)):
            raise RuntimeError("Kaynak ve hedef Telegram grubu olmalıdır.")
        rights = getattr(target, "admin_rights", None)
        if not (getattr(target, "creator", False) or (rights and getattr(rights, "invite_users", False))):
            raise ChatAdminRequiredError(request=None)

        selected_ids = {row["telegram_user_id"] for row in candidates}
        users: dict[int, User] = {}
        async for user in client.iter_participants(source, limit=5000):
            if isinstance(user, User) and user.id in selected_ids:
                users[user.id] = user
                if len(users) == len(selected_ids):
                    break

        configured_quota = max(
            1,
            min(
                int(job["daily_limit"]),
                int(get_app_setting("activity_daily_quota") or DEFAULT_DAILY_ACTIVITY_QUOTA),
            ),
        )

        for candidate_index, candidate in enumerate(candidates):
            today = datetime.now(UTC).date().isoformat()
            
            # Mevcut session'ın bugünkü kullanımını kontrol et
            with get_connection() as connection:
                usage = connection.execute(
                    "SELECT operation_count FROM session_usage_daily WHERE session_id=? AND usage_date=?",
                    (current_session_id, today),
                ).fetchone()
            used = int(usage["operation_count"]) if usage else 0

            # KOTA KONTROLÜ VE OTOMATİK SESSION DEĞİŞİMİ
            if used >= configured_quota:
                logger_msg = f"Session #{current_session_id} için günlük {configured_quota} kota doldu. Sıradaki session aranıyor..."
                add_log("info", "quota_switch", logger_msg, current_session_id, job_id)

                # Kotası dolmamış, aktif başka bir session bul
                with get_connection() as connection:
                    next_session = connection.execute(
                        """
                        SELECT s.id 
                        FROM telegram_sessions s
                        LEFT JOIN session_usage_daily u 
                            ON u.session_id = s.id AND u.usage_date = ?
                        WHERE s.status = 'active' 
                          AND s.id != ?
                          AND COALESCE(u.operation_count, 0) < ?
                        ORDER BY s.id ASC
                        LIMIT 1
                        """,
                        (today, current_session_id, configured_quota),
                    ).fetchone()

                if next_session:
                    new_session_id = next_session["id"]
                    add_log(
                        "success", 
                        "quota_switch", 
                        f"Kota dolduğu için Session #{current_session_id} -> Session #{new_session_id} geçişi yapıldı.", 
                        new_session_id, 
                        job_id
                    )
                    
                    # Eski client bağlantısını kapat ve yenisine bağlan
                    await client.disconnect()
                    current_session_id = new_session_id
                    client = await _client_for(current_session_id)
                    
                    # Hedef & kaynak varlıklarını yeni session ile tekrar doğrula
                    source = await _resolve_entity(client, job["source_ref"])
                    target = await _resolve_entity(client, job["target_ref"])
                    
                    # Yeni session verisiyle güncellenmiş kullanımı çek
                    with get_connection() as connection:
                        usage = connection.execute(
                            "SELECT operation_count FROM session_usage_daily WHERE session_id=? AND usage_date=?",
                            (current_session_id, today),
                        ).fetchone()
                    used = int(usage["operation_count"]) if usage else 0
                else:
                    # Başka kullanılabilir session kalmadıysa işi duraklat
                    message = f"Tüm aktif session'ların günlük {configured_quota} işlem kotası doldu. İş duraklatıldı."
                    with get_connection() as connection:
                        connection.execute(
                            "UPDATE transfer_jobs SET status='paused_quota', last_error=?, updated_at=? WHERE id=?",
                            (message, utc_now(), job_id),
                        )
                    add_log("warning", "quota", message, current_session_id, job_id)
                    add_notification("warning", "Davet işi duraklatıldı", message, "jobs")
                    return

            user = users.get(candidate["telegram_user_id"])
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
                continue

            try:
                await client(InviteToChannelRequest(target, [user]))
            except UserAlreadyParticipantError:
                status, reason, counter = "existing", "Kullanıcı hedef grupta zaten bulunuyor", "skipped"
            except (UserPrivacyRestrictedError, UserChannelsTooMuchError) as error:
                message = f"Telegram kritik kullanıcı kısıtı bildirdi: {type(error).__name__}; iş durduruldu."
                with get_connection() as connection:
                    connection.execute(
                        "UPDATE transfer_jobs SET status='failed', last_error=?, updated_at=? WHERE id=?",
                        (message, utc_now(), job_id),
                    )
                    connection.execute(
                        "UPDATE job_candidates SET reason=? WHERE id=?",
                        (message, candidate["id"]),
                    )
                add_log("error", "invite", message, current_session_id, job_id)
                add_notification("error", "Davet işi durduruldu", message, "jobs")
                return
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
                add_notification("warning", "Davet işi bekletildi", message, "jobs")
                return
            except (PeerFloodError, ChatAdminRequiredError) as error:
                raise RuntimeError(f"Telegram davet işlemini durdurdu: {type(error).__name__}") from error
            except Exception as error:
                status, reason, counter = "failed", f"Davet başarısız: {type(error).__name__}", "failed"
            else:
                status, reason, counter = "invited", "Telegram daveti gönderildi", "succeeded"

            now = utc_now()
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
                connection.execute(
                    """
                    INSERT INTO session_usage_daily(session_id, usage_date, operation_count, last_used_at)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(session_id, usage_date) DO UPDATE SET
                        operation_count=operation_count+1, last_used_at=excluded.last_used_at
                    """,
                    (current_session_id, today, now),
                )
            if status in {"existing", "skipped", "failed"}:
                add_log(
                    "warning" if status != "failed" else "error",
                    "invite_candidate",
                    f"{candidate['telegram_user_id']}: {reason}",
                    current_session_id,
                    job_id,
                )
            
            if candidate_index < len(candidates) - 1:
                await asyncio.sleep(random.uniform(job["min_delay_seconds"], job["max_delay_seconds"]))

        now = utc_now()
        with get_connection() as connection:
            connection.execute(
                "UPDATE transfer_jobs SET status='completed', execution_finished_at=?, updated_at=? WHERE id=?",
                (now, now, job_id),
            )
        add_log("success", "invite", "Seçili üyelerin davet işlemi tamamlandı", current_session_id, job_id)
        add_notification("success", "Davet işi tamamlandı", job["name"], "jobs")
    except Exception as error:
        message = str(error)
        with get_connection() as connection:
            connection.execute(
                "UPDATE transfer_jobs SET status='failed', last_error=?, execution_finished_at=?, updated_at=? WHERE id=?",
                (message, utc_now(), utc_now(), job_id),
            )
        add_log("error", "invite", message, current_session_id, job_id)
        add_notification("error", "Davet işi durduruldu", message, "jobs")
    finally:
        await client.disconnect()


async def scan_group_activity(scan: dict) -> dict:
    requested_session_id = scan["session_id"]
    session_ids = _activity_session_candidates(requested_session_id)
    if not session_ids:
        raise RuntimeError("Aktivite taraması için kullanılabilir Telegram session bulunamadı.")

    client = None
    selected_session_id = None
    entity = None
    last_access_error: Exception | None = None
    for session_id in session_ids:
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
            break
        except GroupJoinPending:
            if candidate_client is not None:
                await candidate_client.disconnect()
            raise
        except FloodWaitError:
            raise
        except Exception as error:
            last_access_error = error
            continue
    if client is None or entity is None or selected_session_id is None:
        raise RuntimeError(
            "Hiçbir aktif session seçilen gruba erişemedi. Özel grup için t.me/+... davet bağlantısını kullanın."
        ) from last_access_error

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
