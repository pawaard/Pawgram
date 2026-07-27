import asyncio
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.tl.types import Chat, ChatParticipantAdmin, ChatPhotoEmpty, User

from app.config import get_settings


class PreviewFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["DATABASE_PATH"] = str(Path(self.temp_dir.name) / "preview.db")
        get_settings.cache_clear()
        from app.database import get_connection, initialize_database, utc_now

        initialize_database()
        now = utc_now()
        with get_connection() as connection:
            session = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, created_at, updated_at
                ) VALUES ('Test', '+90 ***', 'enc', 'session', 'Test', 'active', ?, ?)
                """,
                (now, now),
            ).lastrowid
            previous_job = connection.execute(
                """
                INSERT INTO transfer_jobs(name, session_id, source_ref, target_ref, status, created_at, updated_at)
                VALUES ('Önceki', ?, '@a', '@b', 'approved', ?, ?)
                """,
                (session, now, now),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO member_history(
                    telegram_user_id, display_name, first_job_id, status, first_seen_at, last_seen_at
                ) VALUES (2, 'Önceki Kullanıcı', ?, 'approved', ?, ?)
                """,
                (previous_job, now, now),
            )
            self.job_id = connection.execute(
                """
                INSERT INTO transfer_jobs(
                    name, session_id, source_ref, source_id, source_title,
                    target_ref, target_id, target_title, status, max_users, created_at, updated_at
                ) VALUES ('Yeni', ?, '@source', 100, 'Kaynak', '@target', 200, 'Hedef', 'ready', 10, ?, ?)
                """,
                (session, now, now),
            ).lastrowid
            self.session_id = session

    def tearDown(self):
        get_settings.cache_clear()
        self.temp_dir.cleanup()

    def test_admins_and_previous_users_are_excluded(self):
        from app.database import get_connection
        from app.telegram_service import preview_job_candidates

        source = Chat(100, "Kaynak", ChatPhotoEmpty(), 3, datetime.now(UTC), 1)
        target = Chat(200, "Hedef", ChatPhotoEmpty(), 0, datetime.now(UTC), 1, creator=True)
        users = (
            User(1, first_name="Yönetici", access_hash=101),
            User(2, first_name="Geçmiş", access_hash=102),
            User(3, first_name="Uygun", access_hash=103),
        )

        class Message:
            def __init__(self, user):
                self.sender_id = user.id
                self.id = 800 + user.id
                self.user = user

            async def get_sender(self):
                return self.user

        class FakeClient:
            async def iter_participants(self, entity, limit=None, filter=None):
                if False:
                    yield entity

            async def iter_messages(self, entity, limit=None):
                for user in users:
                    yield Message(user)

            async def __call__(self, request):
                return SimpleNamespace(
                    full_chat=SimpleNamespace(
                        participants=SimpleNamespace(
                            participants=[ChatParticipantAdmin(1, 99, datetime.now(UTC))]
                        )
                    )
                )

            async def disconnect(self):
                return None

        with get_connection() as connection:
            job = connection.execute(
                "SELECT * FROM transfer_jobs WHERE id=?", (self.job_id,)
            ).fetchone()

        fake_client = FakeClient()
        with patch("app.telegram_service._client_for", new=AsyncMock(return_value=fake_client)), patch(
            "app.telegram_service._resolve_entity",
            new=AsyncMock(side_effect=[source, target]),
        ):
            summary = asyncio.run(preview_job_candidates(job))

        self.assertEqual(summary["admin"], 1)
        self.assertEqual(summary["previously_used"], 1)
        self.assertEqual(summary["eligible"], 1)
        with get_connection() as connection:
            statuses = {
                row["telegram_user_id"]: row["status"]
                for row in connection.execute(
                    "SELECT telegram_user_id, status FROM job_candidates WHERE job_id=?",
                    (self.job_id,),
                ).fetchall()
            }
        self.assertEqual(statuses, {2: "previously_used", 3: "eligible"})
        self.assertNotIn(1, statuses)

    def test_hidden_member_list_is_supplemented_from_message_authors(self):
        from app.database import get_connection
        from app.telegram_service import preview_job_candidates

        source = Chat(100, "Kaynak", ChatPhotoEmpty(), 3, datetime.now(UTC), 1)
        target = Chat(200, "Hedef", ChatPhotoEmpty(), 0, datetime.now(UTC), 1, creator=True)
        admin = User(1, first_name="Yönetici")
        bot = User(4, first_name="Bot", bot=True)
        regular = User(5, first_name="Normal", last_name="Üye")

        class Message:
            def __init__(self, sender):
                self.sender_id = sender.id
                self.sender = sender
                self.id = 500 + sender.id

            async def get_sender(self):
                return self.sender

        class FakeClient:
            async def iter_participants(self, entity, limit=None, filter=None):
                if entity.id == source.id:
                    yield admin
                    yield bot

            async def iter_messages(self, entity, limit=None):
                for sender in (admin, bot, regular):
                    yield Message(sender)

            async def __call__(self, request):
                return SimpleNamespace(
                    full_chat=SimpleNamespace(
                        participants=SimpleNamespace(
                            participants=[ChatParticipantAdmin(1, 99, datetime.now(UTC))]
                        )
                    )
                )

            async def disconnect(self):
                return None

        with get_connection() as connection:
            job = connection.execute(
                "SELECT * FROM transfer_jobs WHERE id=?", (self.job_id,)
            ).fetchone()

        with patch(
            "app.telegram_service._client_for",
            new=AsyncMock(return_value=FakeClient()),
        ), patch(
            "app.telegram_service._resolve_entity",
            new=AsyncMock(side_effect=[source, target]),
        ):
            summary = asyncio.run(preview_job_candidates(job))

        self.assertEqual(summary["admin"], 1)
        self.assertEqual(summary["bot"], 1)
        self.assertEqual(summary["eligible"], 1)
        with get_connection() as connection:
            regular_row = connection.execute(
                "SELECT * FROM job_candidates WHERE job_id=? AND telegram_user_id=5",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(regular_row["status"], "eligible")

    def test_completed_activity_results_feed_the_transfer_preview(self):
        from app.database import get_connection, utc_now
        from app.telegram_service import preview_job_candidates

        source = Chat(100, "Kaynak", ChatPhotoEmpty(), 2, datetime.now(UTC), 1)
        target = Chat(200, "Hedef", ChatPhotoEmpty(), 0, datetime.now(UTC), 1, creator=True)
        admin = User(1, first_name="Yönetici")
        now = utc_now()
        with get_connection() as connection:
            scan_id = connection.execute(
                """
                INSERT INTO activity_scans(
                    name, session_id, group_ref, group_id, group_title, window_hours,
                    status, last_run_at, created_at, updated_at
                ) VALUES ('Tarama', ?, '@source', 100, 'Kaynak', 24,
                          'completed', ?, ?, ?)
                """,
                (self.session_id, now, now, now),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO activity_results(
                    scan_id, telegram_user_id, display_name, username, access_hash,
                    source_message_id, message_count, last_message_at, created_at
                ) VALUES (?, 6, 'Aktif Üye', NULL, 987654321, 606, 4, ?, ?)
                """,
                (scan_id, now, now),
            )
            job = connection.execute(
                "SELECT * FROM transfer_jobs WHERE id=?", (self.job_id,)
            ).fetchone()

        class FakeClient:
            async def iter_participants(self, entity, limit=None, filter=None):
                if entity.id == source.id:
                    yield admin

            async def iter_messages(self, entity, limit=None):
                if False:
                    yield entity

            async def __call__(self, request):
                return SimpleNamespace(
                    full_chat=SimpleNamespace(
                        participants=SimpleNamespace(
                            participants=[ChatParticipantAdmin(1, 99, datetime.now(UTC))]
                        )
                    )
                )

            async def disconnect(self):
                return None

        with patch(
            "app.telegram_service._client_for", new=AsyncMock(return_value=FakeClient())
        ), patch(
            "app.telegram_service._resolve_entity",
            new=AsyncMock(side_effect=[source, target]),
        ):
            summary = asyncio.run(preview_job_candidates(job))

        self.assertEqual(summary["activity_scan_id"], scan_id)
        self.assertEqual(summary["eligible"], 1)
        with get_connection() as connection:
            candidate = connection.execute(
                "SELECT * FROM job_candidates WHERE job_id=?", (self.job_id,)
            ).fetchone()
        self.assertEqual(candidate["telegram_user_id"], 6)
        self.assertEqual(candidate["access_hash"], 987654321)
        self.assertEqual(candidate["source_message_id"], 606)

    def test_legacy_activity_rows_without_context_do_not_block_fresh_message_authors(self):
        from app.database import get_connection, utc_now
        from app.telegram_service import preview_job_candidates

        source = Chat(100, "Kaynak", ChatPhotoEmpty(), 2, datetime.now(UTC), 1)
        target = Chat(200, "Hedef", ChatPhotoEmpty(), 0, datetime.now(UTC), 1, creator=True)
        admin = User(1, first_name="Yönetici", access_hash=101)
        fresh_user = User(7, first_name="Yeni", last_name="Üye", min=True)
        now = utc_now()
        with get_connection() as connection:
            scan_id = connection.execute(
                """
                INSERT INTO activity_scans(
                    name, session_id, group_ref, group_id, group_title, window_hours,
                    status, last_run_at, created_at, updated_at
                ) VALUES ('Eski tarama', ?, '@source', 100, 'Kaynak', 24,
                          'completed', ?, ?, ?)
                """,
                (self.session_id, now, now, now),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO activity_results(
                    scan_id, telegram_user_id, display_name, username, access_hash,
                    message_count, last_message_at, created_at
                ) VALUES (?, 7, 'Eski Üye', NULL, 111, 4, ?, ?)
                """,
                (scan_id, now, now),
            )
            job = connection.execute(
                "SELECT * FROM transfer_jobs WHERE id=?", (self.job_id,)
            ).fetchone()

        class Message:
            sender_id = 7
            id = 707

            async def get_sender(self):
                return fresh_user

        class FakeClient:
            async def iter_participants(self, entity, limit=None, filter=None):
                if entity.id == source.id:
                    yield admin

            async def iter_messages(self, entity, limit=None):
                yield Message()

            async def __call__(self, request):
                return SimpleNamespace(
                    full_chat=SimpleNamespace(
                        participants=SimpleNamespace(
                            participants=[ChatParticipantAdmin(1, 99, datetime.now(UTC))]
                        )
                    )
                )

            async def disconnect(self):
                return None

        with patch(
            "app.telegram_service._client_for", new=AsyncMock(return_value=FakeClient())
        ), patch(
            "app.telegram_service._resolve_entity",
            new=AsyncMock(side_effect=[source, target]),
        ):
            summary = asyncio.run(preview_job_candidates(job))

        self.assertEqual(summary["eligible"], 1)
        with get_connection() as connection:
            candidate = connection.execute(
                "SELECT * FROM job_candidates WHERE job_id=? AND telegram_user_id=7",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(candidate["source_message_id"], 707)


if __name__ == "__main__":
    unittest.main()
