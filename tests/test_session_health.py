import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.config import get_settings


class SessionHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["DATABASE_PATH"] = str(Path(self.temp_dir.name) / "health.db")
        get_settings.cache_clear()
        from app.database import get_connection, initialize_database, utc_now

        initialize_database()
        now = utc_now()
        self.session_ids = []
        with get_connection() as connection:
            for number in (1, 2):
                self.session_ids.append(connection.execute(
                    """
                    INSERT INTO telegram_sessions(
                        label, phone_masked, phone_encrypted, session_encrypted,
                        status, proxy_enabled, proxy_type, proxy_host, proxy_port,
                        created_at, updated_at
                    ) VALUES (?, '+90 ***', 'enc', 'session', 'active', 1,
                              'socks5', 'proxy.local', 1080, ?, ?)
                    """,
                    (f"Session {number}", now, now),
                ).lastrowid)

    def tearDown(self):
        get_settings.cache_clear()
        self.temp_dir.cleanup()

    def _add_batch(self, source_ref="@source", target_ref="@target") -> int:
        from app.database import get_connection, utc_now

        now = utc_now()
        with get_connection() as connection:
            batch_id = connection.execute(
                """
                INSERT INTO session_health_batches(
                    source_ref, target_ref, status, total_count, created_at, updated_at
                ) VALUES (?, ?, 'queued', 2, ?, ?)
                """,
                (source_ref, target_ref, now, now),
            ).lastrowid
            connection.executemany(
                """
                INSERT INTO session_health_items(batch_id, session_id, position, status)
                VALUES (?, ?, ?, 'queued')
                """,
                [
                    (batch_id, session_id, position)
                    for position, session_id in enumerate(self.session_ids, start=1)
                ],
            )
        return batch_id

    @staticmethod
    def _client():
        return SimpleNamespace(disconnect=AsyncMock())

    def test_ready_sessions_report_proxy_access_and_target_permission(self):
        from app.database import get_connection
        from app.session_health_service import execute_session_health_batch

        batch_id = self._add_batch()
        clients = [self._client(), self._client()]
        target = SimpleNamespace(
            creator=False,
            admin_rights=SimpleNamespace(invite_users=True),
        )
        membership_results = [
            (True, SimpleNamespace(), "Kaynak hazır"),
            (True, target, "Hedef hazır"),
            (True, SimpleNamespace(), "Kaynak hazır"),
            (True, target, "Hedef hazır"),
        ]
        with (
            patch("app.session_health_service._client_for", new=AsyncMock(side_effect=clients)),
            patch("app.session_health_service._inspect_membership", new=AsyncMock(side_effect=membership_results)),
        ):
            asyncio.run(execute_session_health_batch(batch_id))

        with get_connection() as connection:
            batch = connection.execute(
                "SELECT * FROM session_health_batches WHERE id=?", (batch_id,)
            ).fetchone()
            items = connection.execute(
                """
                SELECT status, proxy_ok, session_ok, source_access,
                       target_access, target_can_invite
                FROM session_health_items WHERE batch_id=? ORDER BY position
                """,
                (batch_id,),
            ).fetchall()
            locks = connection.execute("SELECT COUNT(*) count FROM session_operation_locks").fetchone()["count"]
        self.assertEqual(batch["status"], "completed")
        self.assertEqual(batch["ready_count"], 2)
        self.assertTrue(all(item["status"] == "ready" for item in items))
        self.assertTrue(all(item["target_can_invite"] == 1 for item in items))
        self.assertEqual(locks, 0)

    def test_busy_session_is_reported_without_interrupting_its_active_operation(self):
        from app.database import get_connection
        from app.session_health_service import execute_session_health_batch
        from app.session_operation import (
            acquire_session_operation,
            get_session_operation,
        )

        batch_id = self._add_batch(source_ref=None, target_ref=None)
        second_client = self._client()

        async def scenario():
            active = await acquire_session_operation(
                self.session_ids[0], "invite_job", "job:99", "JOB-99 üye ekleme"
            )
            try:
                with patch(
                    "app.session_health_service._client_for",
                    new=AsyncMock(return_value=second_client),
                ) as client_for:
                    await execute_session_health_batch(batch_id)
                self.assertEqual(client_for.await_count, 1)
                self.assertEqual(
                    get_session_operation(self.session_ids[0])["operation_key"], "job:99"
                )
            finally:
                await active.release()

        asyncio.run(scenario())

        with get_connection() as connection:
            items = connection.execute(
                "SELECT status, busy_operation FROM session_health_items WHERE batch_id=? ORDER BY position",
                (batch_id,),
            ).fetchall()
        self.assertEqual(items[0]["status"], "busy")
        self.assertIn("JOB-99", items[0]["busy_operation"])
        self.assertEqual(items[1]["status"], "ready")

    def test_proxy_failure_is_fail_closed_and_other_session_is_still_checked(self):
        from app.database import get_connection
        from app.session_health_service import execute_session_health_batch
        from app.telegram_service import ProxyUnavailableError

        batch_id = self._add_batch(source_ref=None, target_ref=None)
        second_client = self._client()
        with patch(
            "app.session_health_service._client_for",
            new=AsyncMock(side_effect=[ProxyUnavailableError("Proxy kapalı"), second_client]),
        ):
            asyncio.run(execute_session_health_batch(batch_id))

        with get_connection() as connection:
            items = connection.execute(
                "SELECT status, proxy_ok, reason FROM session_health_items WHERE batch_id=? ORDER BY position",
                (batch_id,),
            ).fetchall()
        self.assertEqual(items[0]["status"], "failed")
        self.assertEqual(items[0]["proxy_ok"], 0)
        self.assertIn("ana IP kullanılmadı", items[0]["reason"])
        self.assertEqual(items[1]["status"], "ready")

    def test_missing_target_permission_is_warning_not_a_destructive_action(self):
        from app.database import get_connection
        from app.session_health_service import execute_session_health_batch

        batch_id = self._add_batch(source_ref=None, target_ref="@target")
        target = SimpleNamespace(creator=False, admin_rights=None)
        with (
            patch(
                "app.session_health_service._client_for",
                new=AsyncMock(side_effect=[self._client(), self._client()]),
            ),
            patch(
                "app.session_health_service._inspect_membership",
                new=AsyncMock(return_value=(True, target, "Hedef hazır")),
            ),
        ):
            asyncio.run(execute_session_health_batch(batch_id))

        with get_connection() as connection:
            items = connection.execute(
                "SELECT status, target_can_invite FROM session_health_items WHERE batch_id=?",
                (batch_id,),
            ).fetchall()
        self.assertTrue(all(item["status"] == "attention" for item in items))
        self.assertTrue(all(item["target_can_invite"] == 0 for item in items))


if __name__ == "__main__":
    unittest.main()
