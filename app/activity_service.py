import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from telethon.errors import FloodWaitError

from app.database import add_log, add_notification, get_connection, utc_now
from app.licensing import local_license_status
from app.telegram_service import (
    GroupJoinPending,
    SessionBudgetWaiting,
    scan_group_activity,
)

RUNNING_SCANS: set[int] = set()
SCAN_TASKS: dict[int, asyncio.Task[None]] = {}


def start_activity_scan(scan_id: int) -> asyncio.Task[None]:
    existing = SCAN_TASKS.get(scan_id)
    if existing is not None and not existing.done():
        return existing

    task = asyncio.create_task(execute_activity_scan(scan_id))
    SCAN_TASKS[scan_id] = task

    def forget(completed: asyncio.Task[None]) -> None:
        if SCAN_TASKS.get(scan_id) is completed:
            SCAN_TASKS.pop(scan_id, None)

    task.add_done_callback(forget)
    return task


async def cancel_activity_scan(scan_id: int) -> bool:
    task = SCAN_TASKS.get(scan_id)
    if task is None or task.done():
        return False
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    return True


async def stop_activity_scans() -> None:
    tasks = [task for task in SCAN_TASKS.values() if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    SCAN_TASKS.clear()
    RUNNING_SCANS.clear()


async def execute_activity_scan(scan_id: int) -> None:
    license_state = local_license_status()
    if license_state["required"] and not license_state["valid"]:
        add_log("warning", "license", "Aktivite işi geçerli lisans olmadığı için bekletildi")
        return
    if scan_id in RUNNING_SCANS:
        return
    RUNNING_SCANS.add(scan_id)
    try:
        with get_connection() as connection:
            scan = connection.execute(
                "SELECT * FROM activity_scans WHERE id = ?", (scan_id,)
            ).fetchone()
            if not scan:
                return
            connection.execute(
                "UPDATE activity_scans SET status='running', last_error=NULL, updated_at=? WHERE id=?",
                (utc_now(), scan_id),
            )
        result = await scan_group_activity(scan)
        now = datetime.now(UTC)
        next_run = (
            now + timedelta(minutes=scan["interval_minutes"])
            if scan["recurring"]
            else None
        )
        rows = [
            (
                scan_id,
                author["telegram_user_id"],
                author["display_name"],
                author["username"],
                author["access_hash"],
                author.get("source_message_id"),
                author["message_count"],
                author["last_message_at"].isoformat(),
                now.isoformat(),
            )
            for author in result["authors"]
        ]
        with get_connection() as connection:
            connection.execute("DELETE FROM activity_results WHERE scan_id = ?", (scan_id,))
            connection.executemany(
                """
                INSERT INTO activity_results(
                    scan_id, telegram_user_id, display_name, username,
                    access_hash, source_message_id, message_count, last_message_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            connection.execute(
                """
                UPDATE activity_scans
                SET session_id=?, group_id=?, group_title=?, status=?, next_run_at=?,
                    last_run_at=?, message_count=?, unique_users=?, access_status='member',
                    joined_at=COALESCE(joined_at, ?), last_error=NULL, updated_at=?
                WHERE id=?
                """,
                (
                    result["session_id"], result["group_id"], result["group_title"],
                    "scheduled" if scan["recurring"] else "completed",
                    next_run.isoformat() if next_run else None,
                    now.isoformat(), result["message_count"], len(rows), now.isoformat(),
                    now.isoformat(), scan_id,
                ),
            )
        add_log(
            "success",
            "activity",
            f"{result['message_count']} mesaj tarandı, {len(rows)} benzersiz kullanıcı bulundu",
            result["session_id"],
        )
        add_notification(
            "success",
            "Aktivite taraması tamamlandı",
            f"{result['group_title']}: {len(rows)} aktif kullanıcı bulundu.",
            "activity",
        )
    except GroupJoinPending as error:
        retry_at = datetime.now(UTC) + timedelta(minutes=5)
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE activity_scans
                SET session_id=?, group_title=?, status='waiting_join', access_status='requested',
                    join_requested_at=COALESCE(join_requested_at, ?), next_run_at=?,
                    last_error=?, updated_at=?
                WHERE id=?
                """,
                (
                    error.session_id,
                    error.group_title,
                    utc_now(),
                    retry_at.isoformat(),
                    str(error),
                    utc_now(),
                    scan_id,
                ),
            )
        add_log("info", "activity", str(error), error.session_id)
        add_notification("info", "Grup katılım isteği gönderildi", str(error), "activity")
    except SessionBudgetWaiting as error:
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE activity_scans
                SET status='waiting_budget', next_run_at=?, last_error=?, updated_at=?
                WHERE id=?
                """,
                (error.wait_until.isoformat(), str(error), utc_now(), scan_id),
            )
        add_log("info", "activity", str(error))
        add_notification("info", "Güvenli kullanım bütçesi bekleniyor", str(error), "activity")
    except FloodWaitError as error:
        wait_until = getattr(
            error,
            "wait_until",
            datetime.now(UTC) + timedelta(seconds=error.seconds),
        )
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE activity_scans
                SET status='waiting', next_run_at=?, last_error=?, updated_at=?
                WHERE id=?
                """,
                (
                    wait_until.isoformat(),
                    f"FloodWait: {error.seconds} saniye",
                    utc_now(),
                    scan_id,
                ),
            )
    except Exception as error:  # noqa: BLE001 - task boundary persists every unexpected failure
        with get_connection() as connection:
            connection.execute(
                "UPDATE activity_scans SET status='error', last_error=?, updated_at=? WHERE id=?",
                (str(error), utc_now(), scan_id),
            )
        add_log("error", "activity", str(error))
        add_notification("error", "Aktivite taraması başarısız", str(error), "activity")
    finally:
        RUNNING_SCANS.discard(scan_id)


async def activity_scheduler_loop() -> None:
    while True:
        license_state = local_license_status()
        if license_state["required"] and not license_state["valid"]:
            await asyncio.sleep(5)
            continue
        now = utc_now()
        with get_connection() as connection:
            scans = connection.execute(
                """
                SELECT id FROM activity_scans
                WHERE status IN ('queued', 'scheduled', 'waiting', 'waiting_join', 'waiting_budget')
                  AND (next_run_at IS NULL OR next_run_at <= ?)
                ORDER BY COALESCE(next_run_at, created_at), id
                LIMIT 3
                """,
                (now,),
            ).fetchall()
        for scan in scans:
            if scan["id"] not in RUNNING_SCANS:
                start_activity_scan(scan["id"])
        await asyncio.sleep(5)


async def stop_scheduler(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
