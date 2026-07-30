import asyncio
import csv
import json
import re
import sqlite3
import sys
import zipfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from io import StringIO
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.activity_service import (
    activity_scheduler_loop,
    cancel_activity_scan,
    start_activity_scan,
    stop_activity_scans,
    stop_scheduler,
)
from app.config import APP_DIR, RESOURCE_DIR, get_settings
from app.database import (
    add_log,
    add_notification,
    get_app_setting,
    get_connection,
    initialize_database,
    set_app_setting,
    utc_now,
)
from app.group_access_service import execute_group_access_batch
from app.heartbeat_service import (
    heartbeat_service,
    heartbeat_status,
    save_heartbeat_settings,
)
from app.licensing import (
    activate_license,
    license_refresh_loop,
    local_license_status,
    refresh_license,
)
from app.rate_limit import InMemoryRateLimiter
from app.release_history import (
    acknowledge_release_notes,
    initialize_release_tracking,
    release_notes_overview,
)
from app.runtime_control import schedule_shutdown, shutdown_available
from app.scheduling import next_job_run
from app.schemas import (
    ActivityScanRequest,
    ActivityTransferRequest,
    AdminPasswordRequest,
    CandidateSelectionRequest,
    DefaultProxySettingsRequest,
    GroupAccessBatchRequest,
    GroupResolveRequest,
    HeartbeatSettingsRequest,
    JobCreateRequest,
    LicenseActivationRequest,
    LoginCancelRequest,
    LoginStartRequest,
    LoginVerifyRequest,
    ProxyBulkImportRequest,
    ProxySettingsRequest,
    RotationSettingsRequest,
    SessionHealthBatchRequest,
    SessionInvitePolicyRequest,
    TelegramSettingsRequest,
)
from app.security import (
    create_auth_token,
    decrypt,
    encrypt,
    hash_password,
    verify_auth_token,
    verify_password,
)
from app.session_health_service import execute_session_health_batch
from app.session_operation import clear_stale_session_operations
from app.telegram_service import (
    cancel_pending_login,
    default_login_proxy_public,
    execute_invite_job,
    list_groups,
    preview_job_candidates,
    resolve_group,
    save_default_login_proxy,
    start_login,
    sync_customer_release_proxy,
    test_default_login_proxy,
    test_session_proxy,
    verify_login,
)
from app.updater import (
    UPDATE_MANIFEST_URL,
    check_and_stage_update,
    current_version,
    is_newer_version,
    mark_update_healthy,
    verify_manifest,
)

APP_VERSION = current_version()
APP_BUILD = f"{APP_VERSION}-production"
INVITE_TASKS: dict[int, asyncio.Task[None]] = {}
GROUP_ACCESS_TASKS: dict[int, asyncio.Task[None]] = {}
SESSION_HEALTH_TASKS: dict[int, asyncio.Task[None]] = {}
ADMIN_LOGIN_LIMITER = InMemoryRateLimiter(limit=5, window_seconds=60)


