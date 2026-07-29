import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from app.database import add_log, get_app_setting, get_connection, utc_now
from app.licensing import local_license_status
from app.session_operation import SessionOperationBusy, acquire_session_operation
from app.telegram_service import _client_for

DEFAULT_HEARTBEAT_INTERVAL_MINUTES = 60
DEFAULT_HEARTBEAT_MESSAGE = "Merhabaa"
HEARTBEAT_POLL_SECONDS = 5


def get_heartbeat_settings() -> dict:
    enabled_value = str(get_app_setting("heartbeat_enabled") or "false").lower()
    interval_value = get_app_setting("heartbeat_interval_minutes")
    try:
        interval_minutes = max(1, int(interval_value or DEFAULT_HEARTBEAT_INTERVAL_MINUTES))
    except ValueError:
        interval_minutes = DEFAULT_HEARTBEAT_INTERVAL_MINUTES
    return {
        "enabled": enabled_value in {"1", "true", "yes", "on"},
        "interval_minutes": interval_minutes,
        "group_id": str(get_app_setting("heartbeat_group_id") or ""),
        "message_template": str(
            get_app_setting("heartbeat_message_template") or DEFAULT_HEARTBEAT_MESSAGE
        ),
    }


def save_heartbeat_settings(
    *,
    enabled: bool,
    interval_minutes: int,
    group_id: str,
    message_template: str,
) -> dict:
    now = datetime.now(UTC)
    next_heartbeat = now + timedelta(minutes=interval_minutes) if enabled else None
    values = {
        "heartbeat_enabled": "true" if enabled else "false",
        "heartbeat_interval_minutes": str(interval_minutes),
        "heartbeat_group_id": group_id,
        "heartbeat_message_template": message_template,
    }
    with get_connection() as connection:
        connection.executemany(
            """
            INSERT INTO app_settings(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            [(key, value, now.isoformat()) for key, value in values.items()],
        )
        connection.execute(
            """
            INSERT INTO heartbeat_session_status(
                session_id, current_status, next_heartbeat_at, updated_at
            )
            SELECT id,
                   CASE WHEN ? = 1 AND status='active' THEN 'scheduled'
                        WHEN ? = 1 THEN 'inactive'
                        ELSE 'disabled' END,
                   CASE WHEN ? = 1 AND status='active' THEN ? ELSE NULL END,
                   ?
            FROM telegram_sessions
            WHERE TRUE
            ON CONFLICT(session_id) DO UPDATE SET
                current_status=excluded.current_status,
                next_heartbeat_at=excluded.next_heartbeat_at,
                updated_at=excluded.updated_at
            """,
            (
                int(enabled),
                int(enabled),
                int(enabled),
                next_heartbeat.isoformat() if next_heartbeat else None,
                now.isoformat(),
            ),
        )
    add_log(
        "success",
        "heartbeat",
        (
            f"Heartbeat {'etkinleştirildi' if enabled else 'devre dışı bırakıldı'}; "
            f"interval={interval_minutes} dakika, hedef={group_id or 'yapılandırılmadı'}."
        ),
    )
    return get_heartbeat_settings()


def heartbeat_status() -> dict:
    settings = get_heartbeat_settings()
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT s.id session_id, s.label session_label, s.status session_status,
                   h.last_heartbeat_at, h.last_success_at, h.last_failure_at,
                   COALESCE(h.success_count, 0) success_count,
                   COALESCE(h.failure_count, 0) failure_count,
                   COALESCE(
                       h.current_status,
                       CASE WHEN ? = 0 THEN 'disabled'
                            WHEN s.status='active' THEN 'pending'
                            ELSE 'inactive' END
                   ) current_status,
                   h.last_error, h.next_heartbeat_at
            FROM telegram_sessions s
            LEFT JOIN heartbeat_session_status h ON h.session_id=s.id
            ORDER BY s.id
            """,
            (int(settings["enabled"]),),
        ).fetchall()
    return {"settings": settings, "sessions": rows}


