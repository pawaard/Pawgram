import asyncio
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import get_settings


class HeartbeatServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["DATABASE_PATH"] = str(Path(self.temp_dir.name) / "heartbeat.db")
        get_settings.cache_clear()
        from app.database import get_connection, initialize_database, utc_now
        from app.session_operation import clear_stale_session_operations

        initialize_database()
        clear_stale_session_operations()
        now = utc_now()
        with get_connection() as connection:
            self.first_session_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, proxy_enabled, proxy_last_status,
                    created_at, updated_at
                ) VALUES (
                    'Heartbeat 1', '+90 ***', 'enc1', 'session1', 'Heartbeat 1',
                    'active', 1, 'success', ?, ?
                )
                """,
                (now, now),
            ).lastrowid
            self.second_session_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, proxy_enabled, proxy_last_status,
                    created_at, updated_at
                ) VALUES (
                    'Heartbeat 2', '+90 ***', 'enc2', 'session2', 'Heartbeat 2',
                    'active', 1, 'success', ?, ?
                )
                """,
                (now, now),
            ).lastrowid
            self.inactive_session_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, proxy_enabled, proxy_last_status,
                    created_at, updated_at
                ) VALUES (
                    'Heartbeat beklemede', '+90 ***', 'enc3', 'session3', 'Beklemede',
                    'flood_wait', 1, 'success', ?, ?
                )
                """,
                (now, now),
            ).lastrowid

    def tearDown(self):
        get_settings.cache_clear()
        self.temp_dir.cleanup()

    def _enable(self):
        from app.heartbeat_service import save_heartbeat_settings

        return save_heartbeat_settings(
            enabled=True,
            interval_minutes=60,
            group_id="-1001234567890",
            message_template="Merhabaa",
        )

    def test_cycle_sends_only_to_configured_group_and_continues_after_failure(self):
        from app.database import get_connection
        from app.heartbeat_service import HeartbeatService

        self._enable()
        first_client = AsyncMock()
        first_client.is_user_authorized.return_value = True
        first_client.send_message.side_effect = RuntimeError("Telegram gönderim hatası")
        second_client = AsyncMock()
        second_client.is_user_authorized.return_value = True
        clients = {
            self.first_session_id: first_client,
            self.second_session_id: second_client,
        }
        future = datetime.now(UTC) + timedelta(minutes=61)

        with patch(
            "app.heartbeat_service._client_for",
            new=AsyncMock(side_effect=lambda session_id, **_: clients[session_id]),
        ) as client_for, patch(
            "app.heartbeat_service.local_license_status",
            return_value={"required": False, "valid": True},
        ):
            processed = asyncio.run(HeartbeatService().run_due_cycle(now=future))

        self.assertEqual(processed, 2)
        first_client.send_message.assert_awaited_once_with(-1001234567890, "Merhabaa")
        second_client.send_message.assert_awaited_once_with(-1001234567890, "Merhabaa")
        first_client.disconnect.assert_awaited_once()
        second_client.disconnect.assert_awaited_once()
        self.assertEqual(client_for.await_count, 2)
        self.assertTrue(
            all(call.kwargs == {"mutate_session_state": False} for call in client_for.await_args_list)
        )

        with get_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM heartbeat_session_status ORDER BY session_id"
            ).fetchall()
            session_statuses = connection.execute(
                "SELECT id, status FROM telegram_sessions ORDER BY id"
            ).fetchall()
        heartbeat_by_id = {row["session_id"]: row for row in rows}
        self.assertEqual(heartbeat_by_id[self.first_session_id]["failure_count"], 1)
        self.assertEqual(heartbeat_by_id[self.first_session_id]["current_status"], "failed")
        self.assertEqual(heartbeat_by_id[self.second_session_id]["success_count"], 1)
        self.assertEqual(heartbeat_by_id[self.second_session_id]["current_status"], "success")
        self.assertEqual(heartbeat_by_id[self.inactive_session_id]["current_status"], "inactive")
        self.assertEqual(
            {row["id"]: row["status"] for row in session_statuses},
            {
                self.first_session_id: "active",
                self.second_session_id: "active",
                self.inactive_session_id: "flood_wait",
            },
        )

    def test_busy_invite_session_is_skipped_without_waiting_or_client_creation(self):
        from app.database import get_connection
        from app.heartbeat_service import HeartbeatService
        from app.session_operation import acquire_session_operation

        self._enable()
        future = datetime.now(UTC) + timedelta(minutes=61)
        client_for = AsyncMock()

        async def scenario():
            lease = await acquire_session_operation(
                self.first_session_id,
                "invite_job",
                "job:99",
                "JOB-99 üye ekleme",
                wait=False,
            )
            try:
                with patch("app.heartbeat_service._client_for", new=client_for), patch(
                    "app.heartbeat_service.local_license_status",
                    return_value={"required": False, "valid": True},
                ):
                    return await asyncio.wait_for(
                        HeartbeatService().run_due_cycle(now=future),
                        timeout=0.5,
                    )
            finally:
                await lease.release()

        processed = asyncio.run(scenario())
        self.assertEqual(processed, 2)
        self.assertEqual(client_for.await_count, 1)
        self.assertEqual(client_for.await_args.args[0], self.second_session_id)
        with get_connection() as connection:
            heartbeat = connection.execute(
                "SELECT * FROM heartbeat_session_status WHERE session_id=?",
                (self.first_session_id,),
            ).fetchone()
        self.assertEqual(heartbeat["current_status"], "skipped_busy")
        self.assertEqual(heartbeat["success_count"], 0)
        self.assertEqual(heartbeat["failure_count"], 0)
        self.assertIn("JOB-99", heartbeat["last_error"])

    def test_non_mutating_client_failure_preserves_session_status(self):
        from app.database import get_connection
        from app.telegram_service import ProxyUnavailableError, _client_for

        with get_connection() as connection:
            connection.execute(
                "UPDATE telegram_sessions SET proxy_enabled=0 WHERE id=?",
                (self.first_session_id,),
            )
        with (
            patch("app.telegram_service._credentials", return_value=(12345, "hash")),
            self.assertRaises(ProxyUnavailableError),
        ):
            asyncio.run(
                _client_for(
                    self.first_session_id,
                    mutate_session_state=False,
                )
            )
        with get_connection() as connection:
            session = connection.execute(
                "SELECT status, last_error FROM telegram_sessions WHERE id=?",
                (self.first_session_id,),
            ).fetchone()
        self.assertEqual(session["status"], "active")
        self.assertIsNone(session["last_error"])

    def test_settings_and_status_use_independent_heartbeat_storage(self):
        from app.database import get_connection
        from app.heartbeat_service import heartbeat_status

        settings = self._enable()
        overview = heartbeat_status()
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["interval_minutes"], 60)
        self.assertEqual(settings["group_id"], "-1001234567890")
        self.assertEqual(settings["message_template"], "Merhabaa")
        self.assertEqual(len(overview["sessions"]), 3)
        with get_connection() as connection:
            keys = connection.execute(
                "SELECT key FROM app_settings WHERE key LIKE 'heartbeat_%' ORDER BY key"
            ).fetchall()
            state_count = connection.execute(
                "SELECT COUNT(*) count FROM heartbeat_session_status"
            ).fetchone()["count"]
        self.assertEqual(
            [row["key"] for row in keys],
            [
                "heartbeat_enabled",
                "heartbeat_group_id",
                "heartbeat_interval_minutes",
                "heartbeat_message_template",
            ],
        )
        self.assertEqual(state_count, 3)


if __name__ == "__main__":
    unittest.main()