def start_invite_job(job_id: int) -> asyncio.Task[None]:
    existing = INVITE_TASKS.get(job_id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(execute_invite_job(job_id))
    INVITE_TASKS[job_id] = task

    def forget(completed: asyncio.Task[None]) -> None:
        if INVITE_TASKS.get(job_id) is completed:
            INVITE_TASKS.pop(job_id, None)

    task.add_done_callback(forget)
    return task


async def stop_invite_jobs() -> None:
    tasks = [task for task in INVITE_TASKS.values() if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    INVITE_TASKS.clear()
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE transfer_jobs
            SET status='approved',
                last_error='Program kapatıldığı için yarım kalan işlem güvenli biçimde durduruldu; yeniden başlatabilirsiniz.',
                updated_at=?
            WHERE status IN ('running', 'queued_execution')
            """,
            (utc_now(),),
        )


def start_group_access_batch(batch_id: int) -> asyncio.Task[None]:
    existing = GROUP_ACCESS_TASKS.get(batch_id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(execute_group_access_batch(batch_id))
    GROUP_ACCESS_TASKS[batch_id] = task

    def forget(completed: asyncio.Task[None]) -> None:
        if GROUP_ACCESS_TASKS.get(batch_id) is completed:
            GROUP_ACCESS_TASKS.pop(batch_id, None)

    task.add_done_callback(forget)
    return task


async def cancel_group_access_batch(batch_id: int) -> bool:
    task = GROUP_ACCESS_TASKS.get(batch_id)
    if task is None or task.done():
        return False
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return True


async def stop_group_access_batches() -> None:
    tasks = [task for task in GROUP_ACCESS_TASKS.values() if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    GROUP_ACCESS_TASKS.clear()
    now = utc_now()
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE group_access_items
            SET status='queued', reason='Program kapatıldı; session henüz işlenmedi.',
                started_at=NULL, finished_at=NULL
            WHERE status='checking'
            """
        )
        connection.execute(
            """
            UPDATE group_access_batches
            SET status='paused', next_action_at=NULL,
                last_error='Program kapatıldı; kuyruk güvenli biçimde duraklatıldı.', updated_at=?
            WHERE status IN ('running', 'queued')
            """,
            (now,),
        )


def start_session_health_batch(batch_id: int) -> asyncio.Task[None]:
    existing = SESSION_HEALTH_TASKS.get(batch_id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(execute_session_health_batch(batch_id))
    SESSION_HEALTH_TASKS[batch_id] = task

    def forget(completed: asyncio.Task[None]) -> None:
        if SESSION_HEALTH_TASKS.get(batch_id) is completed:
            SESSION_HEALTH_TASKS.pop(batch_id, None)

    task.add_done_callback(forget)
    return task


async def cancel_session_health_batch(batch_id: int) -> bool:
    task = SESSION_HEALTH_TASKS.get(batch_id)
    if task is None or task.done():
        return False
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return True


async def stop_session_health_batches() -> None:
    tasks = [task for task in SESSION_HEALTH_TASKS.values() if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    SESSION_HEALTH_TASKS.clear()
    now = utc_now()
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE session_health_items
            SET status='queued', reason='Program kapatıldı; kontrol yeniden başlatılabilir.',
                started_at=NULL, finished_at=NULL
            WHERE status='checking'
            """
        )
        connection.execute(
            """
            UPDATE session_health_batches
            SET status='paused', last_error='Program kapatıldı; kontrol duraklatıldı.', updated_at=?
            WHERE status IN ('running', 'queued')
            """,
            (now,),
        )


async def invite_scheduler_loop() -> None:
    while True:
        license_state = local_license_status()
        if license_state["required"] and not license_state["valid"]:
            await asyncio.sleep(5)
            continue
        now = utc_now()
        with get_connection() as connection:
            jobs = connection.execute(
                """
                SELECT id FROM transfer_jobs
                WHERE status IN ('scheduled', 'paused_batch', 'paused_quota', 'flood_wait')
                  AND resume_at IS NOT NULL AND resume_at <= ?
                ORDER BY resume_at, id
                LIMIT 5
                """,
                (now,),
            ).fetchall()
        for job in jobs:
            start_invite_job(job["id"])
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    sync_customer_release_proxy()
    initialize_release_tracking(APP_VERSION)
    startup_license = await refresh_license()
    if startup_license.get("required") and not startup_license.get("valid"):
        add_log(
            "warning",
            "license",
            f"Pawgram başlangıçta lisans nedeniyle kilitlendi: {startup_license.get('message')}",
        )
    stale_session_locks = clear_stale_session_operations()
    with get_connection() as connection:
        interrupted_jobs = connection.execute(
            "SELECT id, session_id FROM transfer_jobs WHERE status IN ('running', 'queued_execution')"
        ).fetchall()
        if interrupted_jobs:
            now = utc_now()
            connection.execute(
                """
                UPDATE transfer_jobs
                SET status='approved',
                    last_error='Program yeniden başlatıldığı için yarım kalan işlem yeniden başlatılabilir.',
                    updated_at=?
                WHERE status IN ('running', 'queued_execution')
                """,
                (now,),
            )
        interrupted_scans = connection.execute(
            "SELECT id FROM activity_scans WHERE status='running'"
        ).fetchall()
        if interrupted_scans:
            now = utc_now()
            connection.execute(
                """
                UPDATE activity_scans
                SET status='queued', next_run_at=?,
                    last_error='Program yeniden başlatıldığı için yarım kalan tarama yeniden kuyruğa alındı.',
                    updated_at=?
                WHERE status='running'
                """,
                (now, now),
            )
        interrupted_group_batches = connection.execute(
            "SELECT id FROM group_access_batches WHERE status IN ('running', 'queued')"
        ).fetchall()
        if interrupted_group_batches:
            now = utc_now()
            connection.execute(
                """
                UPDATE group_access_items
                SET status='queued', reason='Program yeniden başlatıldı; işlem yeniden başlatılabilir.',
                    started_at=NULL, finished_at=NULL
                WHERE status='checking'
                """
            )
            connection.execute(
                """
                UPDATE group_access_batches
                SET status='paused', next_action_at=NULL,
                    last_error='Program yeniden başlatıldı; kuyruk güvenli biçimde duraklatıldı.', updated_at=?
                WHERE status IN ('running', 'queued')
                """,
                (now,),
            )
        interrupted_health_batches = connection.execute(
            "SELECT id FROM session_health_batches WHERE status IN ('running', 'queued')"
        ).fetchall()
        if interrupted_health_batches:
            now = utc_now()
            connection.execute(
                """
                UPDATE session_health_items
                SET status='queued', reason='Program yeniden başlatıldı; kontrol yeniden başlatılabilir.',
                    started_at=NULL, finished_at=NULL
                WHERE status='checking'
                """
            )
            connection.execute(
                """
                UPDATE session_health_batches
                SET status='paused', last_error='Program yeniden başlatıldı; kontrol duraklatıldı.', updated_at=?
                WHERE status IN ('running', 'queued')
                """,
                (now,),
            )
    for interrupted_job in interrupted_jobs:
        add_log(
            "warning",
            "invite",
            "Program yeniden başlatılırken yarım kalan üye ekleme işi güvenli biçimde durduruldu.",
            interrupted_job["session_id"],
            interrupted_job["id"],
        )
    if interrupted_scans:
        add_log(
            "warning",
            "activity",
            f"Program yeniden başlatılırken yarım kalan {len(interrupted_scans)} tarama yeniden kuyruğa alındı.",
            job_id=None,
        )
    if interrupted_group_batches:
        add_log(
            "warning",
            "group_access",
            f"Program yeniden başlatılırken yarım kalan {len(interrupted_group_batches)} session hazırlama kuyruğu duraklatıldı.",
        )
    if interrupted_health_batches:
        add_log(
            "warning",
            "session_health",
            f"Program yeniden başlatılırken yarım kalan {len(interrupted_health_batches)} sağlık kontrolü duraklatıldı.",
        )
    if stale_session_locks:
        add_log(
            "warning",
            "session_lock",
            f"Yeniden başlatma sırasında {stale_session_locks} eski session işlem kilidi temizlendi.",
        )
    add_log("info", "system", f"Doğrudan üye ekleme yürütücüsü hazır: {APP_BUILD}")
    scheduler_task = asyncio.create_task(activity_scheduler_loop())
    invite_scheduler_task = asyncio.create_task(invite_scheduler_loop())
    heartbeat_task = asyncio.create_task(heartbeat_service.run())
    license_task = asyncio.create_task(license_refresh_loop())
    mark_update_healthy()
    try:
        yield
    finally:
        await heartbeat_service.stop(heartbeat_task)
        await stop_scheduler(invite_scheduler_task)
        await stop_session_health_batches()
        await stop_group_access_batches()
        await stop_invite_jobs()
        await stop_scheduler(scheduler_task)
        await stop_activity_scans()
        await stop_scheduler(license_task)


_startup_settings = get_settings()
app = FastAPI(
    title=_startup_settings.app_name,
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url=None if _startup_settings.customer_release else "/docs",
    redoc_url=None if _startup_settings.customer_release else "/redoc",
    openapi_url=None if _startup_settings.customer_release else "/openapi.json",
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "testserver"],
)


def current_settings():
    return get_settings()
static_dir = RESOURCE_DIR / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

PUBLIC_API_PATHS = {
    "/api/health",
    "/api/auth/status",
    "/api/auth/setup",
    "/api/auth/login",
    "/api/license/status",
    "/api/license/activate",
    "/api/system/shutdown",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    return response


@app.middleware("http")
async def admin_auth_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.url.path not in PUBLIC_API_PATHS:
        license_state = local_license_status()
        if license_state["required"] and not license_state["valid"]:
            return JSONResponse(
                status_code=402,
                content={"detail": license_state["message"], "license_required": True},
            )
    if request.url.path.startswith("/api/") and request.url.path not in PUBLIC_API_PATHS:
        admin_hash = get_app_setting("admin_password_hash")
        if admin_hash and not verify_auth_token(request.cookies.get("pawgram_session")):
            return JSONResponse(status_code=401, content={"detail": "Yönetici oturumu gerekli."})
    return await call_next(request)


def public_session(row: dict) -> dict:
    wait_seconds = 0
    batch_wait_seconds = 0
    if row["flood_wait_until"]:
        try:
            wait_seconds = max(
                0,
                int((datetime.fromisoformat(row["flood_wait_until"]) - datetime.now(UTC)).total_seconds()),
            )
        except ValueError:
            wait_seconds = 0
    if row.get("batch_cooldown_until"):
        try:
            batch_wait_seconds = max(
                0,
                int((datetime.fromisoformat(row["batch_cooldown_until"]) - datetime.now(UTC)).total_seconds()),
            )
        except ValueError:
            batch_wait_seconds = 0
    status = row["status"]
    if status == "flood_wait" and wait_seconds == 0:
        status = "active"
    if status == "batch_wait" and batch_wait_seconds == 0:
        status = "active"
    proxy_status = row.get("proxy_last_status")
    if not row.get("proxy_enabled") or status == "proxy_error" or proxy_status == "failed":
        health_score, health_label = 0, "Proxy çalışmıyor"
    elif status == "proxy_pending" or not proxy_status:
        health_score, health_label = 50, "Proxy testi bekliyor"
    elif status == "flood_wait":
        health_score, health_label = 55, "Telegram beklemesi"
    elif status == "batch_wait":
        health_score, health_label = 85, "Parti beklemesi"
    else:
        health_score, health_label = 100, "Kullanıma hazır"
    return {
        "id": row["id"],
        "label": row["label"],
        "phone_masked": row["phone_masked"],
        "telegram_user_id": row["telegram_user_id"],
        "display_name": row["display_name"],
        "username": row["username"],
        "status": status,
        "health_score": health_score,
        "health_label": health_label,
        "flood_wait_seconds": wait_seconds,
        "flood_wait_until": row["flood_wait_until"],
        "batch_cooldown_seconds": batch_wait_seconds,
        "batch_cooldown_until": row.get("batch_cooldown_until"),
        "batch_success_count": int(row.get("batch_success_count") or 0),
        "invite_batch_limit": int(row.get("invite_batch_limit") or 0),
        "invite_cooldown_minutes": int(row.get("invite_cooldown_minutes") or 0),
        "last_error": row["last_error"],
        "proxy_enabled": bool(row.get("proxy_enabled")),
        "proxy_type": row.get("proxy_type"),
        "proxy_host": row.get("proxy_host"),
        "proxy_port": row.get("proxy_port"),
        "proxy_last_status": row.get("proxy_last_status"),
        "proxy_latency_ms": row.get("proxy_latency_ms"),
        "proxy_last_error": row.get("proxy_last_error"),
        "proxy_last_test_at": row.get("proxy_last_test_at"),
        "operation_type": row.get("operation_type"),
        "operation_label": row.get("operation_label"),
        "operation_acquired_at": row.get("operation_acquired_at"),
        "today_invite_count": int(row.get("today_invite_count") or 0),
        "today_activity_count": int(row.get("today_activity_count") or 0),
        "last_successful_invite_at": row.get("last_successful_invite_at"),
        "last_activity_at": row.get("last_activity_at"),
        "last_event_at": row.get("last_event_at"),
        "last_event_level": row.get("last_event_level"),
        "last_event_category": row.get("last_event_category"),
        "last_event_message": row.get("last_event_message"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def release_expired_session_waits() -> None:
    now = utc_now()
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE telegram_sessions
            SET status='active', flood_wait_until=NULL, last_error=NULL, updated_at=?
            WHERE status='flood_wait' AND flood_wait_until IS NOT NULL AND flood_wait_until <= ?
            """,
            (now, now),
        )
        connection.execute(
            """
            UPDATE telegram_sessions
            SET status='active', batch_cooldown_until=NULL, batch_success_count=0,
                last_error=NULL, updated_at=?
            WHERE status='batch_wait' AND batch_cooldown_until IS NOT NULL
              AND batch_cooldown_until <= ?
            """,
            (now, now),
        )


@app.get("/")
async def index():
    return FileResponse(static_dir / "index.html")


@app.get("/api/health")
async def health():
    panel_configured = bool(
        get_app_setting("telegram_api_id") and get_app_setting("telegram_api_hash_encrypted")
    )
    return {
        "ok": True,
        "app": current_settings().app_name,
        "telegram_configured": current_settings().telegram_configured or panel_configured,
        "telegram_config_source": "environment" if current_settings().telegram_configured else "panel" if panel_configured else None,
        "environment": current_settings().app_env,
        "build": APP_BUILD,
        "license": {key: value for key, value in local_license_status().items() if key != "lease_token"},
    }


def settings_overview_data() -> dict:
    settings = current_settings()
    panel_api_configured = bool(
        get_app_setting("telegram_api_id")
        and get_app_setting("telegram_api_hash_encrypted")
    )
    telegram_configured = settings.telegram_configured or panel_api_configured
    with get_connection() as connection:
        session_summary = connection.execute(
            """
            SELECT
                COUNT(*) total,
                SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) active,
                SUM(CASE WHEN status IN ('flood_wait', 'batch_wait') THEN 1 ELSE 0 END) waiting,
                SUM(CASE WHEN status IN ('error', 'invalid', 'banned', 'proxy_error') THEN 1 ELSE 0 END) problem,
                SUM(CASE WHEN proxy_enabled=1 THEN 1 ELSE 0 END) proxy_configured,
                SUM(CASE WHEN proxy_enabled=1 AND proxy_last_status='success' AND status!='proxy_error' THEN 1 ELSE 0 END) proxy_healthy,
                SUM(CASE WHEN proxy_enabled=0 OR COALESCE(proxy_last_status, '')!='success' OR status='proxy_error' THEN 1 ELSE 0 END) proxy_attention
            FROM telegram_sessions
            """
        ).fetchone()
        job_rows = connection.execute(
            "SELECT status, COUNT(*) count FROM transfer_jobs GROUP BY status"
        ).fetchall()
        scan_rows = connection.execute(
            "SELECT status, COUNT(*) count FROM activity_scans GROUP BY status"
        ).fetchall()
        active_operations = connection.execute(
            "SELECT COUNT(*) count FROM session_operation_locks"
        ).fetchone()["count"]
    backup_dir = settings.database_path.resolve().parent / "backups"
    backup_files = sorted(
        [
            *backup_dir.glob("pawgram-*.zip"),
            *backup_dir.glob("pawgram-*.db"),
        ] if backup_dir.is_dir() else [],
        key=lambda value: value.stat().st_mtime,
        reverse=True,
    )
    latest_backup = backup_files[0] if backup_files else None
    latest_backup_at = (
        datetime.fromtimestamp(latest_backup.stat().st_mtime, UTC)
        if latest_backup
        else None
    )
    backup_age_hours = (
        max(0, round((datetime.now(UTC) - latest_backup_at).total_seconds() / 3600, 1))
        if latest_backup_at
        else None
    )
    job_counts = {row["status"]: int(row["count"]) for row in job_rows}
    scan_counts = {row["status"]: int(row["count"]) for row in scan_rows}
    license_state = local_license_status()
    return {
        "generated_at": utc_now(),
        "app": {
            "name": settings.app_name,
            "version": APP_VERSION,
            "build": APP_BUILD,
            "environment": settings.app_env,
        },
        "configuration": {
            "telegram_api_configured": telegram_configured,
            "telegram_api_source": (
                "environment"
                if settings.telegram_configured
                else "panel"
                if panel_api_configured
                else None
            ),
            "admin_password_configured": bool(get_app_setting("admin_password_hash")),
            "default_proxy_configured": bool(
                get_app_setting("default_login_proxy_encrypted")
            ),
            "activity_daily_quota": int(
                get_app_setting("activity_daily_quota") or "30"
            ),
        },
        "sessions": {
            "total": int(session_summary["total"] or 0),
            "active": int(session_summary["active"] or 0),
            "waiting": int(session_summary["waiting"] or 0),
            "problem": int(session_summary["problem"] or 0),
            "proxy_configured": int(session_summary["proxy_configured"] or 0),
            "proxy_healthy": int(session_summary["proxy_healthy"] or 0),
            "proxy_attention": int(session_summary["proxy_attention"] or 0),
            "active_operations": int(active_operations or 0),
        },
        "jobs": {
            "total": sum(job_counts.values()),
            "running": job_counts.get("running", 0),
            "attention": sum(
                job_counts.get(status, 0)
                for status in (
                    "paused_batch",
                    "paused_quota",
                    "proxy_error",
                    "flood_wait",
                    "telegram_restricted",
                    "failed",
                )
            ),
        },
        "activity": {
            "total": sum(scan_counts.values()),
            "running": scan_counts.get("running", 0) + scan_counts.get("queued", 0),
            "attention": sum(
                scan_counts.get(status, 0)
                for status in (
                    "paused",
                    "waiting",
                    "waiting_join",
                    "waiting_budget",
                    "error",
                )
            ),
        },
        "backup": {
            "count": len(backup_files),
            "latest_created_at": latest_backup_at.isoformat() if latest_backup_at else None,
            "latest_size_bytes": latest_backup.stat().st_size if latest_backup else None,
            "age_hours": backup_age_hours,
            "database_size_bytes": (
                settings.database_path.stat().st_size
                if settings.database_path.is_file()
                else 0
            ),
        },
        "update": {
            "current_version": APP_VERSION,
            "channel": "stable",
            "official_source": "github_release",
            "automatic_startup_check": bool(getattr(sys, "frozen", False)),
        },
        "license": {
            "required": bool(license_state.get("required")),
            "valid": bool(license_state.get("valid")),
            "status": license_state.get("status"),
        },
        "security": {
            "proxy_fail_closed": True,
            "secrets_included": False,
        },
    }


@app.get("/api/settings/overview")
async def settings_overview():
    return settings_overview_data()


@app.get("/api/settings/diagnostics/report")
async def settings_diagnostics_report():
    report = settings_overview_data()
    report["report"] = {
        "format": "pawgram-redacted-diagnostics-v1",
        "contains_credentials": False,
        "note": (
            "Telefon, kullanıcı adı, proxy adresi, parola, API Hash, session verisi "
            "ve şifreleme anahtarı bu rapora dahil edilmez."
        ),
    }
    content = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        iter([content]),
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="pawgram-diagnostics-{stamp}.json"'
            )
        },
    )


