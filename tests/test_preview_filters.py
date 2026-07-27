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

        class FakeClient:
            async def iter_participants(self, entity, limit=None, filter=None):
                if entity.id == source.id:
                    for user in (
                        User(1, first_name="Yönetici"),
                        User(2, first_name="Geçmiş"),
                        User(3, first_name="Uygun"),
                    ):
                        yield user

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
        self.assertEqual(statuses, {1: "admin", 2: "previously_used", 3: "eligible"})


if __name__ == "__main__":
    unittest.main()
