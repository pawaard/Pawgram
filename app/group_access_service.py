import asyncio
import random
from datetime import UTC, datetime, timedelta

from telethon import utils
from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    UserAlreadyParticipantError,
    UserNotParticipantError,
)
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import Channel

from app.database import add_log, add_notification, get_connection, utc_now
from app.session_operation import acquire_session_operation
from app.telegram_service import (
    GroupJoinPending,
    ProxyUnavailableError,
    _client_for,
    _entity_can_invite_users,
    _private_invite_hash,
    _resolve_entity,
    _resolve_or_request_group_access,
)

TERMINAL_ITEM_STATUSES = ("already_member", "joined", "approval_pending", "failed")


def _refresh_batch_counts(batch_id: int) -> dict[str, int]:
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT status, COUNT(*) count FROM group_access_items WHERE batch_id=? GROUP BY status",
            (batch_id,),
        ).fetchall()
        counts = {row["status"]: int(row["count"]) for row in rows}
        processed = sum(counts.get(status, 0) for status in TERMINAL_ITEM_STATUSES)
        connection.execute(
            """
            UPDATE group_access_batches
            SET processed_count=?, ready_count=?, joined_count=?, pending_count=?, failed_count=?, updated_at=?
            WHERE id=?
            """,
            (
                processed,
                counts.get("already_member", 0),
                counts.get("joined", 0),
                counts.get("approval_pending", 0),
                counts.get("failed", 0),
                utc_now(),
                batch_id,
            ),
        )
    return counts


async def _session_already_has_access(client, reference: str) -> bool:
    invite_hash = _private_invite_hash(reference)
    if invite_hash:
        invite = await client(CheckChatInviteRequest(invite_hash))
        return getattr(invite, "chat", None) is not None

    try:
        entity = await _resolve_entity(client, reference)
    except Exception:  # noqa: BLE001 - normal join helper returns the precise resolution error
        return False
    if not isinstance(entity, Channel):
        return True
    try:
        await client(GetParticipantRequest(entity, "me"))
        return True
    except UserNotParticipantError:
        return False


def _finish_item(
    item_id: int,
    *,
    status: str,
    reason: str,
    entity=None,
    can_invite_users: bool | None = None,
) -> None:
    now = utc_now()
    group_id = utils.get_peer_id(entity) if entity is not None else None
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE group_access_items
            SET status=?, reason=?, resolved_group_id=?, resolved_group_title=?,
                resolved_group_username=?, can_invite_users=?, finished_at=?
            WHERE id=?
            """,
            (
                status,
                reason[:1000],
                group_id,
                getattr(entity, "title", None),
                getattr(entity, "username", None),
                None if can_invite_users is None else int(can_invite_users),
                now,
                item_id,
            ),
        )


def _pause_for_telegram_limit(
    batch_id: int,
    item_id: int,
    session_id: int,
    until: datetime,
    message: str,
) -> None:
    now = utc_now()
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE telegram_sessions
            SET status='flood_wait', flood_wait_until=?, last_error=?, updated_at=?
            WHERE id=?
            """,
            (until.isoformat(), message[:500], now, session_id),
        )
        connection.execute(
            """
            UPDATE group_access_items
            SET status='queued', reason=?, started_at=NULL, finished_at=NULL
            WHERE id=?
            """,
            (message[:1000], item_id),
        )
        connection.execute(
            """
            UPDATE group_access_batches
            SET status='paused', last_error=?, next_action_at=?, updated_at=?
            WHERE id=?
            """,
            (message[:1000], until.isoformat(), now, batch_id),
        )
    _refresh_batch_counts(batch_id)
    add_log("warning", "group_access", f"Hazırlama kuyruğu #{batch_id} durdu: {message}", session_id)
    add_notification("warning", "Session hazırlama durduruldu", message, "groups")