async def fetch_update_status() -> dict:
    checked_at = utc_now()
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(5.0),
            headers={"User-Agent": f"Pawgram/{APP_VERSION}"},
        ) as client:
            response = await client.get(UPDATE_MANIFEST_URL)
        if response.status_code == 404:
            return {
                "reachable": True,
                "checked_at": checked_at,
                "current_version": APP_VERSION,
                "latest_version": None,
                "update_available": False,
                "channel": "stable",
                "message": "Yayınlanmış güncelleme manifesti bulunamadı.",
            }
        response.raise_for_status()
        payload = verify_manifest(response.json())
        latest_version = str(payload["version"])
        update_available = is_newer_version(latest_version, APP_VERSION)
        return {
            "reachable": True,
            "checked_at": checked_at,
            "current_version": APP_VERSION,
            "latest_version": latest_version,
            "update_available": update_available,
            "channel": str(payload["channel"]),
            "message": (
                f"{latest_version} sürümü kullanılabilir."
                if update_available
                else "Pawgram güncel."
            ),
        }
    except (httpx.HTTPError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "reachable": False,
            "checked_at": checked_at,
            "current_version": APP_VERSION,
            "latest_version": None,
            "update_available": False,
            "channel": "stable",
            "message": (
                "Güncelleme sunucusuna ulaşılamadı veya imzalı manifest doğrulanamadı. "
                "Mevcut kurulum değiştirilmedi."
            ),
        }


@app.get("/api/settings/update-status")
async def settings_update_status():
    return await fetch_update_status()


@app.post("/api/settings/update-install")
async def settings_update_install():
    if not shutdown_available():
        raise HTTPException(
            status_code=503,
            detail="Pawgram yeniden başlatma denetimine ulaşılamadı.",
        )
    status = await fetch_update_status()
    if not status["reachable"]:
        raise HTTPException(status_code=503, detail=status["message"])
    if not status["update_available"]:
        raise HTTPException(status_code=409, detail="Kurulabilecek yeni bir Pawgram sürümü bulunamadı.")
    try:
        staged = await asyncio.to_thread(check_and_stage_update, raise_errors=True)
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    if not staged:
        raise HTTPException(status_code=409, detail="Güncelleme artık kullanılamıyor veya zaten kurulu.")
    if not schedule_shutdown():
        raise HTTPException(
            status_code=503,
            detail="Güncelleme hazırlandı ancak Pawgram yeniden başlatma denetimine ulaşılamadı.",
        )
    add_log(
        "info",
        "system",
        f"Pawgram {status['latest_version']} güncellemesi panelden başlatıldı; uygulama yeniden başlatılıyor.",
    )
    return {
        "started": True,
        "latest_version": status["latest_version"],
        "message": "Güncelleme indirildi; Pawgram kapanıp yeni sürümle yeniden başlayacak.",
    }


@app.post("/api/system/shutdown")
async def system_shutdown():
    add_log("info", "system", "Pawgram kullanıcı tarafından panelden kapatılıyor.")
    if not schedule_shutdown():
        raise HTTPException(status_code=503, detail="Pawgram kapatma denetimine ulaşılamadı.")
    return {"closing": True, "message": "Pawgram kapatılıyor."}


@app.get("/api/license/status")
async def license_status():
    return await refresh_license()


@app.post("/api/license/activate")
async def license_activate(payload: LicenseActivationRequest):
    try:
        return await activate_license(payload.license_key)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/auth/status")
async def auth_status(request: Request):
    configured = bool(get_app_setting("admin_password_hash"))
    required = not current_settings().customer_release
    return {
        "required": required,
        "configured": configured,
        "authenticated": (
            not required
            or not configured
            or verify_auth_token(request.cookies.get("pawgram_session"))
        ),
    }


@app.post("/api/auth/setup")
async def auth_setup(payload: AdminPasswordRequest, response: Response):
    with get_connection() as connection:
        created = connection.execute(
            """
            INSERT INTO app_settings(key, value, updated_at)
            VALUES ('admin_password_hash', ?, ?)
            ON CONFLICT(key) DO NOTHING
            """,
            (hash_password(payload.password), utc_now()),
        )
    if created.rowcount != 1:
        raise HTTPException(status_code=409, detail="Yönetici parolası zaten oluşturulmuş.")
    response.set_cookie(
        "pawgram_session",
        create_auth_token(),
        httponly=True,
        samesite="strict",
        secure=current_settings().app_env == "production",
        max_age=86400,
    )
    add_log("success", "security", "Yönetici parolası oluşturuldu")
    add_notification("success", "Pawgram koruma altında", "Yönetici parolası başarıyla oluşturuldu.", "settings")
    return {"ok": True}


@app.post("/api/auth/login")
async def auth_login(payload: AdminPasswordRequest, response: Response, request: Request):
    client_key = request.client.host if request.client else "unknown"
    if not ADMIN_LOGIN_LIMITER.allow(client_key):
        raise HTTPException(
            status_code=429,
            detail="Çok fazla giriş denemesi yapıldı. Bir dakika bekleyin.",
            headers={"Retry-After": "60"},
        )
    stored = get_app_setting("admin_password_hash")
    if not stored or not verify_password(payload.password, stored):
        raise HTTPException(status_code=401, detail="Yönetici parolası hatalı.")
    ADMIN_LOGIN_LIMITER.reset(client_key)
    response.set_cookie(
        "pawgram_session",
        create_auth_token(),
        httponly=True,
        samesite="strict",
        secure=current_settings().app_env == "production",
        max_age=86400,
    )
    add_log("info", "security", "Yönetici oturumu açıldı")
    return {"ok": True}


