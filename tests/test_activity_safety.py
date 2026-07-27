import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings


class ActivitySafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["DATABASE_PATH"] = str(Path(self.temp_dir.name) / "safety.db")
        get_settings.cache_clear()
        from app.database import initialize_database

        initialize_database()

    def tearDown(self):
        get_settings.cache_clear()
        self.temp_dir.cleanup()

    def _add_session(self, label: str) -> int:
        from app.database import get_connection, utc_now

        now = utc_now()
        with get_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, created_at, updated_at
                ) VALUES (?, '+90 ***', 'encrypted', 'session', ?, 'active', ?, ?)
                """,
                (label, label, now, now),
            )
        return cursor.lastrowid

    def test_private_invite_links_are_recognized(self):
        from app.telegram_service import _private_invite_hash

        self.assertEqual(_private_invite_hash("https://t.me/+Abc_123-xyz"), "Abc_123-xyz")
        self.assertEqual(
            _private_invite_hash("https://telegram.me/joinchat/InviteHash"),
            "InviteHash",
        )
        self.assertIsNone(_private_invite_hash("@public_group"))

    def test_round_robin_skips_session_at_fixed_quota(self):
        from app.database import get_connection, set_app_setting, utc_now
        from app.telegram_service import _activity_session_candidates

        limited_id = self._add_session("Limite yakın")
        ready_id = self._add_session("Hazır")
        today = datetime.now(UTC).date().isoformat()
        set_app_setting("activity_daily_quota", "10")
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO session_usage_daily(session_id, usage_date, operation_count, last_used_at)
                VALUES (?, ?, 10, ?), (?, ?, 3, ?)
                """,
                (limited_id, today, utc_now(), ready_id, today, utc_now()),
            )

        self.assertEqual(_activity_session_candidates(None), [ready_id])

    def test_round_robin_advances_before_each_new_operation(self):
        from app.telegram_service import _activity_session_candidates

        first_id = self._add_session("Birinci")
        second_id = self._add_session("İkinci")
        self.assertEqual(_activity_session_candidates(None), [first_id])
        self.assertEqual(_activity_session_candidates(None), [second_id])

    def test_all_sessions_at_fixed_quota_wait_safely(self):
        from app.database import get_connection, set_app_setting, utc_now
        from app.telegram_service import SessionBudgetWaiting, _activity_session_candidates

        session_id = self._add_session("Bekleyen")
        today = datetime.now(UTC).date().isoformat()
        set_app_setting("activity_daily_quota", "10")
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO session_usage_daily(session_id, usage_date, operation_count, last_used_at)
                VALUES (?, ?, 10, ?)
                """,
                (session_id, today, utc_now()),
            )

        with self.assertRaises(SessionBudgetWaiting):
            _activity_session_candidates(None)


if __name__ == "__main__":
    unittest.main()
