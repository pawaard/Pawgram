import asyncio
import secrets
import sqlite3
from dataclasses import dataclass

from app.config import get_settings
from app.database import get_connection, utc_now

_SESSION_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}


class SessionOperationBusy(RuntimeError):
    def __init__(self, session_id: int, operation: dict | None = None):
        self.session_id = session_id
        self.operation = operation or {}
        label = self.operation.get("operation_label") or "başka bir Telegram işlemi"
        super().__init__(f"Session #{session_id} şu anda {label} için kullanılıyor.")


@dataclass
class SessionOperationLease:
    session_id: int
    owner_token: str
    lock: asyncio.Lock
    released: bool = False

    async def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            with get_connection() as connection:
                connection.execute(
                    "DELETE FROM session_operation_locks WHERE session_id=? AND owner_token=?",
                    (self.session_id, self.owner_token),
                )
        finally:
            if self.lock.locked():
                self.lock.release()


def get_session_operation(session_id: int) -> dict | None:
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM session_operation_locks WHERE session_id=?",
            (session_id,),
        ).fetchone()


def clear_stale_session_operations() -> int:
    _SESSION_LOCKS.clear()
    with get_connection() as connection:
        deleted = connection.execute("DELETE FROM session_operation_locks").rowcount
    return int(deleted)


async def acquire_session_operation(
    session_id: int,
    operation_type: str,
    operation_key: str,
    operation_label: str,
    *,
    wait: bool = True,
) -> SessionOperationLease:
    database_key = str(get_settings().database_path.resolve())
    lock = _SESSION_LOCKS.setdefault((database_key, session_id), asyncio.Lock())
    if not wait and lock.locked():
        raise SessionOperationBusy(session_id, get_session_operation(session_id))

    await lock.acquire()
    owner_token = secrets.token_hex(16)
    try:
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO session_operation_locks(
                    session_id, operation_type, operation_key, operation_label,
                    owner_token, acquired_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    operation_type,
                    operation_key,
                    operation_label,
                    owner_token,
                    utc_now(),
                ),
            )
    except sqlite3.IntegrityError as error:
        lock.release()
        raise SessionOperationBusy(session_id, get_session_operation(session_id)) from error
    except Exception:
        lock.release()
        raise
    return SessionOperationLease(session_id, owner_token, lock)