@app.post("/api/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie("pawgram_session")
    return {"ok": True}


@app.get("/api/onboarding")
async def onboarding():
    with get_connection() as connection:
        session_count = connection.execute("SELECT COUNT(*) count FROM telegram_sessions").fetchone()["count"]
    api_configured = current_settings().telegram_configured or bool(
        get_app_setting("telegram_api_id") and get_app_setting("telegram_api_hash_encrypted")
    )
    customer_release = current_settings().customer_release
    admin_configured = customer_release or bool(get_app_setting("admin_password_hash"))
    return {
        "customer_release": customer_release,
        "admin_configured": admin_configured,
        "api_configured": api_configured,
        "session_configured": session_count > 0,
        "complete": admin_configured and api_configured and session_count > 0,
    }


@app.get("/api/release-notes")
async def release_notes():
    return release_notes_overview(APP_VERSION)


@app.post("/api/release-notes/{version}/acknowledge")
async def release_notes_acknowledge(version: str):
    try:
        return acknowledge_release_notes(version, APP_VERSION)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/settings/telegram")
async def telegram_settings():
    panel_api_id = get_app_setting("telegram_api_id")
    configured = current_settings().telegram_configured or bool(
        panel_api_id and get_app_setting("telegram_api_hash_encrypted")
    )
    return {
        "configured": configured,
        "source": "environment" if current_settings().telegram_configured else "panel" if configured else None,
        "api_id": current_settings().telegram_api_id if current_settings().telegram_configured else int(panel_api_id) if panel_api_id else None,
        "api_hash_masked": "••••••••••••••••••••••••••••••••" if configured else None,
    }


@app.post("/api/settings/telegram")
async def save_telegram_settings(payload: TelegramSettingsRequest):
    if current_settings().telegram_configured:
        raise HTTPException(
            status_code=409,
            detail="Telegram API bilgileri sunucu ortam değişkenlerinden yönetiliyor.",
        )
    set_app_setting("telegram_api_id", str(payload.api_id))
    set_app_setting("telegram_api_hash_encrypted", encrypt(payload.api_hash.strip()))
    add_log("success", "settings", "Telegram API bilgileri panelden güvenli biçimde kaydedildi")
    add_notification("success", "Telegram API hazır", "API ID ve API Hash güvenli biçimde kaydedildi.", "settings")
    return {"ok": True, "configured": True, "api_id": payload.api_id}


@app.get("/api/dashboard")
async def dashboard():
    release_expired_session_waits()
    today = datetime.now(UTC).date().isoformat()
    with get_connection() as connection:
        sessions = connection.execute(
            "SELECT status, COUNT(*) count FROM telegram_sessions GROUP BY status"
        ).fetchall()
        jobs = connection.execute(
            "SELECT status, COUNT(*) count FROM transfer_jobs GROUP BY status"
        ).fetchall()
        totals = connection.execute(
            "SELECT COALESCE(SUM(processed),0) processed, COALESCE(SUM(succeeded),0) succeeded FROM transfer_jobs"
        ).fetchone()
        proxy_attention = connection.execute(
            """
            SELECT COUNT(*) count FROM telegram_sessions
            WHERE proxy_enabled=0 OR status='proxy_error' OR proxy_last_status='failed'
            """
        ).fetchone()["count"]
        pending_group_approvals = connection.execute(
            "SELECT COUNT(*) count FROM group_access_items WHERE status='approval_pending'"
        ).fetchone()["count"]
        activity_attention = connection.execute(
            """
            SELECT COUNT(*) count FROM activity_scans
            WHERE status IN ('paused', 'waiting', 'waiting_join', 'waiting_budget', 'error')
            """
        ).fetchone()["count"]
        job_attention = connection.execute(
            """
            SELECT COUNT(*) count FROM transfer_jobs
            WHERE status IN ('paused_batch', 'paused_quota', 'proxy_error', 'flood_wait', 'telegram_restricted', 'failed')
            """
        ).fetchone()["count"]
        active_operations = connection.execute(
            """
            SELECT l.session_id, l.operation_type, l.operation_key,
                   l.operation_label, l.acquired_at,
                   s.label session_label, s.phone_masked
            FROM session_operation_locks l
            JOIN telegram_sessions s ON s.id=l.session_id
            ORDER BY l.acquired_at, l.session_id
            LIMIT 20
            """
        ).fetchall()
        today_invites = connection.execute(
            """
            SELECT COALESCE(SUM(invite_count), 0) count
            FROM session_invite_usage_daily WHERE usage_date=?
            """,
            (today,),
        ).fetchone()["count"]
        today_candidates = connection.execute(
            """
            SELECT
                SUM(CASE WHEN status IN ('skipped', 'existing') THEN 1 ELSE 0 END) skipped,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed
            FROM job_candidates
            WHERE processed_at IS NOT NULL AND SUBSTR(processed_at, 1, 10)=?
            """,
            (today,),
        ).fetchone()
        unique_active_users = connection.execute(
            """
            SELECT COUNT(DISTINCT telegram_user_id) count FROM activity_results
            WHERE SUBSTR(created_at, 1, 10)=?
            """,
            (today,),
        ).fetchone()["count"]
        completed_scans_today = connection.execute(
            """
            SELECT COUNT(*) count FROM activity_scans
            WHERE status='completed' AND last_run_at IS NOT NULL
              AND SUBSTR(last_run_at, 1, 10)=?
            """,
            (today,),
        ).fetchone()["count"]
        remaining_candidates = connection.execute(
            """
            SELECT COUNT(*) count FROM job_candidates
            WHERE selected=1 AND status='eligible'
            """
        ).fetchone()["count"]
        latest_health = connection.execute(
            """
            SELECT id, status, total_count, ready_count, warning_count,
                   failed_count, finished_at, created_at
            FROM session_health_batches
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    session_counts = {row["status"]: row["count"] for row in sessions}
    job_counts = {row["status"]: row["count"] for row in jobs}
    return {
        "sessions_total": sum(session_counts.values()),
        "sessions_active": session_counts.get("active", 0),
        "sessions_waiting": session_counts.get("flood_wait", 0) + session_counts.get("batch_wait", 0),
        "jobs_total": sum(job_counts.values()),
        "jobs_active": job_counts.get("running", 0),
        "processed": totals["processed"],
        "succeeded": totals["succeeded"],
        "alerts": {
            "proxy_attention": proxy_attention,
            "flood_wait": session_counts.get("flood_wait", 0),
            "batch_wait": session_counts.get("batch_wait", 0),
            "pending_group_approvals": pending_group_approvals,
            "job_attention": job_attention,
            "activity_attention": activity_attention,
        },
        "active_operations": active_operations,
        "today": {
            "invited": today_invites,
            "skipped": int(today_candidates["skipped"] or 0),
            "failed": int(today_candidates["failed"] or 0),
            "unique_active_users": unique_active_users,
            "completed_scans": completed_scans_today,
            "remaining_candidates": remaining_candidates,
        },
        "latest_health": latest_health,
    }


@app.get("/api/activity-scans")
async def activity_scans():
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT a.*, s.label session_label, s.phone_masked,
                   (SELECT COUNT(DISTINCT telegram_user_id) FROM activity_results)
                       AS global_unique_users
            FROM activity_scans a
            LEFT JOIN telegram_sessions s ON s.id = a.session_id
            ORDER BY a.id DESC
            """
        ).fetchall()


@app.post("/api/activity-scans")
async def create_activity_scan(payload: ActivityScanRequest):
    if payload.session_id:
        with get_connection() as connection:
            session = connection.execute(
                "SELECT id FROM telegram_sessions WHERE id = ?", (payload.session_id,)
            ).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="Seçilen Telegram session bulunamadı.")
    now = utc_now()
    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO activity_scans(
                name, session_id, group_ref, window_hours, status, recurring,
                interval_minutes, next_run_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
            """,
            (
                payload.name,
                payload.session_id,
                payload.group_ref,
                payload.window_hours,
                int(payload.recurring),
                payload.interval_minutes,
                now,
                now,
                now,
            ),
        )
        scan_id = cursor.lastrowid
    if scan_id is None:
        raise HTTPException(status_code=500, detail="Aktivite taraması oluşturulamadı.")
    add_log("success", "activity", f"Aktivite taraması oluşturuldu: {payload.name}")
    add_notification("info", "Aktivite taraması sırada", f"{payload.name} otomatik kuyruğa eklendi.", "activity")
    start_activity_scan(scan_id)
    return {"ok": True, "scan_id": scan_id, "status": "queued"}


def get_activity_scan_or_404(scan_id: int) -> dict:
    with get_connection() as connection:
        scan = connection.execute("SELECT * FROM activity_scans WHERE id = ?", (scan_id,)).fetchone()
    if not scan:
        raise HTTPException(status_code=404, detail="Aktivite taraması bulunamadı.")
    return scan


@app.post("/api/activity-scans/{scan_id}/run")
async def run_activity_scan(scan_id: int):
    scan = get_activity_scan_or_404(scan_id)
    if scan["status"] in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="Aktivite taraması zaten çalışıyor.")
    with get_connection() as connection:
        connection.execute(
            "UPDATE activity_scans SET status='queued', next_run_at=?, last_error=NULL, updated_at=? WHERE id=?",
            (utc_now(), utc_now(), scan_id),
        )
    start_activity_scan(scan_id)
    return {"ok": True, "status": "queued"}


@app.post("/api/activity-scans/{scan_id}/pause")
async def pause_activity_scan(scan_id: int):
    scan = get_activity_scan_or_404(scan_id)
    if scan["status"] not in {"queued", "running", "scheduled", "waiting", "waiting_join", "waiting_budget"}:
        raise HTTPException(status_code=409, detail="Bu aktivite taraması duraklatılabilir durumda değil.")
    with get_connection() as connection:
        connection.execute(
            "UPDATE activity_scans SET status='paused', updated_at=? WHERE id=?",
            (utc_now(), scan_id),
        )
    await cancel_activity_scan(scan_id)
    return {"ok": True, "status": "paused"}


@app.post("/api/activity-scans/{scan_id}/resume")
async def resume_activity_scan(scan_id: int):
    scan = get_activity_scan_or_404(scan_id)
    if scan["status"] != "paused":
        raise HTTPException(status_code=409, detail="Yalnızca duraklatılmış bir tarama devam ettirilebilir.")
    with get_connection() as connection:
        connection.execute(
            "UPDATE activity_scans SET status='queued', next_run_at=?, updated_at=? WHERE id=?",
            (utc_now(), utc_now(), scan_id),
        )
    start_activity_scan(scan_id)
    return {"ok": True, "status": "queued"}


@app.delete("/api/activity-scans/{scan_id}")
async def delete_activity_scan(scan_id: int):
    scan = get_activity_scan_or_404(scan_id)
    if scan["status"] in {"queued", "running"}:
        raise HTTPException(
            status_code=409,
            detail="Çalışan veya sırada olan taramayı silmeden önce duraklatın.",
        )
    with get_connection() as connection:
        claimed = connection.execute(
            """
            UPDATE activity_scans
            SET status='deleting', next_run_at=NULL, updated_at=?
            WHERE id=? AND status NOT IN ('queued', 'running')
            """,
            (utc_now(), scan_id),
        ).rowcount
    if claimed != 1:
        raise HTTPException(
            status_code=409,
            detail="Tarama durumu değişti; listeyi yenileyip tekrar deneyin.",
        )

    await cancel_activity_scan(scan_id)
    with get_connection() as connection:
        result_count = connection.execute(
            "SELECT COUNT(*) count FROM activity_results WHERE scan_id=?",
            (scan_id,),
        ).fetchone()["count"]
        connection.execute("DELETE FROM activity_scans WHERE id=?", (scan_id,))
        unique_users = connection.execute(
            "SELECT COUNT(DISTINCT telegram_user_id) count FROM activity_results"
        ).fetchone()["count"]
    add_log(
        "info",
        "activity",
        f"Aktivite taraması silindi: {scan['name']} (SCAN-{scan_id:04d}, {result_count} sonuç)",
        scan.get("session_id"),
    )
    add_notification(
        "info",
        "Aktivite taraması silindi",
        f"{scan['name']} ve bu taramaya ait {result_count} sonuç silindi.",
        "activity",
    )
    return {
        "ok": True,
        "scan_id": scan_id,
        "deleted_results": result_count,
        "unique_active_users": unique_users,
    }


@app.get("/api/activity-scans/{scan_id}/results")
async def activity_scan_results(scan_id: int):
    scan = get_activity_scan_or_404(scan_id)
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT * FROM activity_results
            WHERE scan_id = ?
            ORDER BY last_message_at DESC, message_count DESC
            """,
            (scan_id,),
        ).fetchall()
    return {"scan": scan, "items": rows}


@app.post("/api/activity-scans/{scan_id}/prepare-transfer")
async def prepare_activity_transfer(scan_id: int, payload: ActivityTransferRequest):
    scan = get_activity_scan_or_404(scan_id)
    if scan["status"] not in {"completed", "scheduled"} or not scan["last_run_at"]:
        raise HTTPException(status_code=409, detail="Aktarım hazırlanmadan önce aktivite taraması tamamlanmalı.")
    if not scan["session_id"] or not scan["group_id"]:
        raise HTTPException(status_code=409, detail="Taramanın Telegram session veya grup bilgisi eksik.")
    if payload.min_delay_seconds > payload.max_delay_seconds:
        raise HTTPException(status_code=400, detail="Minimum bekleme maksimum beklemeden büyük olamaz.")

    try:
        target = await resolve_group(scan["session_id"], payload.target_ref)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if target["id"] == scan["group_id"]:
        raise HTTPException(status_code=400, detail="Kaynak ve hedef grup aynı olamaz.")

    now = utc_now()
    job_name = f"{scan['name']} → {target['title']}"[:100]
    with get_connection() as connection:
        job_id = connection.execute(
            """
            INSERT INTO transfer_jobs(
                name, session_id, source_ref, source_id, source_title,
                target_ref, target_id, target_title, mode, status,
                max_users, min_delay_seconds, max_delay_seconds, daily_limit,
                working_start, working_end, requires_approval, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'invite', 'ready', ?, ?, ?, ?,
                      '00:00', '23:59', 1, ?, ?)
            """,
            (
                job_name,
                scan["session_id"],
                scan["group_ref"],
                scan["group_id"],
                scan["group_title"] or scan["group_ref"],
                payload.target_ref,
                target["id"],
                target["title"],
                payload.max_users,
                payload.min_delay_seconds,
                payload.max_delay_seconds,
                payload.daily_limit,
                now,
                now,
            ),
        ).lastrowid
        job = connection.execute(
            "SELECT * FROM transfer_jobs WHERE id=?", (job_id,)
        ).fetchone()

    try:
        summary = await preview_job_candidates(job)
    except Exception as error:
        with get_connection() as connection:
            connection.execute(
                "UPDATE transfer_jobs SET status='failed', last_error=?, updated_at=? WHERE id=?",
                (str(error), utc_now(), job_id),
            )
        raise HTTPException(status_code=400, detail=str(error)) from error

    with get_connection() as connection:
        connection.execute(
            "UPDATE job_candidates SET selected=1 WHERE job_id=? AND status='eligible'",
            (job_id,),
        )
        selected_count = connection.execute(
            "SELECT COUNT(*) count FROM job_candidates WHERE job_id=? AND selected=1",
            (job_id,),
        ).fetchone()["count"]

    add_log("success", "queue", f"Aktivite taramasından aktarım hazırlandı: {job_name}", scan["session_id"], job_id)
    add_notification("success", "Aktarım adayları hazır", f"{selected_count} uygun kullanıcı seçildi.", "jobs")
    return {
        "ok": True,
        "job_id": job_id,
        "selected_count": selected_count,
        "summary": summary,
        "target": target,
    }


