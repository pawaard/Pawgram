from contextlib import asynccontextmanager
import asyncio
import csv
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
import sqlite3

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import APP_DIR, RESOURCE_DIR, get_settings
from app.activity_service import activity_scheduler_loop, execute_activity_scan, stop_scheduler
from app.database import add_log, add_notification, get_app_setting, get_connection, initialize_database, set_app_setting, utc_now
from app.licensing import activate_license, license_refresh_loop, local_license_status, refresh_license
from app.schemas import ActivityScanRequest, AdminPasswordRequest, CandidateSelectionRequest, GroupResolveRequest, JobCreateRequest, LicenseActivationRequest, LoginStartRequest, LoginVerifyRequest, ProxySettingsRequest, RotationSettingsRequest, TelegramSettingsRequest
from app.security import create_auth_token, decrypt, encrypt, hash_password, verify_auth_token, verify_password
from app.telegram_service import execute_invite_job, list_groups, preview_job_candidates, resolve_group, start_login, test_session_proxy, verify_login


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    scheduler_task = asyncio.create_task(activity_scheduler_loop())
    license_task = asyncio.create_task(license_refresh_loop())
    try:
        yield
    finally:
        await stop_scheduler(scheduler_task)
        await stop_scheduler(license_task)


settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.2.0", lifespan=lifespan)
JOB_TASKS: set[asyncio.Task] = set()
static_dir = RESOURCE_DIR / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

