from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import shutil
import socket
import sqlite3

# The verifier invokes fixed Windows PowerShell commands with local release paths.
import subprocess  # nosec B404
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import dotenv_values

SOURCE_DIR = Path(__file__).resolve().parent.parent
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from app.config import get_settings
from app.database import get_connection, initialize_database, utc_now
from app.updater import _updater_script

POWERSHELL_QUERY = r"""
$target = [IO.Path]::GetFullPath($env:PAWGRAM_VERIFY_TARGET)
Get-Process -Name Pawgram -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        if ([IO.Path]::GetFullPath($_.Path) -eq $target) { Write-Output $_.Id }
    } catch { }
}
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_release(archive: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(destination)
    root = destination / "Pawgram"
    if not (root / "Pawgram.exe").is_file() or not (root / "_internal").is_dir():
        raise RuntimeError(f"Geçersiz Pawgram release arşivi: {archive}")
    return root


def seed_customer_data(install: Path, started_version: str) -> None:
    database = install / "data" / "console.db"
    previous_database = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(database)
    get_settings.cache_clear()
    try:
        initialize_database()
        now = utc_now()
        with get_connection() as connection:
            connection.executemany(
                """
                INSERT INTO app_settings(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE
                SET value=excluded.value, updated_at=excluded.updated_at
                """,
                [
                    ("heartbeat_enabled", "true", now),
                    ("heartbeat_interval_minutes", "77", now),
                    ("heartbeat_group_id", "-100123456789", now),
                    ("heartbeat_message_template", "Existing heartbeat", now),
                    ("license_lease_token", "existing-license-token", now),
                    ("license_customer_label", "Existing Customer", now),
                    ("local_preference_theme", "midnight", now),
                    ("last_started_version", started_version, now),
                    ("release_notes_seen_version", started_version, now),
                ],
            )
            session_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, proxy_enabled, proxy_type, proxy_host,
                    proxy_port, proxy_username_encrypted, proxy_password_encrypted,
                    invite_batch_limit, invite_cooldown_minutes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'disabled', 1, 'socks5', ?, ?, ?, ?, 7, 35, ?, ?)
                """,
                (
                    "Existing Session",
                    "+90 *** 42",
                    "phone-cipher",
                    "session-cipher",
                    "Existing User",
                    "proxy.persist.example",
                    1080,
                    "proxy-user-cipher",
                    "proxy-pass-cipher",
                    now,
                    now,
                ),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO transfer_jobs(
                    name, session_id, source_ref, source_title, target_ref, target_title,
                    status, max_users, min_delay_seconds, max_delay_seconds, daily_limit,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'completed', 88, 33, 47, 61, ?, ?)
                """,
                (
                    "Existing Invite",
                    session_id,
                    "@existing_source",
                    "Existing Source",
                    "@existing_target",
                    "Existing Target",
                    now,
                    now,
                ),
            )
    finally:
        if previous_database is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = previous_database
        get_settings.cache_clear()
        gc.collect()

    data = install / "data"
    (data / ".secret_key").write_bytes(Fernet.generate_key())
    (data / "existing.session").write_bytes(b"existing-session-file")
    (data / "preferences.json").write_text(
        '{"theme":"midnight","page":"sessions"}', encoding="utf-8"
    )


def customer_snapshot(install: Path) -> dict:
    with sqlite3.connect(install / "data" / "console.db") as connection:
        settings = dict(
            connection.execute(
                """
                SELECT key, value
                FROM app_settings
                WHERE key IN (
                    'heartbeat_enabled', 'heartbeat_interval_minutes',
                    'heartbeat_group_id', 'heartbeat_message_template',
                    'license_lease_token', 'license_customer_label',
                    'local_preference_theme'
                )
                ORDER BY key
                """
            ).fetchall()
        )
        session = connection.execute(
            """
            SELECT label, phone_masked, phone_encrypted, session_encrypted,
                   display_name, status, proxy_enabled, proxy_type, proxy_host,
                   proxy_port, proxy_username_encrypted, proxy_password_encrypted,
                   invite_batch_limit, invite_cooldown_minutes
            FROM telegram_sessions
            WHERE label='Existing Session'
            """
        ).fetchone()
        job = connection.execute(
            """
            SELECT name, source_ref, source_title, target_ref, target_title,
                   status, max_users, min_delay_seconds, max_delay_seconds, daily_limit
            FROM transfer_jobs
            WHERE name='Existing Invite'
            """
        ).fetchone()
    return {
        "env": sha256(install / ".env"),
        "secret_key": sha256(install / "data" / ".secret_key"),
        "session_file": sha256(install / "data" / "existing.session"),
        "preferences_file": sha256(install / "data" / "preferences.json"),
        "settings": settings,
        "session": session,
        "job": job,
    }


def customer_data_preserved(before: dict, after: dict, *, managed_proxy: bool) -> bool:
    preserved_keys = {
        "env",
        "secret_key",
        "session_file",
        "preferences_file",
        "settings",
        "job",
    }
    if any(before[key] != after[key] for key in preserved_keys):
        return False
    if not managed_proxy:
        return before["session"] == after["session"]
    before_session = before["session"]
    after_session = after["session"]
    if before_session is None or after_session is None:
        return before_session == after_session
    return (
        before_session[:5] == after_session[:5]
        and before_session[12:] == after_session[12:]
    )


def managed_proxy_configuration(package: Path) -> dict | None:
    bundle_path = package / "_internal" / "customer-proxy.json"
    if bundle_path.is_file():
        return json.loads(bundle_path.read_text(encoding="utf-8"))
    env_path = package / ".env"
    if not env_path.is_file():
        return None
    environment = dotenv_values(env_path)
    revision = environment.get("DEFAULT_PROXY_REVISION")
    host = environment.get("DEFAULT_PROXY_HOST")
    port = environment.get("DEFAULT_PROXY_PORT")
    if (
        str(environment.get("CUSTOMER_RELEASE", "")).lower() != "true"
        or not revision
        or not host
        or not port
    ):
        return None
    return {
        "revision": revision,
        "proxy_type": environment.get("DEFAULT_PROXY_TYPE") or "socks5",
        "host": host,
        "port": int(port),
        "username": environment.get("DEFAULT_PROXY_USERNAME"),
        "password": environment.get("DEFAULT_PROXY_PASSWORD"),
    }


def managed_proxy_applied(install: Path, proxy: dict | None) -> bool:
    if proxy is None:
        return True
    with sqlite3.connect(install / "data" / "console.db") as connection:
        session = connection.execute(
            """
            SELECT status, proxy_enabled, proxy_type, proxy_host, proxy_port,
                   proxy_username_encrypted IS NOT NULL,
                   proxy_password_encrypted IS NOT NULL
            FROM telegram_sessions
            WHERE label='Existing Session'
            """
        ).fetchone()
        revision = connection.execute(
            "SELECT value FROM app_settings WHERE key='default_login_proxy_revision'"
        ).fetchone()
    if session is None or revision is None:
        return False
    return session == (
        "proxy_pending",
        1,
        proxy["proxy_type"],
        proxy["host"],
        int(proxy["port"]),
        int(bool(proxy.get("username"))),
        int(bool(proxy.get("password"))),
    ) and revision[0] == proxy["revision"]


def powershell_path() -> Path:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
    executable = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not executable.is_file():
        raise RuntimeError("Windows PowerShell bulunamadı.")
    return executable


def run_installer(install: Path, staged: Path, update_root: Path) -> subprocess.CompletedProcess[str]:
    script_path = update_root / "install-update.ps1"
    script_path.write_text(_updater_script(), encoding="utf-8-sig")
    environment = os.environ.copy()
    environment["PAWGRAM_SKIP_UPDATE"] = "1"
    # The executable and arguments are fixed or resolved local release paths.
    return subprocess.run(  # nosec B603
        [
            str(powershell_path()),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            "-RunningProcessId",
            "999999",
            "-InstallDir",
            str(install),
            "-StagedDir",
            str(staged),
        ],
        cwd=update_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def find_pawgram_process(executable: Path) -> int | None:
    environment = os.environ.copy()
    environment["PAWGRAM_VERIFY_TARGET"] = str(executable)
    # The PowerShell query is fixed and the target is passed through a dedicated environment key.
    result = subprocess.run(  # nosec B603
        [
            str(powershell_path()),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            POWERSHELL_QUERY,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    for line in reversed(result.stdout.splitlines()):
        if line.strip().isdigit():
            return int(line.strip())
    return None


def stop_process(process_id: int | None) -> None:
    if process_id is None:
        return
    environment = os.environ.copy()
    environment["PAWGRAM_VERIFY_PID"] = str(process_id)
    # The PowerShell command is fixed and receives a numeric process id.
    subprocess.run(  # nosec B603
        [
            str(powershell_path()),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Stop-Process -Id ([int]$env:PAWGRAM_VERIFY_PID) -ErrorAction SilentlyContinue",
        ],
        env=environment,
        capture_output=True,
        timeout=15,
        check=False,
    )


def wait_for_health(port: int = 8000, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            # The health URL is fixed to localhost.
            with urllib.request.urlopen(  # nosec B310
                f"http://127.0.0.1:{port}/api/health", timeout=1
            ) as response:
                if response.status == 200 and b'"ok":true' in response.read().replace(b" ", b""):
                    return True
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    return False


def assert_port_available(port: int = 8000) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        try:
            handle.bind(("127.0.0.1", port))
        except OSError as error:
            raise RuntimeError(f"Updater simülasyonu için 127.0.0.1:{port} boş olmalı.") from error


def configure_port(install: Path, port: int) -> None:
    env_path = install / ".env"
    lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    filtered = [line for line in lines if not line.startswith("APP_PORT=")]
    env_path.write_text("\n".join([*filtered, f"APP_PORT={port}", ""]), encoding="utf-8")


def simulate_success(customer_zip: Path, update_zip: Path, root: Path, port: int) -> dict:
    install = root / "install-success"
    customer_root = extract_release(customer_zip, root / "customer-success")
    staged = extract_release(update_zip, root / "update-success" / "extracted")
    proxy = managed_proxy_configuration(staged)
    managed_proxy = proxy is not None
    expected_version = (staged / "_internal" / "VERSION").read_text(encoding="utf-8").strip()
    install.mkdir(parents=True)
    shutil.copy2(customer_root / ".env", install / ".env")
    configure_port(install, port)
    (install / "_internal").mkdir()
    (install / "Pawgram.exe").write_bytes(b"OLD-PAWGRAM-EXECUTABLE")
    (install / "_internal" / "VERSION").write_text("0.3.0", encoding="utf-8")
    (install / "_internal" / "old-runtime.txt").write_text("old runtime", encoding="utf-8")
    seed_customer_data(install, "0.3.0")
    before = customer_snapshot(install)
    process_id: int | None = None
    try:
        result = run_installer(install, staged, root / "update-success")
        if result.returncode != 0:
            raise RuntimeError(f"Başarılı update simülasyonu kurulamadı: {result.stderr.strip()}")
        process_id = find_pawgram_process(install / "Pawgram.exe")
        live_health = wait_for_health(port)
        after = customer_snapshot(install)
        log_text = (install / "data" / "update.log").read_text(
            encoding="utf-8-sig", errors="replace"
        )
        evidence = {
            "snapshot_preserved": customer_data_preserved(
                before,
                after,
                managed_proxy=managed_proxy,
            ),
            "managed_proxy_applied": managed_proxy_applied(install, proxy),
            "installed_version": (install / "_internal" / "VERSION")
            .read_text(encoding="utf-8")
            .strip(),
            "health_marker": (root / "update-success" / "startup-health.json").is_file(),
            "live_health": live_health,
            "backup_count": len(list(install.glob(".pawgram-update-backup-*"))),
            "old_runtime_removed": not (install / "_internal" / "old-runtime.txt").exists(),
            "success_logged": "data klasörü korundu" in log_text,
            "restarted": process_id is not None,
        }
        if evidence != {
            "snapshot_preserved": True,
            "managed_proxy_applied": True,
            "installed_version": expected_version,
            "health_marker": True,
            "live_health": True,
            "backup_count": 0,
            "old_runtime_removed": True,
            "success_logged": True,
            "restarted": True,
        }:
            raise RuntimeError(f"Başarılı update kanıtı eksik: {evidence}")
        return evidence
    finally:
        stop_process(process_id or find_pawgram_process(install / "Pawgram.exe"))


def simulate_rollback(customer_zip: Path, root: Path, port: int) -> dict:
    customer_root = extract_release(customer_zip, root / "customer-rollback")
    proxy = managed_proxy_configuration(customer_root)
    managed_proxy = proxy is not None
    install = root / "install-rollback"
    shutil.copytree(customer_root, install)
    configure_port(install, port)
    customer_version = (install / "_internal" / "VERSION").read_text(encoding="utf-8").strip()
    seed_customer_data(install, customer_version)
    before = customer_snapshot(install)
    executable_before = sha256(install / "Pawgram.exe")
    staged = root / "update-rollback" / "extracted" / "Pawgram"
    (staged / "_internal").mkdir(parents=True)
    (staged / "Pawgram.exe").write_bytes(b"NOT-A-WINDOWS-EXECUTABLE")
    (staged / "_internal" / "VERSION").write_text("9.9.9", encoding="utf-8")
    process_id: int | None = None
    try:
        result = run_installer(install, staged, root / "update-rollback")
        if result.returncode == 0:
            raise RuntimeError("Bozuk update simülasyonu beklenmedik biçimde başarılı oldu.")
        deadline = time.monotonic() + 15
        while process_id is None and time.monotonic() < deadline:
            process_id = find_pawgram_process(install / "Pawgram.exe")
            if process_id is None:
                time.sleep(0.25)
        live_health = wait_for_health(port)
        after = customer_snapshot(install)
        log_text = (install / "data" / "update.log").read_text(
            encoding="utf-8-sig", errors="replace"
        )
        evidence = {
            "snapshot_preserved": customer_data_preserved(
                before,
                after,
                managed_proxy=managed_proxy,
            ),
            "managed_proxy_applied": managed_proxy_applied(install, proxy),
            "executable_restored": sha256(install / "Pawgram.exe") == executable_before,
            "restored_version": (install / "_internal" / "VERSION")
            .read_text(encoding="utf-8")
            .strip(),
            "rollback_logged": "geri alınıyor" in log_text,
            "old_version_restarted": process_id is not None,
            "live_health": live_health,
        }
        if evidence != {
            "snapshot_preserved": True,
            "managed_proxy_applied": True,
            "executable_restored": True,
            "restored_version": customer_version,
            "rollback_logged": True,
            "old_version_restarted": True,
            "live_health": True,
        }:
            raise RuntimeError(f"Rollback kanıtı eksik: {evidence}")
        return evidence
    finally:
        stop_process(process_id or find_pawgram_process(install / "Pawgram.exe"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--customer-zip", required=True, type=Path)
    parser.add_argument("--update-zip", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    customer_zip = args.customer_zip.resolve()
    update_zip = args.update_zip.resolve()
    if os.name != "nt":
        raise RuntimeError("Updater simülasyonu yalnızca Windows üzerinde çalışır.")
    assert_port_available(args.port)
    temp_root = Path(tempfile.gettempdir()).resolve()
    simulation_root = Path(tempfile.mkdtemp(prefix="PawgramUpdate-Sim-")).resolve()
    if simulation_root.parent != temp_root or not simulation_root.name.startswith(
        "PawgramUpdate-Sim-"
    ):
        raise RuntimeError("Updater simülasyon klasörü güvenli geçici dizinde değil.")
    try:
        success = simulate_success(customer_zip, update_zip, simulation_root, args.port)
        rollback = simulate_rollback(customer_zip, simulation_root, args.port)
        print(f"Başarılı update simülasyonu: {success}")
        print(f"Rollback simülasyonu: {rollback}")
        print("Updater install/restart/veri koruma/rollback doğrulaması başarılı.")
        return 0
    finally:
        if args.keep:
            print(f"Simülasyon klasörü korundu: {simulation_root}")
        elif simulation_root.parent == temp_root and simulation_root.name.startswith(
            "PawgramUpdate-Sim-"
        ):
            shutil.rmtree(simulation_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
