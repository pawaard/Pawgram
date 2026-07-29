import asyncio
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
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
                    display_name, status, proxy_enabled, proxy_type, proxy_host,
                    proxy_port, proxy_last_status, created_at, updated_at
                ) VALUES (
                    ?, '+90 ***', 'encrypted', 'session', ?, 'active', 1,
                    'http', 'proxy.test', 10000, 'success', ?, ?
                )
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

    def test_invite_batch_wait_does_not_block_activity_scan(self):
        from app.database import get_connection
        from app.telegram_service import scan_group_activity

        session_id = self._add_session("Davet beklemesinde")
        cooldown_until = (datetime.now(UTC) + timedelta(minutes=20)).isoformat()
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE telegram_sessions
                SET status='batch_wait', batch_cooldown_until=?
                WHERE id=?
                """,
                (cooldown_until, session_id),
            )

        class FakeClient:
            def __init__(self):
                self.disconnect = AsyncMock()

            async def iter_messages(self, entity, limit):
                if False:
                    yield entity

        client = FakeClient()
        entity = type("Group", (), {"id": 123, "title": "Test grubu"})()
        scan = {
            "session_id": session_id,
            "group_ref": "@testgrubu",
            "window_hours": 24,
        }
        with (
            patch("app.telegram_service._client_for", new=AsyncMock(return_value=client)),
            patch(
                "app.telegram_service._resolve_or_request_group_access",
                new=AsyncMock(return_value=entity),
            ),
            patch("app.telegram_service._record_activity_operation"),
        ):
            result = asyncio.run(scan_group_activity(scan))

        self.assertEqual(result["session_id"], session_id)
        client.disconnect.assert_awaited_once()
        with get_connection() as connection:
            session = connection.execute(
                "SELECT status, batch_cooldown_until FROM telegram_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        self.assertEqual(session["status"], "batch_wait")
        self.assertEqual(session["batch_cooldown_until"], cooldown_until)

    def test_invite_flood_wait_does_not_block_preferred_activity_session(self):
        from app.database import get_connection
        from app.telegram_service import _activity_session_candidates

        preferred_id = self._add_session("Önceki tarama hesabı")
        ready_id = self._add_session("Hazır yedek")
        flood_until = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE telegram_sessions
                SET status='flood_wait', flood_wait_until=?
                WHERE id=?
                """,
                (flood_until, preferred_id),
            )

        self.assertEqual(_activity_session_candidates(preferred_id), [preferred_id])
        with get_connection() as connection:
            logs = connection.execute(
                """
                SELECT level, message, session_id
                FROM system_logs
                WHERE category='activity_selector'
                ORDER BY id
                """
            ).fetchall()

        self.assertTrue(
            any(
                row["session_id"] == preferred_id
                and "KABUL" in row["message"]
                and "aktivite taramasını önceden engellemez" in row["message"]
                for row in logs
            )
        )
        self.assertTrue(
            any(
                row["session_id"] == ready_id
                and "KABUL" in row["message"]
                and "aktivite kotası uygun" in row["message"]
                for row in logs
            )
        )
        self.assertTrue(any("tercih edilen session" in row["message"] for row in logs))

    def test_proxy_error_is_rejected_with_exact_reason(self):
        from app.database import get_connection
        from app.telegram_service import _activity_session_candidates

        failed_id = self._add_session("Proxy hatalı")
        ready_id = self._add_session("Hazır")
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE telegram_sessions
                SET status='proxy_error', proxy_last_status='failed',
                    proxy_last_error='407 authentication failed'
                WHERE id=?
                """,
                (failed_id,),
            )

        self.assertEqual(_activity_session_candidates(failed_id), [ready_id])
        with get_connection() as connection:
            failed_log = connection.execute(
                """
                SELECT message FROM system_logs
                WHERE category='activity_selector' AND session_id=?
                  AND message LIKE '%RED%'
                ORDER BY id DESC LIMIT 1
                """,
                (failed_id,),
            ).fetchone()
        self.assertIn("RED", failed_log["message"])
        self.assertIn("son proxy testi başarısız", failed_log["message"])
        self.assertIn("407 authentication failed", failed_log["message"])

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
        from app.telegram_service import (
            SessionBudgetWaiting,
            _activity_session_candidates,
        )

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

    def test_running_scan_task_is_deduplicated_and_can_be_cancelled(self):
        from app.activity_service import (
            SCAN_TASKS,
            cancel_activity_scan,
            start_activity_scan,
        )

        async def scenario():
            started = asyncio.Event()

            async def fake_scan(scan_id: int) -> None:
                self.assertEqual(scan_id, 42)
                started.set()
                await asyncio.Event().wait()

            with patch("app.activity_service.execute_activity_scan", new=fake_scan):
                first = start_activity_scan(42)
                second = start_activity_scan(42)
                self.assertIs(first, second)
                await started.wait()
                self.assertTrue(await cancel_activity_scan(42))
                await asyncio.sleep(0)
                self.assertTrue(first.cancelled())
                self.assertNotIn(42, SCAN_TASKS)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
