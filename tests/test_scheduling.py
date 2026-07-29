import unittest
from datetime import UTC, datetime

from app.scheduling import next_job_run, next_working_time, normalize_datetime


class SchedulingTests(unittest.TestCase):
    def test_iso_timestamps_are_normalized_to_utc(self):
        self.assertEqual(
            normalize_datetime("2026-07-28T15:30:00+03:00"),
            "2026-07-28T12:30:00+00:00",
        )

    def test_equal_working_hours_mean_all_day(self):
        now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
        self.assertEqual(next_working_time("00:00", "00:00", now), now)

    def test_future_schedule_and_working_window_are_both_respected(self):
        now = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
        future = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
        job = {
            "scheduled_at": future.isoformat(),
            "resume_at": None,
            "working_start": "00:00",
            "working_end": "00:00",
        }
        self.assertEqual(next_job_run(job, now), future)

    def test_overnight_window_accepts_late_local_time(self):
        local_zone = datetime.now().astimezone().tzinfo
        local_now = datetime(2026, 7, 28, 23, 0, tzinfo=local_zone)
        now = local_now.astimezone(UTC)
        self.assertEqual(next_working_time("22:00", "06:00", now), now)


if __name__ == "__main__":
    unittest.main()
