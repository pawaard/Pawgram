import asyncio
from datetime import UTC, datetime, timedelta

from telethon.errors import FloodWaitError, UserNotParticipantError
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import Channel

from app.database import add_log, add_notification, get_connection, utc_now
from app.session_operation import (
    SessionOperationBusy,
    acquire_session_operation,
    get_session_operation,
)
from app.telegram_service import (
    ProxyUnavailableError,
    _client_for,
    _private_invite_hash,
    _resolve_entity,
)

TERMINAL_HEALTH_STATUSES = ("ready", "attention", "failed", "busy", "waiting")


def _refresh_health_counts(batch_id: int) -> None:
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT status, COUNT(*) count FROM session_health_items WHERE batch_id=? GROUP BY status",
            (batch_id,),
        ).fetchall()
        counts = {row["status"]: int(row["count"]) for row in rows}
        processed = sum(counts.get(status, 0) for status in TERMINAL_HEALTH_STATUSES)
        warnings = (
            counts.get("attention", 0)
            + counts.get("busy", 0)
            + counts.get("waiting", 0)
        )
        connection.execute(
            """
            UPDATE session_health_batches
            SET processed_count=?, ready_count=?, warning_count=?, failed_count=?, updated_at=?
            WHERE id=?
            """,
            (
                processed,
                counts.get("ready", 0),
                warnings,
                counts.get("failed", 0),
                utc_now(),
                batch_id,
            ),
        )