PUBLIC_API_PATHS = {
    "/api/health",
    "/api/auth/status",
    "/api/auth/setup",
    "/api/auth/login",
    "/api/license/status",
    "/api/license/activate",
}


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
    if row["flood_wait_until"]:
        try:
            wait_seconds = max(
                0,
                int((datetime.fromisoformat(row["flood_wait_until"]) - datetime.now(UTC)).total_seconds()),
            )
        except ValueError:
            wait_seconds = 0
    status = "active" if row["status"] == "flood_wait" and wait_seconds == 0 else row["status"]
    health_score = 100 if status == "active" else 55 if status == "flood_wait" else 25
    return {
        "id": row["id"],
        "label": row["label"],
        "phone_masked": row["phone_masked"],
        "telegram_user_id": row["telegram_user_id"],
        "display_name": row["display_name"],
        "username": row["username"],
        "status": status,
        "health_score": health_score,
        "flood_wait_seconds": wait_seconds,
        "flood_wait_until": row["flood_wait_until"],
        "last_error": row["last_error"],
        "proxy_enabled": bool(row.get("proxy_enabled")),
        "proxy_type": row.get("proxy_type"),
        "proxy_host": row.get("proxy_host"),
        "proxy_port": row.get("proxy_port"),
        "proxy_last_status": row.get("proxy_last_status"),
        "proxy_latency_ms": row.get("proxy_latency_ms"),
        "proxy_last_test_at": row.get("proxy_last_test_at"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


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
        "app": settings.app_name,
        "telegram_configured": settings.telegram_configured or panel_configured,
        "telegram_config_source": "environment" if settings.telegram_configured else "panel" if panel_configured else None,
        "environment": settings.app_env,
        "license": {key: value for key, value in local_license_status().items() if key != "lease_token"},
    }


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
    return {
        "configured": configured,
        "authenticated": not configured or verify_auth_token(request.cookies.get("pawgram_session")),
    }


@app.post("/api/auth/setup")
async def auth_setup(payload: AdminPasswordRequest, response: Response):
    if get_app_setting("admin_password_hash"):
        raise HTTPException(status_code=409, detail="Yönetici parolası zaten oluşturulmuş.")
    set_app_setting("admin_password_hash", hash_password(payload.password))
    response.set_cookie(
        "pawgram_session",
        create_auth_token(),
        httponly=True,
        samesite="strict",
        secure=settings.app_env == "production",
        max_age=86400,
    )
    add_log("success", "security", "Yönetici parolası oluşturuldu")
    add_notification("success", "Pawgram koruma altında", "Yönetici parolası başarıyla oluşturuldu.", "settings")
    return {"ok": True}


@app.post("/api/auth/login")
async def auth_login(payload: AdminPasswordRequest, response: Response):
    stored = get_app_setting("admin_password_hash")
    if not stored or not verify_password(payload.password, stored):
        raise HTTPException(status_code=401, detail="Yönetici parolası hatalı.")
    response.set_cookie(
        "pawgram_session",
        create_auth_token(),
        httponly=True,
        samesite="strict",
        secure=settings.app_env == "production",
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
    api_configured = settings.telegram_configured or bool(
        get_app_setting("telegram_api_id") and get_app_setting("telegram_api_hash_encrypted")
    )
    return {
        "admin_configured": bool(get_app_setting("admin_password_hash")),
        "api_configured": api_configured,
        "session_configured": session_count > 0,
        "complete": bool(get_app_setting("admin_password_hash")) and api_configured and session_count > 0,
    }


@app.get("/api/settings/telegram")
async def telegram_settings():
    panel_api_id = get_app_setting("telegram_api_id")
    configured = settings.telegram_configured or bool(
        panel_api_id and get_app_setting("telegram_api_hash_encrypted")
    )
    return {
        "configured": configured,
        "source": "environment" if settings.telegram_configured else "panel" if configured else None,
        "api_id": settings.telegram_api_id if settings.telegram_configured else int(panel_api_id) if panel_api_id else None,
        "api_hash_masked": "••••••••••••••••••••••••••••••••" if configured else None,
    }


@app.post("/api/settings/telegram")
async def save_telegram_settings(payload: TelegramSettingsRequest):
    if settings.telegram_configured:
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
    session_counts = {row["status"]: row["count"] for row in sessions}
    job_counts = {row["status"]: row["count"] for row in jobs}
    return {
        "sessions_total": sum(session_counts.values()),
        "sessions_active": session_counts.get("active", 0),
        "sessions_waiting": session_counts.get("flood_wait", 0),
        "jobs_total": sum(job_counts.values()),
        "jobs_active": job_counts.get("running", 0),
        "processed": totals["processed"],
        "succeeded": totals["succeeded"],
    }


@app.get("/api/activity-scans")
async def activity_scans():
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT a.*, s.label session_label, s.phone_masked
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
    add_log("success", "activity", f"Aktivite taraması oluşturuldu: {payload.name}")
    add_notification("info", "Aktivite taraması sırada", f"{payload.name} otomatik kuyruğa eklendi.", "activity")
    return {"ok": True, "scan_id": scan_id, "status": "queued"}


def get_activity_scan_or_404(scan_id: int) -> dict:
    with get_connection() as connection:
        scan = connection.execute("SELECT * FROM activity_scans WHERE id = ?", (scan_id,)).fetchone()
    if not scan:
        raise HTTPException(status_code=404, detail="Aktivite taraması bulunamadı.")
    return scan


@app.post("/api/activity-scans/{scan_id}/run")
async def run_activity_scan(scan_id: int):
    get_activity_scan_or_404(scan_id)
    with get_connection() as connection:
        connection.execute(
            "UPDATE activity_scans SET status='queued', next_run_at=?, last_error=NULL, updated_at=? WHERE id=?",
            (utc_now(), utc_now(), scan_id),
        )
    asyncio.create_task(execute_activity_scan(scan_id))
    return {"ok": True, "status": "queued"}


@app.post("/api/activity-scans/{scan_id}/pause")
async def pause_activity_scan(scan_id: int):
    get_activity_scan_or_404(scan_id)
    with get_connection() as connection:
        connection.execute(
            "UPDATE activity_scans SET status='paused', updated_at=? WHERE id=?",
            (utc_now(), scan_id),
        )
    return {"ok": True, "status": "paused"}


@app.post("/api/activity-scans/{scan_id}/resume")
async def resume_activity_scan(scan_id: int):
    get_activity_scan_or_404(scan_id)
    with get_connection() as connection:
        connection.execute(
            "UPDATE activity_scans SET status='queued', next_run_at=?, updated_at=? WHERE id=?",
            (utc_now(), utc_now(), scan_id),
        )
    return {"ok": True, "status": "queued"}


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


@app.get("/api/activity-scans/{scan_id}/report.csv")
async def activity_scan_report(scan_id: int):
    scan = get_activity_scan_or_404(scan_id)
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
    with get_connection() as connection:
        rows = connection.execute("SELECT * FROM telegram_sessions ORDER BY id DESC").fetchall()
    return [public_session(row) for row in rows]


@app.post("/api/sessions/login/start")
async def login_start(payload: LoginStartRequest):
    try:
        return await start_login(payload.phone, payload.label)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/sessions/login/verify")
async def login_verify(payload: LoginVerifyRequest):
    try:
        return await verify_login(payload.phone, payload.code, payload.password)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def get_session_or_404(session_id: int) -> dict:
    with get_connection() as connection:
        session = connection.execute(
            "SELECT * FROM telegram_sessions WHERE id = ?", (session_id,)
        ).fetchone()
    if not session:
        raise HTTPException(status_code=404, detail="Telegram session bulunamadı.")
    return session


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
    if job["status"] in {"approved", "running", "paused_quota", "flood_wait", "completed", "failed"}:
        raise HTTPException(status_code=409, detail="Çalışan veya tamamlanan işin aday seçimi değiştirilemez.")
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
            connection.execute(
                f"UPDATE job_candidates SET selected=1 WHERE job_id=? AND id IN ({placeholders})",
                (job_id, *sorted(requested)),
            )
    return {"ok": True, "selected_count": len(requested)}


@app.post("/api/jobs/{job_id}/approve")
async def approve_job(job_id: int):
    job = get_job_or_404(job_id)
    if not job["previewed_at"]:
        raise HTTPException(status_code=400, detail="İş onaylanmadan önce aday önizlemesi tamamlanmalı.")
    with get_connection() as connection:
        selected_count = connection.execute(
            "SELECT COUNT(*) count FROM job_candidates WHERE job_id=? AND status='eligible' AND selected=1",
            (job_id,),
        ).fetchone()["count"]
    if selected_count < 1:
        raise HTTPException(status_code=400, detail="Onaylamadan önce rızası ve üyeliği doğrulanmış en az bir aday seçin.")
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
    if job["status"] not in {"approved", "paused_quota"}:
        raise HTTPException(status_code=409, detail="Yalnızca onaylı veya kota nedeniyle duraklatılmış işler başlatılabilir.")
    with get_connection() as connection:
        selected_count = connection.execute(
            "SELECT COUNT(*) count FROM job_candidates WHERE job_id=? AND selected=1 AND status='eligible'",
            (job_id,),
        ).fetchone()["count"]
    if selected_count < 1:
        raise HTTPException(status_code=400, detail="İşlenecek seçili aday kalmadı.")
    task = asyncio.create_task(execute_invite_job(job_id))
    JOB_TASKS.add(task)
    task.add_done_callback(JOB_TASKS.discard)
    return {"ok": True, "status": "starting", "selected_count": selected_count}


@app.get("/api/jobs/{job_id}/report.csv")
async def job_report_csv(job_id: int):
    job = get_job_or_404(job_id)
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


@app.get("/api/logs")
async def logs(limit: int = 100):
    safe_limit = max(1, min(limit, 500))
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM system_logs ORDER BY id DESC LIMIT ?", (safe_limit,)
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


@app.get("/api/backups")
async def backups():
    backup_dir = APP_DIR / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    return [
        {"name": item.name, "size": item.stat().st_size, "created_at": datetime.fromtimestamp(item.stat().st_mtime, UTC).isoformat()}
        for item in sorted(backup_dir.glob("pawgram-*.db"), key=lambda value: value.stat().st_mtime, reverse=True)
    ]


@app.post("/api/backups")
async def create_backup():
    backup_dir = APP_DIR / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    name = f"pawgram-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.db"
    destination = backup_dir / name
    source_connection = sqlite3.connect(settings.database_path)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    add_log("success", "backup", f"Veritabanı yedeği oluşturuldu: {name}")
    add_notification("success", "Yedekleme tamamlandı", f"{name} güvenli biçimde oluşturuldu.", "settings")
    return {"ok": True, "name": name, "size": destination.stat().st_size}


@app.get("/api/backups/{name}")
async def download_backup(name: str):
    backup_dir = (APP_DIR / "data" / "backups").resolve()
    candidate = (backup_dir / name).resolve()
    if candidate.parent != backup_dir or not candidate.is_file() or not name.startswith("pawgram-"):
        raise HTTPException(status_code=404, detail="Yedek bulunamadı.")
    return FileResponse(candidate, filename=name, media_type="application/octet-stream")