@app.get("/api/activity-scans/{scan_id}/report.csv")
async def activity_scan_report(scan_id: int):
    get_activity_scan_or_404(scan_id)
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT telegram_user_id, display_name, username, message_count, last_message_at
            FROM activity_results WHERE scan_id = ?
            ORDER BY last_message_at DESC
            """,
            (scan_id,),
        ).fetchall()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["telegram_user_id", "display_name", "username", "message_count", "last_message_at"])
    for row in rows:
        writer.writerow([
            row["telegram_user_id"], row["display_name"], row["username"] or "",
            row["message_count"], row["last_message_at"],
        ])
    filename = f"pawgram-activity-{scan_id}.csv"
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/sessions")
async def sessions():
    release_expired_session_waits()
    today = datetime.now(UTC).date().isoformat()
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT s.*, l.operation_type, l.operation_label,
                   l.acquired_at operation_acquired_at,
                   COALESCE(i.invite_count, 0) today_invite_count,
                   COALESCE(a.operation_count, 0) today_activity_count,
                   (
                       SELECT MAX(last_used_at) FROM session_invite_usage_daily
                       WHERE session_id=s.id
                   ) last_successful_invite_at,
                   (
                       SELECT MAX(last_used_at) FROM session_usage_daily
                       WHERE session_id=s.id
                   ) last_activity_at,
                   (
                       SELECT created_at FROM system_logs
                       WHERE session_id=s.id ORDER BY id DESC LIMIT 1
                   ) last_event_at,
                   (
                       SELECT level FROM system_logs
                       WHERE session_id=s.id ORDER BY id DESC LIMIT 1
                   ) last_event_level,
                   (
                       SELECT category FROM system_logs
                       WHERE session_id=s.id ORDER BY id DESC LIMIT 1
                   ) last_event_category,
                   (
                       SELECT message FROM system_logs
                       WHERE session_id=s.id ORDER BY id DESC LIMIT 1
                   ) last_event_message
            FROM telegram_sessions s
            LEFT JOIN session_operation_locks l ON l.session_id=s.id
            LEFT JOIN session_invite_usage_daily i
              ON i.session_id=s.id AND i.usage_date=?
            LEFT JOIN session_usage_daily a
              ON a.session_id=s.id AND a.usage_date=?
            ORDER BY s.id DESC
            """,
            (today, today),
        ).fetchall()
    return [public_session(row) for row in rows]


@app.get("/api/sessions/login/default-proxy")
async def login_default_proxy():
    return default_login_proxy_public()


@app.get("/api/settings/default-proxy")
async def settings_default_proxy():
    return default_login_proxy_public()


@app.put("/api/settings/default-proxy")
async def settings_default_proxy_save(payload: DefaultProxySettingsRequest):
    try:
        return save_default_login_proxy(
            payload.proxy_type,
            payload.host,
            payload.port,
            payload.username,
            payload.password,
        )
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/settings/default-proxy/test")
async def settings_default_proxy_test():
    try:
        return await test_default_login_proxy()
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/sessions/login/start")
async def login_start(payload: LoginStartRequest):
    try:
        return await start_login(
            payload.phone,
            payload.label,
            payload.proxy_type,
            payload.proxy_host,
            payload.proxy_port,
            payload.proxy_username,
            payload.proxy_password,
            use_proxy=payload.use_proxy,
        )
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/sessions/login/verify")
async def login_verify(payload: LoginVerifyRequest):
    try:
        return await verify_login(payload.phone, payload.code, payload.password)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.delete("/api/sessions/login/pending")
async def login_cancel(payload: LoginCancelRequest):
    return cancel_pending_login(payload.phone)


def get_session_or_404(session_id: int) -> dict:
    with get_connection() as connection:
        session = connection.execute(
            "SELECT * FROM telegram_sessions WHERE id = ?", (session_id,)
        ).fetchone()
    if not session:
        raise HTTPException(status_code=404, detail="Telegram session bulunamadı.")
    return session


def parse_proxy_line(raw_line: str, default_proxy_type: str) -> dict:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        raise ValueError("Boş veya yorum satırı")
    proxy_type = default_proxy_type
    host: str | None = None
    port_text: str | None = None
    username: str | None = None
    password: str | None = None
    if "://" in line:
        parsed = urlsplit(line)
        if parsed.scheme not in {"socks5", "http"}:
            raise ValueError("Desteklenen türler socks5 ve http")
        proxy_type = parsed.scheme
        host = parsed.hostname
        port_text = str(parsed.port) if parsed.port else None
        username = unquote(parsed.username) if parsed.username else None
        password = unquote(parsed.password) if parsed.password else None
    elif "@" in line:
        auth, endpoint = line.rsplit("@", 1)
        if ":" not in auth or ":" not in endpoint:
            raise ValueError("Beklenen biçim user:pass@host:port")
        username, password = auth.split(":", 1)
        host, port_text = endpoint.rsplit(":", 1)
    else:
        parts = line.split(":", 3)
        if len(parts) == 2:
            host, port_text = parts
        elif len(parts) == 4:
            host, port_text, username, password = parts
        else:
            raise ValueError("Beklenen biçim host:port:user:pass veya host:port")
    host = host.strip() if host else None
    if not host or not port_text or not port_text.isdigit():
        raise ValueError("Host veya port geçersiz")
    port = int(port_text)
    if port < 1 or port > 65535:
        raise ValueError("Port 1-65535 arasında olmalı")
    return {
        "proxy_type": proxy_type,
        "host": host,
        "port": port,
        "username": username.strip() if username else None,
        "password": password if password else None,
    }


@app.get("/api/sessions/{session_id}/proxy")
async def session_proxy(session_id: int):
    session = get_session_or_404(session_id)
    username = (
        decrypt(session["proxy_username_encrypted"])
        if session.get("proxy_username_encrypted")
        else ""
    )
    return {
        "session_id": session_id,
        "enabled": bool(session.get("proxy_enabled")),
        "proxy_type": session.get("proxy_type") or "socks5",
        "host": session.get("proxy_host") or "",
        "port": session.get("proxy_port"),
        "username": username,
        "password_configured": bool(session.get("proxy_password_encrypted")),
        "fail_closed": True,
        "last_status": session.get("proxy_last_status"),
        "latency_ms": session.get("proxy_latency_ms"),
        "last_error": session.get("proxy_last_error"),
        "last_test_at": session.get("proxy_last_test_at"),
    }


@app.put("/api/sessions/{session_id}/proxy")
async def save_session_proxy(session_id: int, payload: ProxySettingsRequest):
    session = get_session_or_404(session_id)
    if payload.enabled and (not payload.host or not payload.port):
        raise HTTPException(status_code=400, detail="Proxy etkinleştirildiğinde host ve port zorunludur.")
    username_encrypted = encrypt(payload.username.strip()) if payload.username else None
    password_encrypted = session.get("proxy_password_encrypted")
    if payload.password:
        password_encrypted = encrypt(payload.password)
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE telegram_sessions
            SET proxy_enabled=?, proxy_type=?, proxy_host=?, proxy_port=?,
                proxy_username_encrypted=?, proxy_password_encrypted=?,
                proxy_last_status=NULL, proxy_latency_ms=NULL, proxy_last_error=NULL,
                status=CASE
                    WHEN status IN ('flood_wait', 'batch_wait') THEN status
                    WHEN ?=1 THEN 'proxy_pending'
                    ELSE 'proxy_error'
                END,
                last_error=CASE
                    WHEN ?=1 THEN NULL
                    ELSE 'Proxy devre dışı. Bu session ana IP üzerinden çalıştırılmaz; proxyyi etkinleştirip test edin.'
                END,
                updated_at=?
            WHERE id=?
            """,
            (
                int(payload.enabled),
                payload.proxy_type,
                payload.host,
                payload.port,
                username_encrypted,
                password_encrypted,
                int(payload.enabled),
                int(payload.enabled),
                utc_now(),
                session_id,
            ),
        )
    mode = "etkinleştirildi" if payload.enabled else "devre dışı bırakıldı"
    add_log("success", "proxy", f"Session proxy ayarı {mode}", session_id)
    return {"ok": True, "enabled": payload.enabled, "fail_closed": True}


@app.post("/api/sessions/{session_id}/proxy/test")
async def proxy_test(session_id: int):
    get_session_or_404(session_id)
    try:
        return await test_session_proxy(session_id)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/sessions/{session_id}/invite-policy")
async def session_invite_policy(session_id: int):
    session = get_session_or_404(session_id)
    return {
        "session_id": session_id,
        "batch_limit": int(session.get("invite_batch_limit") or 0),
        "cooldown_minutes": int(session.get("invite_cooldown_minutes") or 0),
        "batch_success_count": int(session.get("batch_success_count") or 0),
        "automatic_handoff": True,
        "reuse_candidates": True,
        "switch_on_error": True,
        "switch_on_flood_wait": True,
    }


@app.put("/api/sessions/{session_id}/invite-policy")
async def save_session_invite_policy(
    session_id: int,
    payload: SessionInvitePolicyRequest,
):
    get_session_or_404(session_id)
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE telegram_sessions
            SET invite_batch_limit=?, invite_cooldown_minutes=?, updated_at=?
            WHERE id=?
            """,
            (
                payload.batch_limit,
                payload.cooldown_minutes,
                utc_now(),
                session_id,
            ),
        )
    add_log(
        "success",
        "settings",
        (
            "Session için Pawgram parti sınırı kapatıldı"
            if payload.batch_limit == 0
            else f"Session otomatik devir ayarı güncellendi: {payload.batch_limit} ekleme / {payload.cooldown_minutes} dakika dinlenme"
        ),
        session_id,
    )
    return {
        "ok": True,
        "session_id": session_id,
        "batch_limit": payload.batch_limit,
        "cooldown_minutes": payload.cooldown_minutes,
        "automatic_handoff": True,
        "reuse_candidates": True,
    }


