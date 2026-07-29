"""Run real Telegram proxy workflow checks without sending any invite."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--scan-hours", default=1, type=int)
    parser.add_argument("--max-candidates", default=5, type=int)
    parser.add_argument("--simulate-invite-ready", action="store_true")
    return parser.parse_args()


async def verify(
    scan_hours: int,
    max_candidates: int,
    simulate_invite_ready: bool,
) -> dict:
    from app.database import get_connection
    from app.telegram_service import (
        _close_invite_session,
        _open_invite_session,
        preview_job_candidates,
        scan_group_activity,
        select_next_available_session,
    )

    result: dict[str, object] = {
        "activity": None,
        "preparation": None,
        "round_robin": None,
        "invite_preflight": None,
    }
    with get_connection() as connection:
        scan_row = connection.execute(
            """
            SELECT * FROM activity_scans
            WHERE status='completed'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        job_row = connection.execute(
            """
            SELECT * FROM transfer_jobs
            WHERE target_ref IS NOT NULL
            ORDER BY CASE WHEN status='approved' THEN 0 ELSE 1 END, id DESC
            LIMIT 1
            """
        ).fetchone()
    if scan_row is None or job_row is None:
        raise RuntimeError("Doğrulama için tamamlanmış tarama ve aktarım işi bulunamadı.")

    scan = dict(scan_row)
    scan["id"] = 900001
    scan["name"] = "Proxy workflow verification"
    scan["window_hours"] = max(1, scan_hours)
    try:
        activity = await scan_group_activity(scan)
        result["activity"] = {
            "ok": True,
            "session_id": activity["session_id"],
            "message_count": activity["message_count"],
            "author_count": len(activity["authors"]),
            "group_resolved": bool(activity.get("group_id")),
        }
    except Exception as error:  # noqa: BLE001 - diagnostic boundary
        result["activity"] = {
            "ok": False,
            "error": (str(error).strip() or error.__class__.__name__)[:400],
        }

    job = dict(job_row)
    job["max_users"] = min(
        max(1, max_candidates),
        max(1, int(job.get("max_users") or max_candidates)),
    )
    try:
        preparation = await preview_job_candidates(job)
        result["preparation"] = {
            "ok": True,
            "eligible": preparation["eligible"],
            "scanned": preparation["scanned"],
            "target_members_checked": preparation["target_members_checked"],
            "source_admins_excluded": preparation["source_admins_excluded"],
            "can_invite_users": preparation["permissions"]["can_invite_users"],
        }
    except Exception as error:  # noqa: BLE001 - diagnostic boundary
        result["preparation"] = {
            "ok": False,
            "error": (str(error).strip() or error.__class__.__name__)[:400],
        }

    try:
        if simulate_invite_ready:
            with get_connection() as connection:
                connection.execute(
                    """
                    UPDATE telegram_sessions
                    SET status='active', flood_wait_until=NULL,
                        batch_cooldown_until=NULL, last_error=NULL
                    WHERE proxy_enabled=1 AND proxy_last_status='success'
                    """
                )
        with get_connection() as connection:
            candidates = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM job_candidates
                    WHERE job_id=? AND status='eligible'
                    ORDER BY id LIMIT ?
                    """,
                    (job["id"], max(1, max_candidates)),
                ).fetchall()
            ]
            usage_date = datetime.now(UTC).date().isoformat()
            daily_limit = max(1, int(job.get("daily_limit") or 50))
            first = select_next_available_session(
                connection,
                None,
                usage_date,
                daily_limit,
                preferred_session_id=job.get("session_id"),
                working_start=job.get("working_start") or "00:00",
                working_end=job.get("working_end") or "23:59",
                job_id=job["id"],
            )
            next_selection = select_next_available_session(
                connection,
                first.session_id,
                usage_date,
                daily_limit,
                working_start=job.get("working_start") or "00:00",
                working_end=job.get("working_end") or "23:59",
                job_id=job["id"],
            )
        result["round_robin"] = {
            "ok": bool(
                first.session_id
                and next_selection.session_id
                and first.session_id != next_selection.session_id
            ),
            "first_session_id": first.session_id,
            "next_session_id": next_selection.session_id,
            "immediate_handoff": bool(next_selection.session_id),
            "simulated_wait_expiry": simulate_invite_ready,
        }

        context = await _open_invite_session(first, job, candidates, job["id"])
        try:
            result["invite_preflight"] = {
                "ok": True,
                "session_id": context.session_id,
                "target_resolved": bool(context.target),
                "can_invite_users": True,
                "source_context_ready": (
                    context.source_input is not None if candidates else True
                ),
                "candidate_count": len(candidates),
                "invite_sent": False,
            }
        finally:
            await _close_invite_session(context)
    except Exception as error:  # noqa: BLE001 - diagnostic boundary
        result["invite_preflight"] = {
            "ok": False,
            "error": (str(error).strip() or error.__class__.__name__)[:400],
            "invite_sent": False,
        }
    return result


def main() -> None:
    args = parse_args()
    database = args.database.resolve()
    if not database.is_file():
        raise SystemExit("Veritabanı bulunamadı.")
    if database.name.lower() == "console.db":
        raise SystemExit("Bu doğrulama yalnızca console.db kopyasında çalıştırılabilir.")
    os.environ["DATABASE_PATH"] = str(database)
    print(
        json.dumps(
            asyncio.run(
                verify(
                    args.scan_hours,
                    args.max_candidates,
                    args.simulate_invite_ready,
                )
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