def _finish_health_item(
    item_id: int,
    *,
    status: str,
    reason: str,
    proxy_ok: bool | None = None,
    session_ok: bool | None = None,
    source_access: bool | None = None,
    target_access: bool | None = None,
    target_can_invite: bool | None = None,
    latency_ms: int | None = None,
    busy_operation: str | None = None,
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE session_health_items
            SET status=?, proxy_ok=?, session_ok=?, source_access=?, target_access=?,
                target_can_invite=?, latency_ms=?, busy_operation=?, reason=?, finished_at=?
            WHERE id=?
            """,
            (
                status,
                None if proxy_ok is None else int(proxy_ok),
                None if session_ok is None else int(session_ok),
                None if source_access is None else int(source_access),
                None if target_access is None else int(target_access),
                None if target_can_invite is None else int(target_can_invite),
                latency_ms,
                busy_operation,
                reason[:1000],
                utc_now(),
                item_id,
            ),
        )


async def _inspect_membership(client, reference: str):
    invite_hash = _private_invite_hash(reference)
    if invite_hash:
        invite = await client(CheckChatInviteRequest(invite_hash))
        entity = getattr(invite, "chat", None)
        if entity is None:
            return False, None, "Session özel grubun henüz üyesi değil."
        return True, entity, "Session gruba erişebiliyor."

    entity = await _resolve_entity(client, reference)
    if isinstance(entity, Channel):
        try:
            await client(GetParticipantRequest(entity, "me"))
        except UserNotParticipantError:
            return False, entity, "Session grubun üyesi değil."
    return True, entity, "Session gruba erişebiliyor."


def _can_invite_users(entity) -> bool:
    rights = getattr(entity, "admin_rights", None)
    return bool(
        getattr(entity, "creator", False)
        or (rights and getattr(rights, "invite_users", False))
    )


def _session_wait_reason(session: dict) -> str | None:
    now = utc_now()
    if session["status"] == "flood_wait":
        until = session.get("flood_wait_until")
        if until and until > now:
            return f"Telegram bekleme süresi devam ediyor: {until}"
    if session["status"] == "batch_wait":
        until = session.get("batch_cooldown_until")
        if until and until > now:
            return f"Session parti dinlenmesinde: {until}"
    return None


async def execute_session_health_batch(batch_id: int) -> None:
    now = utc_now()
    with get_connection() as connection:
        claimed = connection.execute(
            """
            UPDATE session_health_batches
            SET status='running', started_at=COALESCE(started_at, ?), finished_at=NULL,
                last_error=NULL, updated_at=?
            WHERE id=? AND status='queued'
            """,
            (now, now, batch_id),
        ).rowcount
    if claimed != 1:
        return

    current_item_id: int | None = None
    try:
        while True:
            with get_connection() as connection:
                batch = connection.execute(
                    "SELECT * FROM session_health_batches WHERE id=?", (batch_id,)
                ).fetchone()
                if not batch or batch["status"] != "running":
                    return
                item = connection.execute(
                    """
                    SELECT i.*, s.status session_status, s.proxy_enabled,
                           s.proxy_host, s.proxy_port, s.flood_wait_until,
                           s.batch_cooldown_until
                    FROM session_health_items i
                    JOIN telegram_sessions s ON s.id=i.session_id
                    WHERE i.batch_id=? AND i.status='queued'
                    ORDER BY i.position, i.id LIMIT 1
                    """,
                    (batch_id,),
                ).fetchone()

            if not item:
                _refresh_health_counts(batch_id)
                finished = utc_now()
                with get_connection() as connection:
                    connection.execute(
                        """
                        UPDATE session_health_batches
                        SET status='completed', finished_at=?, last_error=NULL, updated_at=?
                        WHERE id=? AND status='running'
                        """,
                        (finished, finished, batch_id),
                    )
                    summary = connection.execute(
                        "SELECT * FROM session_health_batches WHERE id=?", (batch_id,)
                    ).fetchone()
                if summary and summary["status"] == "completed":
                    message = (
                        f"{summary['total_count']} session kontrol edildi: "
                        f"{summary['ready_count']} hazır, {summary['warning_count']} uyarı, "
                        f"{summary['failed_count']} hata."
                    )
                    add_log("success", "session_health", f"Sağlık kontrolü #{batch_id}: {message}")
                    add_notification("success", "Toplu sağlık kontrolü tamamlandı", message, "groups")
                return

            current_item_id = int(item["id"])
            session_id = int(item["session_id"])
            with get_connection() as connection:
                connection.execute(
                    """
                    UPDATE session_health_items
                    SET status='checking', reason='Session ve proxy kontrol ediliyor',
                        started_at=?, finished_at=NULL
                    WHERE id=? AND status='queued'
                    """,
                    (utc_now(), current_item_id),
                )

            operation = get_session_operation(session_id)
            if operation:
                label = operation["operation_label"]
                _finish_health_item(
                    current_item_id,
                    status="busy",
                    reason=f"Session aktif işlem nedeniyle test edilmedi: {label}",
                    busy_operation=label,
                )
                _refresh_health_counts(batch_id)
                current_item_id = None
                continue

            wait_reason = _session_wait_reason(
                {
                    "status": item["session_status"],
                    "flood_wait_until": item["flood_wait_until"],
                    "batch_cooldown_until": item["batch_cooldown_until"],
                }
            )
            if wait_reason:
                _finish_health_item(
                    current_item_id,
                    status="waiting",
                    reason=wait_reason,
                )
                _refresh_health_counts(batch_id)
                current_item_id = None
                continue

            if not item["proxy_enabled"] or not item["proxy_host"] or not item["proxy_port"]:
                _finish_health_item(
                    current_item_id,
                    status="failed",
                    proxy_ok=False,
                    session_ok=None,
                    reason="Sabit proxy eksik; session çalıştırılmadı ve ana IP kullanılmadı.",
                )
                _refresh_health_counts(batch_id)
                current_item_id = None
                continue

            client = None
            lease = None
            try:
                lease = await acquire_session_operation(
                    session_id,
                    "health_check",
                    f"health:{batch_id}",
                    f"Sağlık kontrolü #{batch_id}",
                    wait=False,
                )
                client = await _client_for(session_id)
                with get_connection() as connection:
                    tested_session = connection.execute(
                        "SELECT proxy_latency_ms FROM telegram_sessions WHERE id=?",
                        (session_id,),
                    ).fetchone()
                latency_ms = tested_session["proxy_latency_ms"] if tested_session else None

                source_access = None
                target_access = None
                target_can_invite = None
                reasons = ["Proxy ve Telegram session bağlantısı başarılı."]
                if batch["source_ref"]:
                    source_access, _, source_reason = await _inspect_membership(
                        client, batch["source_ref"]
                    )
                    reasons.append(f"Kaynak: {source_reason}")
                if batch["target_ref"]:
                    target_access, target_entity, target_reason = await _inspect_membership(
                        client, batch["target_ref"]
                    )
                    reasons.append(f"Hedef: {target_reason}")
                    if target_access and target_entity is not None:
                        target_can_invite = _can_invite_users(target_entity)
                        reasons.append(
                            "Hedefte üye ekleme yetkisi var."
                            if target_can_invite
                            else "Hedefte üye ekleme yetkisi yok."
                        )

                ready = (
                    source_access is not False
                    and target_access is not False
                    and target_can_invite is not False
                )
                _finish_health_item(
                    current_item_id,
                    status="ready" if ready else "attention",
                    proxy_ok=True,
                    session_ok=True,
                    source_access=source_access,
                    target_access=target_access,
                    target_can_invite=target_can_invite,
                    latency_ms=latency_ms,
                    reason=" ".join(reasons),
                )
            except SessionOperationBusy as error:
                label = error.operation.get("operation_label") or "aktif Telegram işlemi"
                _finish_health_item(
                    current_item_id,
                    status="busy",
                    reason=f"Session test sırasında meşgul oldu: {label}",
                    busy_operation=label,
                )
            except ProxyUnavailableError as error:
                _finish_health_item(
                    current_item_id,
                    status="failed",
                    proxy_ok=False,
                    session_ok=False,
                    reason=f"Proxy bağlantısı başarısız; ana IP kullanılmadı. {error}",
                )
            except FloodWaitError as error:
                until = datetime.now(UTC) + timedelta(seconds=error.seconds)
                with get_connection() as connection:
                    connection.execute(
                        """
                        UPDATE telegram_sessions
                        SET status='flood_wait', flood_wait_until=?, updated_at=?
                        WHERE id=?
                        """,
                        (until.isoformat(), utc_now(), session_id),
                    )
                _finish_health_item(
                    current_item_id,
                    status="waiting",
                    proxy_ok=True,
                    session_ok=True,
                    reason=f"Telegram {error.seconds} saniye FloodWait uyguladı; session beklemeye alındı.",
                )
            except Exception as error:  # noqa: BLE001 - report unexpected probe failures per session
                detail = str(error).strip() or error.__class__.__name__
                _finish_health_item(
                    current_item_id,
                    status="failed",
                    reason=f"{error.__class__.__name__}: {detail}",
                )
            finally:
                try:
                    if client is not None:
                        await client.disconnect()
                finally:
                    if lease is not None:
                        await lease.release()

            _refresh_health_counts(batch_id)
            current_item_id = None
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        with get_connection() as connection:
            if current_item_id is not None:
                connection.execute(
                    """
                    UPDATE session_health_items
                    SET status='queued', reason='Kontrol güvenli biçimde durduruldu.',
                        started_at=NULL, finished_at=NULL
                    WHERE id=? AND status='checking'
                    """,
                    (current_item_id,),
                )
            connection.execute(
                """
                UPDATE session_health_batches
                SET status='paused', last_error='Kontrol durduruldu; devam ettirilebilir.', updated_at=?
                WHERE id=? AND status='running'
                """,
                (utc_now(), batch_id),
            )
        _refresh_health_counts(batch_id)
        raise