@app.delete("/api/sessions/{session_id}/proxy")
async def delete_session_proxy(session_id: int):
    get_session_or_404(session_id)
    guidance = (
        "Proxy silindi. Bu session ana IP üzerinden çalıştırılmaz; "
        "hesabı yeniden kullanmak için yeni bir proxy kaydedip bağlantıyı test edin."
    )
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE telegram_sessions
            SET proxy_enabled=0, proxy_type=NULL, proxy_host=NULL, proxy_port=NULL,
                proxy_username_encrypted=NULL, proxy_password_encrypted=NULL,
                proxy_last_status=NULL, proxy_latency_ms=NULL, proxy_last_error=NULL,
                proxy_last_test_at=NULL, status='proxy_error', last_error=?, updated_at=?
            WHERE id=?
            """,
            (guidance, utc_now(), session_id),
        )
    add_log("warning", "proxy", "Session proxy bilgileri tamamen silindi", session_id)
    add_notification(
        "warning",
        "Proxy silindi",
        "Hesap güvenlik gereği durduruldu. Yeni proxy kaydedip bağlantıyı test edin.",
        "settings",
    )
    return {"ok": True, "session_id": session_id, "fail_closed": True, "message": guidance}


@app.post("/api/proxies/bulk-assign")
async def bulk_assign_proxies(payload: ProxyBulkImportRequest):
    parsed_proxies: list[dict] = []
    invalid_lines: list[dict] = []
    for line_number, raw_line in enumerate(payload.content.splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        try:
            parsed_proxies.append(
                parse_proxy_line(raw_line, payload.default_proxy_type)
            )
        except ValueError as error:
            invalid_lines.append({"line": line_number, "error": str(error)})
    if not parsed_proxies:
        raise HTTPException(
            status_code=400,
            detail="TXT içinde geçerli proxy bulunamadı. host:port:user:pass biçimini kontrol edin.",
        )
    now = utc_now()
    assignments: list[dict] = []
    with get_connection() as connection:
        empty_sessions = connection.execute(
            """
            SELECT id, label, phone_masked FROM telegram_sessions
            WHERE session_encrypted IS NOT NULL
              AND (TRIM(COALESCE(proxy_host, ''))='' OR proxy_port IS NULL)
            ORDER BY id
            """
        ).fetchall()
        for session, proxy in zip(empty_sessions, parsed_proxies):
            connection.execute(
                """
                UPDATE telegram_sessions
                SET proxy_enabled=1, proxy_type=?, proxy_host=?, proxy_port=?,
                    proxy_username_encrypted=?, proxy_password_encrypted=?,
                    proxy_last_status=NULL, proxy_latency_ms=NULL, proxy_last_error=NULL,
                    proxy_last_test_at=NULL,
                    status=CASE WHEN status IN ('flood_wait', 'batch_wait') THEN status ELSE 'proxy_pending' END,
                    last_error=NULL, updated_at=?
                WHERE id=?
                """,
                (
                    proxy["proxy_type"],
                    proxy["host"],
                    proxy["port"],
                    encrypt(proxy["username"]) if proxy["username"] else None,
                    encrypt(proxy["password"]) if proxy["password"] else None,
                    now,
                    session["id"],
                ),
            )
            assignments.append(
                {
                    "session_id": session["id"],
                    "label": session["label"],
                    "phone_masked": session["phone_masked"],
                    "proxy": f"{proxy['host']}:{proxy['port']}",
                    "proxy_type": proxy["proxy_type"],
                }
            )
    add_log(
        "success",
        "proxy",
        f"Toplu proxy dağıtımı: {len(assignments)} session'a sabit proxy atandı",
    )
    add_notification(
        "success",
        "Toplu proxy dağıtımı tamamlandı",
        f"{len(assignments)} hesaba proxy atandı. Her hesap işe başlamadan önce otomatik test edilecek.",
        "settings",
    )
    return {
        "ok": True,
        "assigned_count": len(assignments),
        "assignments": assignments,
        "invalid_lines": invalid_lines,
        "unused_proxy_count": max(0, len(parsed_proxies) - len(assignments)),
        "unassigned_session_count": max(0, len(empty_sessions) - len(assignments)),
    }


@app.get("/api/sessions/{session_id}/groups")
async def groups(session_id: int):
    try:
        return await list_groups(session_id)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/groups/resolve")
async def group_resolve(payload: GroupResolveRequest):
    try:
        return await resolve_group(payload.session_id, payload.reference)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def get_group_access_batch_or_404(batch_id: int) -> dict:
    with get_connection() as connection:
        batch = connection.execute(
            "SELECT * FROM group_access_batches WHERE id=?", (batch_id,)
        ).fetchone()
    if not batch:
        raise HTTPException(status_code=404, detail="Session hazırlama kuyruğu bulunamadı.")
    return batch


def group_access_batch_detail(batch_id: int) -> dict:
    batch = get_group_access_batch_or_404(batch_id)
    with get_connection() as connection:
        items = connection.execute(
            """
            SELECT i.*, s.label session_label, s.phone_masked, s.status session_status
            FROM group_access_items i
            JOIN telegram_sessions s ON s.id=i.session_id
            WHERE i.batch_id=?
            ORDER BY i.position, i.id
            """,
            (batch_id,),
        ).fetchall()
    for item in items:
        if item["can_invite_users"] is not None:
            item["can_invite_users"] = bool(item["can_invite_users"])
    return {"batch": batch, "items": items}


@app.get("/api/group-access-batches")
async def group_access_batches():
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM group_access_batches ORDER BY id DESC LIMIT 25"
        ).fetchall()


@app.get("/api/group-access-batches/{batch_id}")
async def group_access_batch(batch_id: int):
    return group_access_batch_detail(batch_id)


@app.post("/api/group-access-batches")
async def create_group_access_batch(payload: GroupAccessBatchRequest):
    if payload.min_delay_seconds > payload.max_delay_seconds:
        raise HTTPException(
            status_code=400,
            detail="Minimum bekleme maksimum beklemeden büyük olamaz.",
        )
    placeholders = ",".join("?" for _ in payload.session_ids)
    with get_connection() as connection:
        sessions = connection.execute(
            # Placeholder count is generated locally; every ID remains parameterized.
            f"SELECT id FROM telegram_sessions WHERE id IN ({placeholders})",  # nosec B608
            tuple(payload.session_ids),
        ).fetchall()
    found_ids = {int(session["id"]) for session in sessions}
    missing_ids = [session_id for session_id in payload.session_ids if session_id not in found_ids]
    if missing_ids:
        raise HTTPException(
            status_code=404,
            detail=f"Bulunamayan session ID: {', '.join(map(str, missing_ids))}",
        )

    now = utc_now()
    with get_connection() as connection:
        batch_id = connection.execute(
            """
            INSERT INTO group_access_batches(
                group_ref, purpose, status, min_delay_seconds, max_delay_seconds,
                total_count, created_at, updated_at
            ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)
            """,
            (
                payload.group_ref,
                payload.purpose,
                payload.min_delay_seconds,
                payload.max_delay_seconds,
                len(payload.session_ids),
                now,
                now,
            ),
        ).lastrowid
        if batch_id is None:
            raise HTTPException(status_code=500, detail="Session hazırlama kuyruğu oluşturulamadı.")
        connection.executemany(
            """
            INSERT INTO group_access_items(batch_id, session_id, position, status)
            VALUES (?, ?, ?, 'queued')
            """,
            [
                (batch_id, session_id, position)
                for position, session_id in enumerate(payload.session_ids, start=1)
            ],
        )
    add_log(
        "info",
        "group_access",
        f"Hazırlama kuyruğu #{batch_id} oluşturuldu: {len(payload.session_ids)} session.",
    )
    add_notification(
        "info",
        "Session hazırlama başladı",
        f"{len(payload.session_ids)} session gruba sırayla hazırlanıyor.",
        "groups",
    )
    start_group_access_batch(batch_id)
    return group_access_batch_detail(batch_id)


@app.post("/api/group-access-batches/{batch_id}/resume")
async def resume_group_access_batch(batch_id: int):
    batch = get_group_access_batch_or_404(batch_id)
    if batch["status"] in {"running", "queued"}:
        raise HTTPException(status_code=409, detail="Bu hazırlama kuyruğu zaten çalışıyor.")
    if batch["next_action_at"] and batch["next_action_at"] > utc_now():
        raise HTTPException(
            status_code=409,
            detail=f"Telegram bekleme süresi henüz dolmadı. Devam zamanı: {batch['next_action_at']}",
        )
    with get_connection() as connection:
        if batch["status"] == "completed":
            retryable = connection.execute(
                """
                UPDATE group_access_items
                SET status='queued', reason='Erişim yeniden kontrol edilecek',
                    started_at=NULL, finished_at=NULL
                WHERE batch_id=? AND status IN ('approval_pending', 'failed')
                """,
                (batch_id,),
            ).rowcount
            if not retryable:
                raise HTTPException(
                    status_code=409,
                    detail="Bu kuyruktaki tüm sessionlar zaten başarıyla hazırlandı.",
                )
        connection.execute(
            """
            UPDATE group_access_batches
            SET status='queued', last_error=NULL, next_action_at=NULL,
                finished_at=NULL, updated_at=?
            WHERE id=?
            """,
            (utc_now(), batch_id),
        )
    start_group_access_batch(batch_id)
    return group_access_batch_detail(batch_id)


@app.post("/api/group-access-batches/{batch_id}/stop")
async def stop_group_access_batch(batch_id: int):
    batch = get_group_access_batch_or_404(batch_id)
    if batch["status"] not in {"queued", "running", "paused"}:
        raise HTTPException(status_code=409, detail="Bu hazırlama kuyruğu durdurulabilir durumda değil.")
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE group_access_batches
            SET status='stopped', next_action_at=NULL,
                last_error='Kullanıcı tarafından durduruldu.', updated_at=?
            WHERE id=?
            """,
            (utc_now(), batch_id),
        )
    await cancel_group_access_batch(batch_id)
    return group_access_batch_detail(batch_id)


def get_session_health_batch_or_404(batch_id: int) -> dict:
    with get_connection() as connection:
        batch = connection.execute(
            "SELECT * FROM session_health_batches WHERE id=?", (batch_id,)
        ).fetchone()
    if not batch:
        raise HTTPException(status_code=404, detail="Toplu sağlık kontrolü bulunamadı.")
    return batch


def session_health_batch_detail(batch_id: int) -> dict:
    batch = get_session_health_batch_or_404(batch_id)
    with get_connection() as connection:
        items = connection.execute(
            """
            SELECT i.*, s.label session_label, s.phone_masked, s.status session_status
            FROM session_health_items i
            JOIN telegram_sessions s ON s.id=i.session_id
            WHERE i.batch_id=?
            ORDER BY i.position, i.id
            """,
            (batch_id,),
        ).fetchall()
    boolean_fields = (
        "proxy_ok",
        "session_ok",
        "source_access",
        "target_access",
        "target_can_invite",
    )
    for item in items:
        for field in boolean_fields:
            if item[field] is not None:
                item[field] = bool(item[field])
    return {"batch": batch, "items": items}


@app.get("/api/session-health-batches")
async def session_health_batches():
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM session_health_batches ORDER BY id DESC LIMIT 25"
        ).fetchall()


@app.get("/api/session-health-batches/{batch_id}")
async def session_health_batch(batch_id: int):
    return session_health_batch_detail(batch_id)


