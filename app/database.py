import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from app.config import get_settings


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS telegram_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    phone_masked TEXT NOT NULL,
    phone_encrypted TEXT NOT NULL,
    session_encrypted TEXT,
    telegram_user_id INTEGER,
    display_name TEXT,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'awaiting_code',
    flood_wait_until TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_auth (
    phone_hash TEXT PRIMARY KEY,
    phone_encrypted TEXT NOT NULL,
    label TEXT NOT NULL,
    session_encrypted TEXT NOT NULL,
    code_hash_encrypted TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transfer_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    session_id INTEGER NOT NULL,
    source_ref TEXT NOT NULL,
    source_id INTEGER,
    source_title TEXT,
    target_ref TEXT NOT NULL,
    target_id INTEGER,
    target_title TEXT,
    mode TEXT NOT NULL DEFAULT 'preview',
    status TEXT NOT NULL DEFAULT 'draft',
    max_users INTEGER NOT NULL DEFAULT 25,
    min_delay_seconds INTEGER NOT NULL DEFAULT 45,
    max_delay_seconds INTEGER NOT NULL DEFAULT 90,
    daily_limit INTEGER NOT NULL DEFAULT 50,
    processed INTEGER NOT NULL DEFAULT 0,
    succeeded INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES telegram_sessions(id)
);

CREATE TABLE IF NOT EXISTS system_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    session_id INTEGER,
    job_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    telegram_user_id INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    username TEXT,
    access_hash INTEGER,
    source_message_id INTEGER,
    status TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, telegram_user_id),
    FOREIGN KEY(job_id) REFERENCES transfer_jobs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    action_page TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    session_id INTEGER,
    group_ref TEXT NOT NULL,
    group_id INTEGER,
    group_title TEXT,
    window_hours INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    recurring INTEGER NOT NULL DEFAULT 0,
    interval_minutes INTEGER NOT NULL DEFAULT 1440,
    next_run_at TEXT,
    last_run_at TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    unique_users INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES telegram_sessions(id)
);

CREATE TABLE IF NOT EXISTS activity_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL,
    telegram_user_id INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    username TEXT,
    access_hash INTEGER,
    source_message_id INTEGER,
    message_count INTEGER NOT NULL,
    last_message_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(scan_id, telegram_user_id),
    FOREIGN KEY(scan_id) REFERENCES activity_scans(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS session_usage_daily (
    session_id INTEGER NOT NULL,
    usage_date TEXT NOT NULL,
    operation_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    PRIMARY KEY(session_id, usage_date),
    FOREIGN KEY(session_id) REFERENCES telegram_sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS session_invite_usage_daily (
    session_id INTEGER NOT NULL,
    usage_date TEXT NOT NULL,
    invite_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    PRIMARY KEY(session_id, usage_date),
    FOREIGN KEY(session_id) REFERENCES telegram_sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS member_history (
    telegram_user_id INTEGER PRIMARY KEY,
    display_name TEXT,
    username TEXT,
    first_job_id INTEGER NOT NULL,
    source_group_id INTEGER,
    target_group_id INTEGER,
    status TEXT NOT NULL DEFAULT 'approved',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    FOREIGN KEY(first_job_id) REFERENCES transfer_jobs(id)
);
"""


JOB_COLUMNS = {
    "scheduled_at": "TEXT",
    "working_start": "TEXT NOT NULL DEFAULT '09:00'",
    "working_end": "TEXT NOT NULL DEFAULT '22:00'",
    "requires_approval": "INTEGER NOT NULL DEFAULT 1",
    "approved_at": "TEXT",
    "previewed_at": "TEXT",
    "candidate_count": "INTEGER NOT NULL DEFAULT 0",
    "execution_started_at": "TEXT",
    "execution_finished_at": "TEXT",
    "last_error": "TEXT",
}


CANDIDATE_COLUMNS = {
    "selected": "INTEGER NOT NULL DEFAULT 0",
    "processed_at": "TEXT",
    "access_hash": "INTEGER",
    "source_message_id": "INTEGER",
}


ACTIVITY_SCAN_COLUMNS = {
    "access_status": "TEXT NOT NULL DEFAULT 'unknown'",
    "join_requested_at": "TEXT",
    "joined_at": "TEXT",
}


ACTIVITY_RESULT_COLUMNS = {
    "access_hash": "INTEGER",
    "source_message_id": "INTEGER",
}


SESSION_COLUMNS = {
    "proxy_enabled": "INTEGER NOT NULL DEFAULT 0",
    "proxy_type": "TEXT",
    "proxy_host": "TEXT",
    "proxy_port": "INTEGER",
    "proxy_username_encrypted": "TEXT",
    "proxy_password_encrypted": "TEXT",
    "proxy_last_status": "TEXT",
    "proxy_latency_ms": "INTEGER",
    "proxy_last_error": "TEXT",
    "proxy_last_test_at": "TEXT",
    "batch_success_count": "INTEGER NOT NULL DEFAULT 0",
    "batch_cooldown_until": "TEXT",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def dict_factory(cursor: sqlite3.Cursor, row: sqlite3.Row) -> dict:
    return {description[0]: row[index] for index, description in enumerate(cursor.description)}


def initialize_database() -> None:
    path = Path(get_settings().database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        existing_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(transfer_jobs)").fetchall()
        }
        for column, definition in JOB_COLUMNS.items():
            if column not in existing_columns:
                connection.execute(f"ALTER TABLE transfer_jobs ADD COLUMN {column} {definition}")
        existing_candidate_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(job_candidates)").fetchall()
        }
        for column, definition in CANDIDATE_COLUMNS.items():
            if column not in existing_candidate_columns:
                connection.execute(f"ALTER TABLE job_candidates ADD COLUMN {column} {definition}")
        existing_activity_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(activity_scans)").fetchall()
        }
        for column, definition in ACTIVITY_SCAN_COLUMNS.items():
            if column not in existing_activity_columns:
                connection.execute(f"ALTER TABLE activity_scans ADD COLUMN {column} {definition}")
        existing_activity_result_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(activity_results)").fetchall()
        }
        for column, definition in ACTIVITY_RESULT_COLUMNS.items():
            if column not in existing_activity_result_columns:
                connection.execute(f"ALTER TABLE activity_results ADD COLUMN {column} {definition}")
        existing_session_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(telegram_sessions)").fetchall()
        }
        for column, definition in SESSION_COLUMNS.items():
            if column not in existing_session_columns:
                connection.execute(f"ALTER TABLE telegram_sessions ADD COLUMN {column} {definition}")


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(get_settings().database_path)
    connection.row_factory = dict_factory
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def add_log(
    level: str,
    category: str,
    message: str,
    session_id: int | None = None,
    job_id: int | None = None,
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO system_logs(level, category, message, session_id, job_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (level, category, message, session_id, job_id, utc_now()),
        )


def get_app_setting(key: str) -> str | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else None


def set_app_setting(key: str, value: str) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO app_settings(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (key, value, utc_now()),
        )


def add_notification(
    level: str,
    title: str,
    message: str,
    action_page: str | None = None,
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO notifications(level, title, message, action_page, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (level, title, message, action_page, utc_now()),
        )
