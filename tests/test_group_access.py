import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.errors import FloodWaitError

from app.config import get_settings


class GroupAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["DATABASE_PATH"] = str(Path(self.temp_dir.name) / "group-access.db")
        get_settings.cache_clear()
        from app.database import get_connection, initialize_database, utc_now

        initialize_database()
        now = utc_now()
        self.session_ids = []
        with get_connection() as connection:
            for number in (1, 2):
                session_id = connection.execute(
                    """
                    INSERT INTO telegram_sessions(
                        label, phone_masked, phone_encrypted, session_encrypted,
                        display_name, status, proxy_enabled, created_at, updated_at
                    ) VALUES (?, '+90 ***', 'enc', 'session', ?, 'active', 1, ?, ?)
                    """,
                    (f"Session {number}", f"Session {number}", now, now),
                ).lastrowid
                self.session_ids.append(session_id)

    def tearDown(self):
        get_settings.cache_clear()
        self.temp_dir.cleanup()

    def _add_batch(self, *, purpose="source") -> int:
        from app.database import get_connection, utc_now

        now = utc_now()
        with get_connection() as connection:
            batch_id = connection.execute(
                """
                INSERT INTO group_access_batches(
                    group_ref, purpose, status, min_delay_seconds, max_delay_seconds,
                    total_count, created_at, updated_at
                ) VALUES ('@test_group', ?, 'queued', 0, 0, 2, ?, ?)
                """,
                (purpose, now, now),
            ).lastrowid
            connection.executemany(
                """
                INSERT INTO group_access_items(batch_id, session_id, position, status)
                VALUES (?, ?, ?, 'queued')
                """,
                [
                    (batch_id, session_id, position)
                    for position, session_id in enumerate(self.session_ids, start=1)
                ],
            )
        return batch_id

    @staticmethod
    def _client(session_id: int):
        return SimpleNamespace(session_id=session_id, disconnect=AsyncMock())

    def test_sessions_are_processed_sequentially_without_rescanning(self):
        from app.database import get_connection
        from app.group_access_service import execute_group_access_batch

        batch_id = self._add_batch()
        clients = {session_id: self._client(session_id) for session_id in self.session_ids}
        active_calls = 0
        max_active_calls = 0
        call_order = []

        async def client_for(session_id):
            call_order.append(("client", session_id))
            return clients[session_id]

        async def resolve(client, session_id, reference):
            nonlocal active_calls, max_active_calls
            self.assertEqual(reference, "@test_group")
            active_calls += 1
            max_active_calls = max(max_active_calls, active_calls)
            call_order.append(("resolve", session_id))
            await asyncio.sleep(0)
            active_calls -= 1
            return SimpleNamespace(title="Test Group", username="test_group", creator=False, admin_rights=None)

        with (
            patch("app.group_access_service._client_for", side_effect=client_for),
            patch("app.group_access_service._session_already_has_access", new=AsyncMock(return_value=False)),
            patch("app.group_access_service._resolve_or_request_group_access", side_effect=resolve),
            patch("app.group_access_service.utils.get_peer_id", return_value=-100123),
            patch("app.group_access_service.random.randint", return_value=0),
        ):
            asyncio.run(execute_group_access_batch(batch_id))

        self.assertEqual(max_active_calls, 1)
        self.assertEqual(
            [entry for entry in call_order if entry[0] == "resolve"],
            [("resolve", self.session_ids[0]), ("resolve", self.session_ids[1])],
        )
        with get_connection() as connection:
            batch = connection.execute(
                "SELECT * FROM group_access_batches WHERE id=?", (batch_id,)
            ).fetchone()
            items = connection.execute(
                "SELECT status FROM group_access_items WHERE batch_id=? ORDER BY position",
                (batch_id,),
            ).fetchall()
        self.assertEqual(batch["status"], "completed")
        self.assertEqual(batch["joined_count"], 2)
        self.assertEqual([item["status"] for item in items], ["joined", "joined"])
        for client in clients.values():
            client.disconnect.assert_awaited_once()

    def test_private_join_request_is_pending_and_queue_continues(self):
        from app.database import get_connection
        from app.group_access_service import execute_group_access_batch
        from app.telegram_service import GroupJoinPending

        batch_id = self._add_batch()
        clients = {session_id: self._client(session_id) for session_id in self.session_ids}

        async def resolve(client, session_id, reference):
            if session_id == self.session_ids[0]:
                raise GroupJoinPending(session_id, "Özel Grup")
            return SimpleNamespace(title="Özel Grup", username=None, creator=False, admin_rights=None)

        with (
            patch("app.group_access_service._client_for", side_effect=lambda session_id: clients[session_id]),
            patch("app.group_access_service._session_already_has_access", new=AsyncMock(return_value=False)),
            patch("app.group_access_service._resolve_or_request_group_access", side_effect=resolve),
            patch("app.group_access_service.utils.get_peer_id", return_value=-100456),
            patch("app.group_access_service.random.randint", return_value=0),
        ):
            asyncio.run(execute_group_access_batch(batch_id))

        with get_connection() as connection:
            rows = connection.execute(
                "SELECT status FROM group_access_items WHERE batch_id=? ORDER BY position",
                (batch_id,),
            ).fetchall()
            batch = connection.execute(
                "SELECT * FROM group_access_batches WHERE id=?", (batch_id,)
            ).fetchone()
        self.assertEqual([row["status"] for row in rows], ["approval_pending", "joined"])
        self.assertEqual(batch["pending_count"], 1)
        self.assertEqual(batch["status"], "completed")

    def test_flood_wait_stops_the_whole_queue_and_preserves_current_item(self):
        from app.database import get_connection
        from app.group_access_service import execute_group_access_batch

        batch_id = self._add_batch()
        first_client = self._client(self.session_ids[0])

        with (
            patch("app.group_access_service._client_for", new=AsyncMock(return_value=first_client)) as client_for,
            patch("app.group_access_service._session_already_has_access", new=AsyncMock(return_value=False)),
            patch(
                "app.group_access_service._resolve_or_request_group_access",
                new=AsyncMock(side_effect=FloodWaitError(request=None, capture=60)),
            ),
        ):
            asyncio.run(execute_group_access_batch(batch_id))

        with get_connection() as connection:
            batch = connection.execute(
                "SELECT * FROM group_access_batches WHERE id=?", (batch_id,)
            ).fetchone()
            items = connection.execute(
                "SELECT status FROM group_access_items WHERE batch_id=? ORDER BY position",
                (batch_id,),
            ).fetchall()
        self.assertEqual(batch["status"], "paused")
        self.assertIsNotNone(batch["next_action_at"])
        self.assertEqual([item["status"] for item in items], ["queued", "queued"])
        client_for.assert_awaited_once_with(self.session_ids[0])

    def test_proxy_failure_is_fail_closed_and_next_session_can_continue(self):
        from app.database import get_connection
        from app.group_access_service import execute_group_access_batch
        from app.telegram_service import ProxyUnavailableError

        batch_id = self._add_batch(purpose="target")
        second_client = self._client(self.session_ids[1])

        async def client_for(session_id):
            if session_id == self.session_ids[0]:
                raise ProxyUnavailableError("Proxy yanıt vermedi")
            return second_client

        entity = SimpleNamespace(
            title="Hedef Grup",
            username="hedef",
            creator=False,
            admin_rights=SimpleNamespace(invite_users=True),
        )
        with (
            patch("app.group_access_service._client_for", side_effect=client_for) as client_mock,
            patch("app.group_access_service._session_already_has_access", new=AsyncMock(return_value=True)),
            patch("app.group_access_service._resolve_or_request_group_access", new=AsyncMock(return_value=entity)),
            patch("app.group_access_service.utils.get_peer_id", return_value=-100789),
            patch("app.group_access_service.random.randint", return_value=0),
        ):
            asyncio.run(execute_group_access_batch(batch_id))

        with get_connection() as connection:
            items = connection.execute(
                """
                SELECT status, can_invite_users, reason
                FROM group_access_items WHERE batch_id=? ORDER BY position
                """,
                (batch_id,),
            ).fetchall()
        self.assertEqual(client_mock.await_count, 2)
        self.assertEqual(items[0]["status"], "failed")
        self.assertIn("ana IP kullanılmadı", items[0]["reason"])
        self.assertEqual(items[1]["status"], "already_member")
        self.assertEqual(items[1]["can_invite_users"], 1)


if __name__ == "__main__":
    unittest.main()