@app.post("/api/session-health-batches")
async def create_session_health_batch(payload: SessionHealthBatchRequest):
    placeholders = ",".join("?" for _ in payload.session_ids)
    with get_connection() as connection:
        sessions = connection.execute(
            # Placeholder count is generated locally; every ID remains parameterized.
            f"SELECT id FROM telegram_sessions WHERE id IN ({placeholders})",  # nosec B608
            tuple(payload.session_ids),
        ).fetchall()
    found_ids = {int(session["id"]) for session in sessions}
    missing_ids = [session_id for session_id in payload.session_ids if session_id not in found_ids]
    if missing_ids:
        raise HTTPException(
            status_code=404,
            detail=f"Bulunamayan session ID: {', '.join(map(str, missing_ids))}",
        )

    now = utc_now()
    with get_connection() as connection:
        batch_id = connection.execute(
            """
            INSERT INTO session_health_batches(
                source_ref, target_ref, status, total_count, created_at, updated_at
            ) VALUES (?, ?, 'queued', ?, ?, ?)
            """,
            (
                payload.source_ref,
                payload.target_ref,
                len(payload.session_ids),
                now,
                now,
            ),
        ).lastrowid
        if batch_id is None:
            raise HTTPException(status_code=500, detail="Sağlık kontrolü oluşturulamadı.")
        connection.executemany(
            """
            INSERT INTO session_health_items(batch_id, session_id, position, status)
            VALUES (?, ?, ?, 'queued')
            """,
            [
                (batch_id, session_id, position)
                for position, session_id in enumerate(payload.session_ids, start=1)
            ],
        )
    add_log(
        "info",
        "session_health",
        f"Toplu sağlık kontrolü #{batch_id} başlatıldı: {len(payload.session_ids)} session.",
    )
    start_session_health_batch(batch_id)
    return session_health_batch_detail(batch_id)


@app.post("/api/session-health-batches/{batch_id}/resume")
async def resume_session_health_batch(batch_id: int):
    batch = get_session_health_batch_or_404(batch_id)
    if batch["status"] in {"running", "queued"}:
        raise HTTPException(status_code=409, detail="Bu sağlık kontrolü zaten çalışıyor.")
    with get_connection() as connection:
        retryable = connection.execute(
            """
            UPDATE session_health_items
            SET status='queued', reason='Session yeniden kontrol edilecek',
                started_at=NULL, finished_at=NULL
            WHERE batch_id=? AND status IN ('attention', 'failed', 'busy', 'waiting')
            """,
            (batch_id,),
        ).rowcount
        queued = connection.execute(
            "SELECT COUNT(*) count FROM session_health_items WHERE batch_id=? AND status='queued'",
            (batch_id,),
        ).fetchone()["count"]
        if not retryable and not queued:
            raise HTTPException(
                status_code=409,
                detail="Bu kontroldeki tüm sessionlar zaten kullanıma hazır.",
            )
        connection.execute(
            """
            UPDATE session_health_batches
            SET status='queued', last_error=NULL, finished_at=NULL, updated_at=?
            WHERE id=?
            """,
            (utc_now(), batch_id),
        )
    start_session_health_batch(batch_id)
    return session_health_batch_detail(batch_id)


@app.post("/api/session-health-batches/{batch_id}/stop")
async def stop_session_health_batch(batch_id: int):
    batch = get_session_health_batch_or_404(batch_id)
    if batch["status"] not in {"queued", "running", "paused"}:
        raise HTTPException(status_code=409, detail="Bu sağlık kontrolü durdurulabilir durumda değil.")
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE session_health_batches
            SET status='stopped', last_error='Kullanıcı tarafından durduruldu.', updated_at=?
            WHERE id=?
            """,
            (utc_now(), batch_id),
        )
    await cancel_session_health_batch(batch_id)
    return session_health_batch_detail(batch_id)


@app.get("/api/jobs")
async def jobs():
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT j.*, s.label session_label, s.phone_masked
            FROM transfer_jobs j
            JOIN telegram_sessions s ON s.id = j.session_id
            ORDER BY j.id DESC
            """
        ).fetchall()


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: int):
    return get_job_or_404(job_id)


@app.post("/api/jobs")
async def create_job(payload: JobCreateRequest):
    if payload.source_ref.strip() == payload.target_ref.strip():
        raise HTTPException(status_code=400, detail="Çekilecek ve gönderilecek grup aynı olamaz.")
    if payload.min_delay_seconds > payload.max_delay_seconds:
        raise HTTPException(status_code=400, detail="Minimum bekleme maksimum beklemeden büyük olamaz.")
    try:
        source = await resolve_group(payload.session_id, payload.source_ref)
        target = await resolve_group(payload.session_id, payload.target_ref)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if source["id"] == target["id"]:
        raise HTTPException(status_code=400, detail="Çekilecek ve gönderilecek alanlar aynı Telegram grubuna çözümlendi.")

    now = utc_now()
    mode = "preview" if payload.dry_run else "invite"
    status = "ready"
    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO transfer_jobs(
                name, session_id, source_ref, source_id, source_title,
                target_ref, target_id, target_title, mode, status,
                max_users, min_delay_seconds, max_delay_seconds, daily_limit,
                scheduled_at, working_start, working_end, requires_approval,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                payload.name, payload.session_id, payload.source_ref, source["id"], source["title"],
                payload.target_ref, target["id"], target["title"], mode, status,
                payload.max_users, payload.min_delay_seconds, payload.max_delay_seconds,
                payload.daily_limit, payload.scheduled_at, payload.working_start,
                payload.working_end, now, now,
            ),
        )
        job_id = cursor.lastrowid
    add_log("success", "queue", f"İş oluşturuldu: {payload.name}", payload.session_id, job_id)
    add_notification("info", "Yeni aktarım hazır", f"{payload.name} önizleme için kuyruğa eklendi.", "jobs")
    return {"ok": True, "job_id": job_id, "source": source, "target": target, "status": status}


