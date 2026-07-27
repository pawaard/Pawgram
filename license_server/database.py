import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

from license_server.config import get_license_server_settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS licenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash TEXT NOT NULL UNIQUE,
    code_hint TEXT NOT NULL,
    customer_label TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    duration_days INTEGER NOT NULL,
    starts_at TEXT,
    expires_at TEXT,
    max_devices INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    installation_id TEXT,
    app_version TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    activated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    FOREIGN KEY(license_id) REFERENCES licenses(id),
    UNIQUE(license_id, device_id)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    license_id INTEGER,
    device_id TEXT,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_licenses_code_hash ON licenses(code_hash);
CREATE INDEX IF NOT EXISTS idx_activations_license ON activations(license_id, status);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def get_connection():
    path = get_license_server_settings().database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def initialize_database() -> None:
    with get_connection() as connection:
        connection.executescript(SCHEMA)


def add_audit(event: str, license_id: int | None, device_id: str | None, detail: str = "") -> None:
    with get_connection() as connection:
        connection.execute(
            "INSERT INTO audit_logs(event, license_id, device_id, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (event, license_id, device_id, detail[:1000], utc_now()),
        )