class HeartbeatService:
    def __init__(self, poll_seconds: int = HEARTBEAT_POLL_SECONDS):
        self.poll_seconds = max(1, poll_seconds)
        self._cycle_lock = asyncio.Lock()

    async def run(self) -> None:
        while True:
            try:
                await self.run_due_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - isolated scheduler boundary
                add_log("error", "heartbeat", f"Heartbeat scheduler hatası: {error}")
            await asyncio.sleep(self.poll_seconds)

    async def stop(self, task: asyncio.Task[None]) -> None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def run_due_cycle(self, *, now: datetime | None = None) -> int:
        if self._cycle_lock.locked():
            return 0
        settings = get_heartbeat_settings()
        if not settings["enabled"] or not settings["group_id"]:
            return 0
        license_state = local_license_status()
        if license_state["required"] and not license_state["valid"]:
            return 0

        current_time = (now or datetime.now(UTC)).astimezone(UTC)
        async with self._cycle_lock:
            with get_connection() as connection:
                sessions = connection.execute(
                    """
                    SELECT s.id
                    FROM telegram_sessions s
                    LEFT JOIN heartbeat_session_status h ON h.session_id=s.id
                    WHERE s.status='active'
                      AND s.session_encrypted IS NOT NULL
                      AND (h.next_heartbeat_at IS NULL OR h.next_heartbeat_at <= ?)
                    ORDER BY s.id
                    """,
                    (current_time.isoformat(),),
                ).fetchall()
            for session in sessions:
                await self._run_session(
                    int(session["id"]),
                    settings,
                    current_time=current_time,
                )
            return len(sessions)

    async def _run_session(
        self,
        session_id: int,
        settings: dict,
        *,
        current_time: datetime,
    ) -> None:
        interval_minutes = int(settings["interval_minutes"])
        next_heartbeat = current_time + timedelta(minutes=interval_minutes)
        attempt_time = current_time.isoformat()
        next_time = next_heartbeat.isoformat()
        self._record_attempt_started(session_id, attempt_time, next_time)

        try:
            lease = await acquire_session_operation(
                session_id,
                "heartbeat",
                f"heartbeat:{attempt_time}",
                f"Session #{session_id} heartbeat",
                wait=False,
            )
        except SessionOperationBusy as error:
            self._record_skipped(session_id, attempt_time, next_time, str(error))
            add_log(
                "info",
                "heartbeat",
                f"Session #{session_id} meşgul olduğu için bu heartbeat döngüsünde atlandı: {error}",
                session_id,
            )
            return

        client = None
        try:
            client = await _client_for(session_id, mutate_session_state=False)
            authorized = await asyncio.wait_for(client.is_user_authorized(), timeout=15)
            if not authorized:
                raise RuntimeError("Telegram session yetkilendirmesi geçersiz.")
            destination = int(settings["group_id"])
            await asyncio.wait_for(
                client.send_message(destination, settings["message_template"]),
                timeout=45,
            )
        except Exception as error:  # noqa: BLE001 - one session cannot stop remaining heartbeats
            message = str(error).strip() or error.__class__.__name__
            self._record_failure(session_id, attempt_time, next_time, message)
            add_log(
                "error",
                "heartbeat",
                f"Session #{session_id} heartbeat başarısız: {message}",
                session_id,
            )
        else:
            self._record_success(session_id, attempt_time, next_time)
            add_log(
                "success",
                "heartbeat",
                f"Session #{session_id} heartbeat mesajını hedef gruba gönderdi.",
                session_id,
            )
        finally:
            if client is not None:
                with suppress(Exception):
                    await client.disconnect()
            await lease.release()

    @staticmethod
    def _record_attempt_started(session_id: int, attempt_time: str, next_time: str) -> None:
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO heartbeat_session_status(
                    session_id, last_heartbeat_at, current_status,
                    next_heartbeat_at, updated_at
                ) VALUES (?, ?, 'running', ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    last_heartbeat_at=excluded.last_heartbeat_at,
                    current_status='running', last_error=NULL,
                    next_heartbeat_at=excluded.next_heartbeat_at,
                    updated_at=excluded.updated_at
                """,
                (session_id, attempt_time, next_time, utc_now()),
            )

    @staticmethod
    def _record_skipped(
        session_id: int,
        attempt_time: str,
        next_time: str,
        reason: str,
    ) -> None:
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE heartbeat_session_status
                SET last_heartbeat_at=?, current_status='skipped_busy',
                    last_error=?, next_heartbeat_at=?, updated_at=?
                WHERE session_id=?
                """,
                (attempt_time, reason[:500], next_time, utc_now(), session_id),
            )

    @staticmethod
    def _record_success(session_id: int, attempt_time: str, next_time: str) -> None:
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE heartbeat_session_status
                SET last_heartbeat_at=?, last_success_at=?,
                    success_count=success_count+1, current_status='success',
                    last_error=NULL, next_heartbeat_at=?, updated_at=?
                WHERE session_id=?
                """,
                (attempt_time, attempt_time, next_time, utc_now(), session_id),
            )

    @staticmethod
    def _record_failure(
        session_id: int,
        attempt_time: str,
        next_time: str,
        error: str,
    ) -> None:
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE heartbeat_session_status
                SET last_heartbeat_at=?, last_failure_at=?,
                    failure_count=failure_count+1, current_status='failed',
                    last_error=?, next_heartbeat_at=?, updated_at=?
                WHERE session_id=?
                """,
                (attempt_time, attempt_time, error[:500], next_time, utc_now(), session_id),
            )


heartbeat_service = HeartbeatService()