def get_job_or_404(job_id: int) -> dict:
    with get_connection() as connection:
        job = connection.execute("SELECT * FROM transfer_jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        raise HTTPException(status_code=404, detail="İş bulunamadı.")
    return job


@app.post("/api/jobs/{job_id}/preview")
async def preview_job(job_id: int):
    job = get_job_or_404(job_id)
    if job["status"] not in {"ready", "previewed"}:
        raise HTTPException(
            status_code=409,
            detail="Onaylanmış, çalışan veya tamamlanmış bir iş yeniden önizlenemez.",
        )
    try:
        summary = await preview_job_candidates(job)
    except Exception as error:
        add_log("error", "preview", str(error), job["session_id"], job_id)
        raise HTTPException(status_code=400, detail=str(error)) from error
    add_notification(
        "success",
        "Önizleme tamamlandı",
        f"{job['name']} için {summary['eligible']} uygun aday bulundu.",
        "jobs",
    )
    return summary


@app.get("/api/jobs/{job_id}/candidates")
async def job_candidates(job_id: int):
    get_job_or_404(job_id)
    with get_connection() as connection:
        candidates = connection.execute(
            "SELECT * FROM job_candidates WHERE job_id = ? ORDER BY status, id", (job_id,)
        ).fetchall()
        counts = connection.execute(
            "SELECT status, COUNT(*) count FROM job_candidates WHERE job_id = ? GROUP BY status",
            (job_id,),
        ).fetchall()
    return {
        "items": candidates,
        "counts": {row["status"]: row["count"] for row in counts},
        "selected_count": sum(1 for row in candidates if row.get("selected")),
    }


@app.put("/api/jobs/{job_id}/candidates/selection")
async def select_job_candidates(job_id: int, payload: CandidateSelectionRequest):
    job = get_job_or_404(job_id)
    if job["status"] != "previewed":
        raise HTTPException(status_code=409, detail="Yalnızca önizlenmiş bir işin aday seçimi değiştirilebilir.")
    requested = set(payload.candidate_ids)
    with get_connection() as connection:
        eligible = connection.execute(
            "SELECT id FROM job_candidates WHERE job_id=? AND status='eligible'",
            (job_id,),
        ).fetchall()
        eligible_ids = {row["id"] for row in eligible}
        if not requested.issubset(eligible_ids):
            raise HTTPException(status_code=400, detail="Yalnızca uygun adaylar seçilebilir.")
        connection.execute("UPDATE job_candidates SET selected=0 WHERE job_id=?", (job_id,))
        if requested:
            placeholders = ",".join("?" for _ in requested)
            # Placeholder text is generated only from the validated list length; every ID stays bound.
            connection.execute(
                f"UPDATE job_candidates SET selected=1 WHERE job_id=? AND id IN ({placeholders})",  # nosec B608
                (job_id, *sorted(requested)),
            )
    return {"ok": True, "selected_count": len(requested)}


@app.post("/api/jobs/{job_id}/approve")
async def approve_job(job_id: int):
    job = get_job_or_404(job_id)
    if job["status"] != "previewed":
        raise HTTPException(status_code=409, detail="Yalnızca önizlenmiş bir iş onaylanabilir.")
    if not job["previewed_at"]:
        raise HTTPException(status_code=400, detail="İş onaylanmadan önce aday önizlemesi tamamlanmalı.")
    with get_connection() as connection:
        selected_count = connection.execute(
            "SELECT COUNT(*) count FROM job_candidates WHERE job_id=? AND status='eligible' AND selected=1",
            (job_id,),
        ).fetchone()["count"]
    if selected_count < 1:
        raise HTTPException(status_code=400, detail="Onaylamadan önce en az bir uygun aday seçin.")
    now = utc_now()
    with get_connection() as connection:
        connection.execute(
            "UPDATE transfer_jobs SET status='approved', approved_at=?, updated_at=? WHERE id=?",
            (now, now, job_id),
        )
    add_log("success", "approval", f"İş yönetici tarafından onaylandı: {job['name']}", job["session_id"], job_id)
    add_notification("success", "Aktarım onaylandı", f"{job['name']} yönetici tarafından onaylandı.", "jobs")
    return {"ok": True, "status": "approved", "selected_count": selected_count}


@app.post("/api/jobs/{job_id}/execute")
async def execute_job(job_id: int):
    job = get_job_or_404(job_id)
    resumable_statuses = {
        "approved",
        "scheduled",
        "paused_quota",
        "paused_batch",
        "proxy_error",
        "flood_wait",
        "telegram_restricted",
    }
    if job["status"] not in resumable_statuses:
        raise HTTPException(
            status_code=409,
            detail="Yalnızca onaylı veya güvenlik nedeniyle duraklatılmış işler başlatılabilir.",
        )
    with get_connection() as connection:
        selected_count = connection.execute(
            "SELECT COUNT(*) count FROM job_candidates WHERE job_id=? AND selected=1 AND status='eligible'",
            (job_id,),
        ).fetchone()["count"]
        missing_message_context = connection.execute(
            """
            SELECT COUNT(*) count FROM job_candidates
            WHERE job_id=? AND selected=1 AND status='eligible' AND source_message_id IS NULL
            """,
            (job_id,),
        ).fetchone()["count"]
    if selected_count < 1:
        raise HTTPException(status_code=400, detail="İşlenecek seçili aday kalmadı.")
    if missing_message_context:
        raise HTTPException(
            status_code=409,
            detail="Seçili adayların Telegram kaynak mesaj referansı eksik. Önizlemeyi yeniden çalıştırın.",
        )
    next_run = next_job_run(job)
    now_dt = datetime.now(UTC)
    if next_run > now_dt:
        message = f"İş planlanan çalışma penceresinde otomatik başlayacak: {next_run.isoformat()}"
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE transfer_jobs
                SET status='scheduled', resume_at=?, last_error=?, updated_at=?
                WHERE id=? AND status IN ('approved', 'scheduled', 'paused_quota', 'paused_batch', 'flood_wait', 'telegram_restricted')
                """,
                (next_run.isoformat(), message, utc_now(), job_id),
            )
        return {
            "ok": True,
            "status": "scheduled",
            "selected_count": selected_count,
            "processed": job["processed"],
            "succeeded": job["succeeded"],
            "skipped": job["skipped"],
            "failed": job["failed"],
            "last_error": message,
            "resume_at": next_run.isoformat(),
        }
    now = utc_now()
    with get_connection() as connection:
        claimed = connection.execute(
            """
            UPDATE transfer_jobs
            SET status='queued_execution', execution_started_at=COALESCE(execution_started_at, ?),
                resume_at=NULL, last_error=NULL, updated_at=?
            WHERE id=? AND status IN ('approved', 'scheduled', 'paused_quota', 'paused_batch', 'proxy_error', 'flood_wait', 'telegram_restricted')
            """,
            (now, now, job_id),
        )
    if claimed.rowcount != 1:
        raise HTTPException(status_code=409, detail="İş zaten başlatılmış veya durumu değişmiş.")
    add_log(
        "info",
        "invite",
        "Düğme üzerinden doğrudan Telegram üye ekleme işlemi başlatıldı",
        job["session_id"],
        job_id,
    )
    start_invite_job(job_id)
    queued_job = get_job_or_404(job_id)
    return {
        "ok": True,
        "status": queued_job["status"],
        "selected_count": selected_count,
        "processed": queued_job["processed"],
        "succeeded": queued_job["succeeded"],
        "skipped": queued_job["skipped"],
        "failed": queued_job["failed"],
        "last_error": queued_job["last_error"],
    }


@app.get("/api/jobs/{job_id}/report.csv")
async def job_report_csv(job_id: int):
    get_job_or_404(job_id)
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT telegram_user_id, display_name, username, status, reason, created_at "
            "FROM job_candidates WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["telegram_user_id", "display_name", "username", "status", "reason", "created_at"])
    for row in rows:
        writer.writerow([
            row["telegram_user_id"], row["display_name"], row["username"] or "",
            row["status"], row["reason"] or "", row["created_at"],
        ])
    filename = f"pawgram-job-{job_id}-report.csv"
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _log_query_parts(
    search: str = "",
    level: str = "",
    category: str = "",
    session_id: int | None = None,
    job_id: int | None = None,
    date_from: str = "",
    date_to: str = "",
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    parameters: list[object] = []
    if search.strip():
        clauses.append("(LOWER(message) LIKE ? OR LOWER(category) LIKE ?)")
        pattern = f"%{search.strip().lower()}%"
        parameters.extend([pattern, pattern])
    if level.strip():
        clauses.append("level = ?")
        parameters.append(level.strip())
    if category.strip():
        clauses.append("category = ?")
        parameters.append(category.strip())
    if session_id is not None:
        clauses.append("session_id = ?")
        parameters.append(session_id)
    if job_id is not None:
        clauses.append("job_id = ?")
        parameters.append(job_id)
    if date_from.strip():
        clauses.append("date(created_at) >= date(?)")
        parameters.append(date_from.strip())
    if date_to.strip():
        clauses.append("date(created_at) <= date(?)")
        parameters.append(date_to.strip())
    return (f" WHERE {' AND '.join(clauses)}" if clauses else ""), parameters


def _redact_log_message(message: str) -> str:
    redacted = re.sub(
        r"(?i)\b(https?|socks5?)://[^\s/@:]+:[^\s/@]+@[^\s/]+",
        lambda match: f"{match.group(1)}://***:***@***",
        message,
    )
    redacted = re.sub(
        r"(?<![\w.-])([a-z0-9.-]+):(\d{2,5}):([^:\s]+):([^:\s]+)",
        r"\1:\2:***:***",
        redacted,
        flags=re.IGNORECASE,
    )
    redacted = re.sub(r"\+\d[\d\s()-]{7,}\d", "+***", redacted)
    redacted = re.sub(
        r"(?i)\b(api[_ -]?hash|proxy[_ -]?password|password|parola|doğrulama kodu|login code)\s*[:=]\s*\S+",
        lambda match: f"{match.group(1)}=***",
        redacted,
    )
    return re.sub(r"\b[a-fA-F0-9]{32,}\b", "***", redacted)


@app.get("/api/logs/export")
async def export_logs(
    format: str = "json",
    search: str = "",
    level: str = "",
    category: str = "",
    session_id: int | None = None,
    job_id: int | None = None,
    date_from: str = "",
    date_to: str = "",
):
    if format not in {"json", "csv"}:
        raise HTTPException(status_code=400, detail="Dışa aktarma biçimi json veya csv olmalıdır.")
    where_sql, parameters = _log_query_parts(
        search, level, category, session_id, job_id, date_from, date_to
    )
    with get_connection() as connection:
        rows = connection.execute(
            # where_sql is assembled only from fixed clauses in _log_query_parts.
            f"SELECT id, level, category, message, session_id, job_id, created_at "  # nosec B608
            f"FROM system_logs{where_sql} ORDER BY id DESC LIMIT 2000",
            parameters,
        ).fetchall()
    records = [
        {
            "id": row["id"],
            "level": row["level"],
            "category": row["category"],
            "message": _redact_log_message(row["message"]),
            "session_id": row["session_id"],
            "job_id": row["job_id"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    if format == "json":
        content = json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")
        return StreamingResponse(
            iter([content]),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="pawgram-logs-{stamp}.json"'},
        )
    output = StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["id", "level", "category", "message", "session_id", "job_id", "created_at"],
    )
    writer.writeheader()
    writer.writerows(records)
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="pawgram-logs-{stamp}.csv"'},
    )


@app.get("/api/logs")
async def logs(
    limit: int = 100,
    offset: int = 0,
    search: str = "",
    level: str = "",
    category: str = "",
    session_id: int | None = None,
    job_id: int | None = None,
    date_from: str = "",
    date_to: str = "",
):
    safe_limit = max(1, min(limit, 500))
    safe_offset = max(0, offset)
    where_sql, parameters = _log_query_parts(
        search, level, category, session_id, job_id, date_from, date_to
    )
    with get_connection() as connection:
        return connection.execute(
            # where_sql is assembled only from fixed clauses in _log_query_parts.
            f"SELECT * FROM system_logs{where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",  # nosec B608
            [*parameters, safe_limit, safe_offset],
        ).fetchall()


@app.get("/api/notifications")
async def notifications(limit: int = 30):
    safe_limit = max(1, min(limit, 100))
    with get_connection() as connection:
        items = connection.execute(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (safe_limit,)
        ).fetchall()
        unread = connection.execute(
            "SELECT COUNT(*) count FROM notifications WHERE is_read = 0"
        ).fetchone()["count"]
    return {"items": items, "unread": unread}


@app.post("/api/notifications/read")
async def mark_notifications_read():
    with get_connection() as connection:
        connection.execute("UPDATE notifications SET is_read = 1 WHERE is_read = 0")
    return {"ok": True}


@app.get("/api/settings/rotation")
async def rotation_settings():
    return {
        "mode": "round_robin",
        "daily_quota": int(get_app_setting("activity_daily_quota") or "30"),
        "switch_timing": "before_operation",
        "switch_on_error": False,
        "switch_on_flood_wait": False,
    }


@app.post("/api/settings/rotation")
async def save_rotation_settings(payload: RotationSettingsRequest):
    set_app_setting("activity_daily_quota", str(payload.daily_quota))
    add_log("success", "settings", f"Round-Robin günlük session kotası {payload.daily_quota} olarak güncellendi")
    add_notification(
        "success",
        "Round-Robin kotası güncellendi",
        f"Her session için günlük en fazla {payload.daily_quota} yeni aktivite işlemi kullanılacak.",
        "settings",
    )
    return {"ok": True, "daily_quota": payload.daily_quota}


@app.get("/api/heartbeat")
async def heartbeat_overview():
    return heartbeat_status()


@app.post("/api/heartbeat/settings")
async def heartbeat_settings_save(payload: HeartbeatSettingsRequest):
    if payload.enabled and not payload.group_id:
        raise HTTPException(
            status_code=400,
            detail="Heartbeat etkinleştirilmeden önce Telegram Group ID girilmelidir.",
        )
    settings = save_heartbeat_settings(
        enabled=payload.enabled,
        interval_minutes=payload.interval_minutes,
        group_id=payload.group_id,
        message_template=payload.message_template,
    )
    return {"ok": True, "settings": settings}


@app.get("/api/backups")
async def backups():
    settings = current_settings()
    backup_dir = settings.database_path.resolve().parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    return [
        {"name": item.name, "size": item.stat().st_size, "created_at": datetime.fromtimestamp(item.stat().st_mtime, UTC).isoformat()}
        for item in sorted(
            [*backup_dir.glob("pawgram-*.zip"), *backup_dir.glob("pawgram-*.db")],
            key=lambda value: value.stat().st_mtime,
            reverse=True,
        )
    ]


@app.post("/api/backups")
async def create_backup():
    settings = current_settings()
    data_dir = settings.database_path.resolve().parent
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    name = f"pawgram-{stamp}.zip"
    destination = backup_dir / name
    database_copy = backup_dir / f".pawgram-{stamp}.db.tmp"
    try:
        source_connection = sqlite3.connect(settings.database_path)
        destination_connection = sqlite3.connect(database_copy)
        try:
            source_connection.backup(destination_connection)
            integrity = destination_connection.execute("PRAGMA quick_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise RuntimeError("Oluşturulan veritabanı yedeği bütünlük denetiminden geçemedi.")
        finally:
            destination_connection.close()
            source_connection.close()
        secret_path = data_dir / ".secret_key"
        installation_path = APP_DIR / "data" / "installation_id"
        metadata = {
            "product": "Pawgram",
            "created_at": utc_now(),
            "database": "console.db",
            "secret_included": secret_path.is_file(),
            "restore_note": (
                "Pawgram kapalıyken console.db ve varsa .secret_key dosyasını "
                "uygulamanın data klasörüne geri koyun."
            ),
        }
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(database_copy, "console.db")
            archive.writestr(
                "backup-info.json",
                json.dumps(metadata, ensure_ascii=False, indent=2),
            )
            if secret_path.is_file():
                archive.write(secret_path, ".secret_key")
            if installation_path.is_file():
                archive.write(installation_path, "installation_id")
    finally:
        database_copy.unlink(missing_ok=True)
    add_log("success", "backup", f"Veritabanı yedeği oluşturuldu: {name}")
    add_notification("success", "Yedekleme tamamlandı", f"{name} güvenli biçimde oluşturuldu.", "settings")
    return {
        "ok": True,
        "name": name,
        "size": destination.stat().st_size,
        "secret_included": metadata["secret_included"],
    }


@app.get("/api/backups/{name}")
async def download_backup(name: str):
    settings = current_settings()
    backup_dir = (settings.database_path.resolve().parent / "backups").resolve()
    candidate = (backup_dir / name).resolve()
    if (
        candidate.parent != backup_dir
        or not candidate.is_file()
        or not name.startswith("pawgram-")
        or candidate.suffix.lower() not in {".zip", ".db"}
    ):
        raise HTTPException(status_code=404, detail="Yedek bulunamadı.")
    return FileResponse(candidate, filename=name, media_type="application/octet-stream")