async def execute_group_access_batch(batch_id: int) -> None:
    now = utc_now()
    with get_connection() as connection:
        claimed = connection.execute(
            """
            UPDATE group_access_batches
            SET status='running', started_at=COALESCE(started_at, ?), finished_at=NULL,
                next_action_at=NULL, last_error=NULL, updated_at=?
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
                    "SELECT * FROM group_access_batches WHERE id=?", (batch_id,)
                ).fetchone()
                if not batch or batch["status"] != "running":
                    return
                item = connection.execute(
                    """
                    SELECT i.*, s.label session_label
                    FROM group_access_items i
                    JOIN telegram_sessions s ON s.id=i.session_id
                    WHERE i.batch_id=? AND i.status='queued'
                    ORDER BY i.position, i.id
                    LIMIT 1
                    """,
                    (batch_id,),
                ).fetchone()

            if not item:
                _refresh_batch_counts(batch_id)
                finished = utc_now()
                with get_connection() as connection:
                    connection.execute(
                        """
                        UPDATE group_access_batches
                        SET status='completed', finished_at=?, next_action_at=NULL,
                            last_error=NULL, updated_at=?
                        WHERE id=? AND status='running'
                        """,
                        (finished, finished, batch_id),
                    )
                    summary = connection.execute(
                        "SELECT * FROM group_access_batches WHERE id=?", (batch_id,)
                    ).fetchone()
                if summary and summary["status"] == "completed":
                    message = (
                        f"{summary['total_count']} session işlendi: {summary['ready_count']} zaten hazır, "
                        f"{summary['joined_count']} katıldı, {summary['pending_count']} onay bekliyor, "
                        f"{summary['failed_count']} başarısız."
                    )
                    add_log("success", "group_access", f"Hazırlama kuyruğu #{batch_id} tamamlandı. {message}")
                    add_notification("success", "Session hazırlama tamamlandı", message, "groups")
                return

            current_item_id = int(item["id"])
            session_id = int(item["session_id"])
            with get_connection() as connection:
                connection.execute(
                    """
                    UPDATE group_access_items
                    SET status='checking', reason='Proxy ve grup erişimi kontrol ediliyor',
                        started_at=?, finished_at=NULL
                    WHERE id=? AND status='queued'
                    """,
                    (utc_now(), current_item_id),
                )

            client = None
            lease = None
            try:
                lease = await acquire_session_operation(
                    session_id,
                    "group_access",
                    f"group-access:{batch_id}",
                    f"Hazırlama kuyruğu #{batch_id}",
                )
                client = await _client_for(session_id)
                already_member = await _session_already_has_access(client, batch["group_ref"])
                try:
                    entity = await _resolve_or_request_group_access(
                        client, session_id, batch["group_ref"]
                    )
                except UserAlreadyParticipantError:
                    entity = await _resolve_entity(client, batch["group_ref"])
                    already_member = True

                can_invite = (
                    _entity_can_invite_users(entity)
                    if batch["purpose"] == "target"
                    else None
                )
                status = "already_member" if already_member else "joined"
                reason = "Session zaten grubun üyesi."
                if not already_member:
                    reason = "Session gruba başarıyla katıldı."
                if batch["purpose"] == "target":
                    permission_text = (
                        "Hedef grupta üye ekleme yetkisi var."
                        if can_invite
                        else (
                            "Hedef grupta üye ekleme yetkisi yok; genel 'Üye ekle' "
                            "iznini açın veya hesabı yetkili yönetici yapın."
                        )
                    )
                    reason = f"{reason} {permission_text}"
                _finish_item(
                    current_item_id,
                    status=status,
                    reason=reason,
                    entity=entity,
                    can_invite_users=can_invite,
                )
                add_log(
                    "success",
                    "group_access",
                    f"Hazırlama #{batch_id}: session #{session_id} - {reason}",
                    session_id,
                )
            except GroupJoinPending as error:
                _finish_item(
                    current_item_id,
                    status="approval_pending",
                    reason=f"{error.group_title} için katılım isteği gönderildi; yönetici onayı bekleniyor.",
                )
                add_log(
                    "info",
                    "group_access",
                    f"Hazırlama #{batch_id}: session #{session_id} katılım onayı bekliyor.",
                    session_id,
                )
            except FloodWaitError as error:
                until = datetime.now(UTC) + timedelta(seconds=error.seconds)
                _pause_for_telegram_limit(
                    batch_id,
                    current_item_id,
                    session_id,
                    until,
                    f"Telegram session #{session_id} için {error.seconds} saniye FloodWait uyguladı. Kuyruk güvenli biçimde durduruldu.",
                )
                return
            except PeerFloodError:
                until = datetime.now(UTC) + timedelta(hours=24)
                _pause_for_telegram_limit(
                    batch_id,
                    current_item_id,
                    session_id,
                    until,
                    f"Session #{session_id} Telegram kısıtlamasına takıldı. Hesap 24 saat dinlenmeye, kuyruk duraklatmaya alındı.",
                )
                return
            except ProxyUnavailableError as error:
                reason = f"Proxy bağlantısı kurulamadı; ana IP kullanılmadı. {error}"
                _finish_item(current_item_id, status="failed", reason=reason)
                add_log("error", "group_access", f"Hazırlama #{batch_id}: {reason}", session_id)
            except Exception as error:  # noqa: BLE001 - isolate each queued session
                detail = str(error).strip() or error.__class__.__name__
                reason = f"{error.__class__.__name__}: {detail}"
                _finish_item(current_item_id, status="failed", reason=reason)
                add_log("error", "group_access", f"Hazırlama #{batch_id}: {reason}", session_id)
            finally:
                try:
                    if client is not None:
                        await client.disconnect()
                finally:
                    if lease is not None:
                        await lease.release()

            _refresh_batch_counts(batch_id)
            current_item_id = None
            with get_connection() as connection:
                remaining = connection.execute(
                    "SELECT COUNT(*) count FROM group_access_items WHERE batch_id=? AND status='queued'",
                    (batch_id,),
                ).fetchone()["count"]
                latest = connection.execute(
                    "SELECT status FROM group_access_batches WHERE id=?", (batch_id,)
                ).fetchone()
            if not remaining or not latest or latest["status"] != "running":
                continue

            delay_seconds = random.randint(
                int(batch["min_delay_seconds"]), int(batch["max_delay_seconds"])
            )
            next_action_at = (datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat()
            with get_connection() as connection:
                connection.execute(
                    "UPDATE group_access_batches SET next_action_at=?, updated_at=? WHERE id=? AND status='running'",
                    (next_action_at, utc_now(), batch_id),
                )
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            with get_connection() as connection:
                connection.execute(
                    "UPDATE group_access_batches SET next_action_at=NULL, updated_at=? WHERE id=? AND status='running'",
                    (utc_now(), batch_id),
                )
    except asyncio.CancelledError:
        now = utc_now()
        with get_connection() as connection:
            if current_item_id is not None:
                connection.execute(
                    """
                    UPDATE group_access_items
                    SET status='queued', reason='Program veya kullanıcı tarafından güvenli biçimde durduruldu.',
                        started_at=NULL, finished_at=NULL
                    WHERE id=? AND status='checking'
                    """,
                    (current_item_id,),
                )
            connection.execute(
                """
                UPDATE group_access_batches
                SET status='paused', next_action_at=NULL,
                    last_error='Kuyruk güvenli biçimde durduruldu; devam ettirebilirsiniz.', updated_at=?
                WHERE id=? AND status='running'
                """,
                (now, batch_id),
            )
        _refresh_batch_counts(batch_id)
        raise
