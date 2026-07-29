from __future__ import annotations

from datetime import UTC, datetime, time, timedelta


def parse_datetime(value: str) -> datetime:
    """Parse an ISO timestamp and return an aware UTC datetime.

    Browser clients send UTC offsets, while older database rows may be naive.
    Naive values are interpreted in the machine's local timezone.
    """

    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone(UTC)


def normalize_datetime(value: str) -> str:
    return parse_datetime(value).isoformat()


def next_working_time(
    working_start: str,
    working_end: str,
    now: datetime | None = None,
) -> datetime:
    """Return ``now`` when inside the local working window, else its next opening.

    Equal start/end values intentionally mean an all-day window. Windows that
    cross midnight (for example 22:00-06:00) are supported.
    """

    current_utc = (now or datetime.now(UTC)).astimezone(UTC)
    local_now = current_utc.astimezone()
    start_value = time.fromisoformat(working_start)
    end_value = time.fromisoformat(working_end)
    local_time = local_now.timetz().replace(tzinfo=None)

    if start_value == end_value:
        return current_utc
    if start_value < end_value:
        if start_value <= local_time < end_value:
            return current_utc
        opening_date = local_now.date() if local_time < start_value else local_now.date() + timedelta(days=1)
    else:
        if local_time >= start_value or local_time < end_value:
            return current_utc
        opening_date = local_now.date()

    opening_local = datetime.combine(opening_date, start_value, tzinfo=local_now.tzinfo)
    return opening_local.astimezone(UTC)


def next_job_run(job: dict, now: datetime | None = None) -> datetime:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    candidate = current
    scheduled_at = job.get("scheduled_at")
    if scheduled_at:
        candidate = max(candidate, parse_datetime(str(scheduled_at)))
    resume_at = job.get("resume_at")
    if resume_at:
        candidate = max(candidate, parse_datetime(str(resume_at)))
    return next_working_time(
        str(job.get("working_start") or "00:00"),
        str(job.get("working_end") or "23:59"),
        candidate,
    )
