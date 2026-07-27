import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

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

    def test_automatic_selection_returns_all_sessions_in_round_robin_order(self):
        from app.database import set_app_setting
        from app.telegram_service import _activity_session_candidates

        first_id = self._add_session("Birinci")
        second_id = self._add_session("İkinci")
        self.assertEqual(_activity_session_candidates(None), [first_id, second_id])
        set_app_setting("activity_round_robin_cursor", str(first_id))
        self.assertEqual(_activity_session_candidates(None), [second_id, first_id])

    def test_access_error_keeps_the_real_telegram_error(self):
        from app.telegram_service import _activity_access_error

        error = _activity_access_error(None, [(7, RuntimeError("Proxy bağlantısı kurulamadı"))])

        self.assertIn("session 7", str(error))
        self.assertIn("Proxy bağlantısı kurulamadı", str(error))

    def test_automatic_scan_tries_the_next_session_after_access_error(self):
        from app.database import get_app_setting
        from app.telegram_service import scan_group_activity

        first_id = self._add_session("Birinci")
        second_id = self._add_session("İkinci")

        class FakeClient:
            def __init__(self):
                self.disconnect = AsyncMock()

            async def iter_messages(self, entity, limit):
                if False:
                    yield entity

        first_client = FakeClient()
        second_client = FakeClient()
        entity = type("Group", (), {"id": 123, "title": "Test grubu"})()

        async def client_for(session_id):
            return first_client if session_id == first_id else second_client

        async def resolve(client, session_id, reference):
            if session_id == first_id:
                raise RuntimeError("İlk session erişemedi")
            return entity

        scan = {
            "session_id": None,
            "group_ref": "@testgrubu",
            "window_hours": 24,
        }
        with (
            patch("app.telegram_service._client_for", side_effect=client_for),
            patch("app.telegram_service._resolve_or_request_group_access", side_effect=resolve),
            patch("app.telegram_service._record_activity_operation"),
        ):
            result = __import__("asyncio").run(scan_group_activity(scan))

        self.assertEqual(result["session_id"], second_id)
        first_client.disconnect.assert_awaited_once()
        second_client.disconnect.assert_awaited_once()
        self.assertEqual(get_app_setting("activity_round_robin_cursor"), str(second_id))

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
