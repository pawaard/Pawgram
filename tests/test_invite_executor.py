import asyncio
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telethon.errors import UserPrivacyRestrictedError
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.types import Chat, ChatPhotoEmpty, User

from app.config import get_settings


class InviteExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["DATABASE_PATH"] = str(Path(self.temp_dir.name) / "invite.db")
        get_settings.cache_clear()
        from app.database import get_connection, initialize_database, utc_now

        initialize_database()
        now = utc_now()
        with get_connection() as connection:
            session_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, created_at, updated_at
                ) VALUES ('Davet', '+90 ***', 'enc', 'session', 'Davet', 'active', ?, ?)
                """,
                (now, now),
            ).lastrowid
            self.job_id = connection.execute(
                """
                INSERT INTO transfer_jobs(
                    name, session_id, source_ref, source_id, target_ref, target_id,
                    status, daily_limit, min_delay_seconds, max_delay_seconds,
                    created_at, updated_at
                ) VALUES ('Davet işi', ?, '@source', 100, '@target', 200,
                          'approved', 30, 10, 10, ?, ?)
                """,
                (session_id, now, now),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO job_candidates(
                    job_id, telegram_user_id, display_name, username,
                    status, reason, selected, created_at
                ) VALUES (?, 77, 'Rızalı Üye', 'rizali_uye', 'eligible', 'Onaylı', 1, ?)
                """,
                (self.job_id, now),
            )

    def tearDown(self):
        get_settings.cache_clear()
        self.temp_dir.cleanup()

    def test_selected_candidate_is_invited_and_recorded(self):
        from app.database import get_connection
        from app.telegram_service import execute_invite_job

        source = Chat(100, "Kaynak", ChatPhotoEmpty(), 1, datetime.now(UTC), 1)
        target = Chat(200, "Hedef", ChatPhotoEmpty(), 0, datetime.now(UTC), 1, creator=True)
        user = User(77, first_name="Rızalı", last_name="Üye", username="rizali_uye")

        class FakeClient:
            async def iter_participants(self, entity, limit=None, filter=None):
                yield user

            async def __call__(self, request):
                self.last_request = request
                return None

            async def disconnect(self):
                return None

        fake_client = FakeClient()
        with patch("app.telegram_service._client_for", new=AsyncMock(return_value=fake_client)), patch(
            "app.telegram_service._resolve_entity",
            new=AsyncMock(side_effect=[source, target]),
        ), patch("app.telegram_service.asyncio.sleep", new=AsyncMock()):
            asyncio.run(execute_invite_job(self.job_id))

        self.assertIsInstance(fake_client.last_request, InviteToChannelRequest)
        with get_connection() as connection:
            job = connection.execute("SELECT * FROM transfer_jobs WHERE id=?", (self.job_id,)).fetchone()
            candidate = connection.execute(
                "SELECT * FROM job_candidates WHERE job_id=?", (self.job_id,)
            ).fetchone()
            history = connection.execute(
                "SELECT * FROM member_history WHERE telegram_user_id=77"
            ).fetchone()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["succeeded"], 1)
        self.assertEqual(candidate["status"], "invited")
        self.assertEqual(history["status"], "invited")

    def test_privacy_restriction_stops_job_and_is_logged(self):
        from app.database import get_connection
        from app.telegram_service import execute_invite_job

        source = Chat(100, "Kaynak", ChatPhotoEmpty(), 1, datetime.now(UTC), 1)
        target = Chat(200, "Hedef", ChatPhotoEmpty(), 0, datetime.now(UTC), 1, creator=True)
        user = User(77, first_name="Rızalı", username="rizali_uye")

        class RestrictedClient:
            async def iter_participants(self, entity, limit=None, filter=None):
                yield user

            async def __call__(self, request):
                raise UserPrivacyRestrictedError(request=request)

            async def disconnect(self):
                return None

        with patch(
            "app.telegram_service._client_for",
            new=AsyncMock(return_value=RestrictedClient()),
        ), patch(
            "app.telegram_service._resolve_entity",
            new=AsyncMock(side_effect=[source, target]),
        ):
            asyncio.run(execute_invite_job(self.job_id))

        with get_connection() as connection:
            job = connection.execute("SELECT * FROM transfer_jobs WHERE id=?", (self.job_id,)).fetchone()
            history = connection.execute(
                "SELECT * FROM member_history WHERE telegram_user_id=77"
            ).fetchone()
            log = connection.execute(
                "SELECT * FROM system_logs WHERE job_id=? AND category='invite' ORDER BY id DESC",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(job["status"], "failed")
        self.assertIsNone(history)
        self.assertIn("UserPrivacyRestrictedError", log["message"])


if __name__ == "__main__":
    unittest.main()
