import asyncio
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telethon.errors import FloodWaitError, PeerFloodError, UserPrivacyRestrictedError
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.messages import AddChatUserRequest
from telethon.tl.functions.users import GetUsersRequest
from telethon.tl.types import Chat, ChatPhotoEmpty, InputPeerChannel, InputUser, User

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
                    display_name, status, proxy_enabled, proxy_last_status,
                    created_at, updated_at
                ) VALUES (
                    'Davet', '+90 ***', 'enc', 'session', 'Davet',
                    'active', 1, 'success', ?, ?
                )
                """,
                (now, now),
            ).lastrowid
            self.session_id = session_id
            self.job_id = connection.execute(
                """
                INSERT INTO transfer_jobs(
                    name, session_id, source_ref, source_id, target_ref, target_id,
                    status, daily_limit, min_delay_seconds, max_delay_seconds,
                    working_start, working_end, created_at, updated_at
                ) VALUES ('Davet işi', ?, '@source', 100, '@target', 200,
                          'approved', 30, 10, 10, '00:00', '00:00', ?, ?)
                """,
                (session_id, now, now),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO job_candidates(
                    job_id, telegram_user_id, display_name, username, access_hash,
                    status, reason, selected, created_at
                ) VALUES (?, 77, 'Rızalı Üye', 'rizali_uye', 777001, 'eligible', 'Onaylı', 1, ?)
                """,
                (self.job_id, now),
            )

    def tearDown(self):
        get_settings.cache_clear()
        self.temp_dir.cleanup()

    def test_selected_candidate_is_invited_and_recorded(self):
        from app.database import get_connection
        from app.telegram_service import execute_invite_job

        today = datetime.now(UTC).date().isoformat()
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO session_usage_daily(session_id, usage_date, operation_count, last_used_at)
                VALUES (?, ?, 99, ?)
                """,
                (self.session_id, today, datetime.now(UTC).isoformat()),
            )

        target = Chat(200, "Hedef", ChatPhotoEmpty(), 0, datetime.now(UTC), 1, creator=True)
        user = User(77, access_hash=777001, first_name="Rızalı", last_name="Üye", username="rizali_uye")

        class Message:
            sender_id = user.id

            async def get_sender(self):
                return user

        class FakeClient:
            async def iter_participants(self, entity, limit=None, filter=None):
                raise AssertionError("Davet yürütücüsü kaynak üyeleri yeniden taramamalı")
                yield entity

            async def iter_messages(self, entity, limit=None):
                raise AssertionError("Davet yürütücüsü kaynak mesajları yeniden taramamalı")
                yield entity

            async def __call__(self, request):
                if isinstance(request, GetUsersRequest):
                    return [user]
                self.last_request = request
                return None

            async def disconnect(self):
                return None

        fake_client = FakeClient()
        with patch("app.telegram_service._client_for", new=AsyncMock(return_value=fake_client)), patch(
            "app.telegram_service._resolve_entity",
            new=AsyncMock(return_value=target),
        ), patch("app.telegram_service.asyncio.sleep", new=AsyncMock()):
            asyncio.run(execute_invite_job(self.job_id))

        self.assertIsInstance(fake_client.last_request, AddChatUserRequest)
        with get_connection() as connection:
            job = connection.execute("SELECT * FROM transfer_jobs WHERE id=?", (self.job_id,)).fetchone()
            candidate = connection.execute(
                "SELECT * FROM job_candidates WHERE job_id=?", (self.job_id,)
            ).fetchone()
            history = connection.execute(
                "SELECT * FROM member_history WHERE telegram_user_id=77"
            ).fetchone()
            invite_usage = connection.execute(
                "SELECT invite_count FROM session_invite_usage_daily WHERE session_id=? AND usage_date=?",
                (self.session_id, today),
            ).fetchone()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["succeeded"], 1)
        self.assertEqual(candidate["status"], "invited")
        self.assertEqual(history["status"], "invited")
        self.assertEqual(invite_usage["invite_count"], 1)

    def test_privacy_restriction_skips_candidate_and_completes_job(self):
        from app.database import get_connection
        from app.telegram_service import execute_invite_job

        target = Chat(200, "Hedef", ChatPhotoEmpty(), 0, datetime.now(UTC), 1, creator=True)
        user = User(77, access_hash=777001, first_name="Rızalı", username="rizali_uye")

        class RestrictedClient:
            async def iter_participants(self, entity, limit=None, filter=None):
                yield user

            async def iter_messages(self, entity, limit=None):
                if False:
                    yield entity

            async def __call__(self, request):
                if isinstance(request, GetUsersRequest):
                    return [user]
                raise UserPrivacyRestrictedError(request=request)

            async def disconnect(self):
                return None

        with patch(
            "app.telegram_service._client_for",
            new=AsyncMock(return_value=RestrictedClient()),
        ), patch(
            "app.telegram_service._resolve_entity",
            new=AsyncMock(return_value=target),
        ):
            asyncio.run(execute_invite_job(self.job_id))

        with get_connection() as connection:
            job = connection.execute("SELECT * FROM transfer_jobs WHERE id=?", (self.job_id,)).fetchone()
            history = connection.execute(
                "SELECT * FROM member_history WHERE telegram_user_id=77"
            ).fetchone()
            log = connection.execute(
                "SELECT * FROM system_logs WHERE job_id=? AND category='invite_candidate' ORDER BY id DESC",
                (self.job_id,),
            ).fetchone()
            candidate = connection.execute(
                "SELECT * FROM job_candidates WHERE job_id=?", (self.job_id,)
            ).fetchone()
            invite_usage = connection.execute(
                "SELECT invite_count FROM session_invite_usage_daily WHERE session_id=?",
                (self.session_id,),
            ).fetchone()
        self.assertEqual(job["status"], "completed")
        self.assertIsNone(history)
        self.assertEqual(candidate["status"], "skipped")
        self.assertIn("gizlilik", log["message"])
        self.assertIsNone(invite_usage)

    def test_peer_flood_switches_to_next_session_and_completes_without_rescan(self):
        from app.database import get_connection
        from app.telegram_service import execute_invite_job

        before = datetime.now(UTC)
        target = Chat(200, "Hedef", ChatPhotoEmpty(), 0, before, 1, creator=True)
        user = User(77, access_hash=777001, first_name="Rızalı", username="rizali_uye")

        with get_connection() as connection:
            replacement_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, proxy_enabled, proxy_last_status,
                    created_at, updated_at
                ) VALUES (
                    'Yedek', '+90 ***', 'enc', 'session', 'Yedek',
                    'active', 1, 'success', ?, ?
                )
                """,
                (before.isoformat(), before.isoformat()),
            ).lastrowid

        class FloodedClient:
            async def __call__(self, request):
                if isinstance(request, GetUsersRequest):
                    return [user]
                raise PeerFloodError(request=request)

            async def disconnect(self):
                return None

        class ReplacementClient:
            async def __call__(self, request):
                if isinstance(request, GetUsersRequest):
                    return [user]
                if isinstance(request, (InviteToChannelRequest, AddChatUserRequest)):
                    return None
                raise AssertionError(type(request).__name__)

            async def iter_participants(self, entity, limit=None, filter=None):
                raise AssertionError("Session değişiminde kaynak grup yeniden taranmamalı")
                yield entity

            async def iter_messages(self, entity, limit=None):
                raise AssertionError("Session değişiminde kaynak mesajlar yeniden taranmamalı")
                yield entity

            async def disconnect(self):
                return None

        clients = {
            self.session_id: FloodedClient(),
            replacement_id: ReplacementClient(),
        }

        with patch(
            "app.telegram_service._client_for",
            new=AsyncMock(side_effect=lambda session_id: clients[session_id]),
        ), patch(
            "app.telegram_service._resolve_entity",
            new=AsyncMock(return_value=target),
        ):
            asyncio.run(execute_invite_job(self.job_id))

        with get_connection() as connection:
            job = connection.execute(
                "SELECT * FROM transfer_jobs WHERE id=?", (self.job_id,)
            ).fetchone()
            candidate = connection.execute(
                "SELECT * FROM job_candidates WHERE job_id=?", (self.job_id,)
            ).fetchone()
            session = connection.execute(
                "SELECT * FROM telegram_sessions WHERE id=?", (self.session_id,)
            ).fetchone()
            invite_usage = connection.execute(
                "SELECT invite_count FROM session_invite_usage_daily WHERE session_id=?",
                (self.session_id,),
            ).fetchone()
            replacement_usage = connection.execute(
                "SELECT invite_count FROM session_invite_usage_daily WHERE session_id=?",
                (replacement_id,),
            ).fetchone()
            notifications = connection.execute(
                "SELECT title, message FROM notifications ORDER BY id"
            ).fetchall()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["session_id"], replacement_id)
        self.assertEqual(candidate["status"], "invited")
        self.assertEqual(session["status"], "flood_wait")
        flood_until = datetime.fromisoformat(session["flood_wait_until"])
        self.assertGreater(flood_until, before + timedelta(hours=23, minutes=59))
        self.assertLess(flood_until, before + timedelta(hours=24, minutes=1))
        self.assertIsNone(invite_usage)
        self.assertEqual(replacement_usage["invite_count"], 1)
        self.assertTrue(
            any(
                row["title"] == "Davet session'ı değiştirildi"
                and f"Session #{replacement_id} ile hemen devam ediyor" in row["message"]
                for row in notifications
            )
        )
        self.assertFalse(
            any(
                row["title"] == "Telegram ekleme kısıtlaması"
                and "hemen devam ediyor" in row["message"]
                for row in notifications
            )
        )
        with get_connection() as connection:
            replacement = connection.execute(
                "SELECT status FROM telegram_sessions WHERE id=?", (replacement_id,)
            ).fetchone()
        self.assertEqual(replacement["status"], "active")

    def test_peer_flood_without_replacement_reports_search_then_scheduled_wait(self):
        from app.database import get_connection
        from app.telegram_service import execute_invite_job

        target = Chat(200, "Hedef", ChatPhotoEmpty(), 0, datetime.now(UTC), 1, creator=True)
        user = User(77, access_hash=777001, first_name="Rızalı", username="rizali_uye")

        class FloodedClient:
            async def __call__(self, request):
                if isinstance(request, GetUsersRequest):
                    return [user]
                raise PeerFloodError(request=request)

            async def disconnect(self):
                return None

        with patch(
            "app.telegram_service._client_for",
            new=AsyncMock(return_value=FloodedClient()),
        ), patch(
            "app.telegram_service._resolve_entity",
            new=AsyncMock(return_value=target),
        ):
            asyncio.run(execute_invite_job(self.job_id))

        with get_connection() as connection:
            job = connection.execute(
                "SELECT status, resume_at, last_error FROM transfer_jobs WHERE id=?",
                (self.job_id,),
            ).fetchone()
            notifications = connection.execute(
                "SELECT title, message FROM notifications ORDER BY id"
            ).fetchall()

        self.assertEqual(job["status"], "scheduled")
        self.assertIsNotNone(job["resume_at"])
        self.assertIn("Tüm Telegram session'ları", job["last_error"])
        self.assertTrue(
            any("sıradaki uygun session aranıyor" in row["message"] for row in notifications)
        )
        self.assertTrue(
            any(row["title"] == "Tüm session'lar beklemede" for row in notifications)
        )
        self.assertFalse(
            any("hemen devam ediyor" in row["message"] for row in notifications)
        )

    def test_flood_wait_switches_to_next_session_and_retries_same_candidate(self):
        from app.database import get_connection, utc_now
        from app.telegram_service import execute_invite_job

        now = utc_now()
        with get_connection() as connection:
            replacement_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, proxy_enabled, proxy_last_status,
                    created_at, updated_at
                ) VALUES (
                    'Flood Yedek', '+90 ***', 'enc2', 'session2', 'Flood Yedek',
                    'active', 1, 'success', ?, ?
                )
                """,
                (now, now),
            ).lastrowid

        target = Chat(200, "Hedef", ChatPhotoEmpty(), 0, datetime.now(UTC), 1, creator=True)
        user = User(77, access_hash=777001, first_name="Rızalı", username="rizali_uye")

        class FloodClient:
            async def __call__(self, request):
                if isinstance(request, GetUsersRequest):
                    return [user]
                raise FloodWaitError(request=request, capture=60)

            async def disconnect(self):
                return None

        class ReadyClient:
            async def __call__(self, request):
                if isinstance(request, GetUsersRequest):
                    return [user]
                if isinstance(request, (InviteToChannelRequest, AddChatUserRequest)):
                    return None
                raise AssertionError(type(request).__name__)

            async def disconnect(self):
                return None

        clients = {
            self.session_id: FloodClient(),
            replacement_id: ReadyClient(),
        }
        with patch(
            "app.telegram_service._client_for",
            new=AsyncMock(side_effect=lambda session_id: clients[session_id]),
        ), patch(
            "app.telegram_service._resolve_entity",
            new=AsyncMock(return_value=target),
        ):
            asyncio.run(execute_invite_job(self.job_id))

        with get_connection() as connection:
            job = connection.execute(
                "SELECT status, session_id, succeeded FROM transfer_jobs WHERE id=?",
                (self.job_id,),
            ).fetchone()
            candidate = connection.execute(
                "SELECT status FROM job_candidates WHERE job_id=?",
                (self.job_id,),
            ).fetchone()
            flooded = connection.execute(
                "SELECT status, flood_wait_until FROM telegram_sessions WHERE id=?",
                (self.session_id,),
            ).fetchone()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["session_id"], replacement_id)
        self.assertEqual(job["succeeded"], 1)
        self.assertEqual(candidate["status"], "invited")
        self.assertEqual(flooded["status"], "flood_wait")
        self.assertGreater(datetime.fromisoformat(flooded["flood_wait_until"]), datetime.now(UTC))

    def test_daily_limit_switches_to_next_session_without_pausing_job(self):
        from app.database import get_connection, utc_now
        from app.telegram_service import execute_invite_job

        now = utc_now()
        today = datetime.now(UTC).date().isoformat()
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO session_invite_usage_daily(
                    session_id, usage_date, invite_count, last_used_at
                ) VALUES (?, ?, 30, ?)
                """,
                (self.session_id, today, now),
            )
            replacement_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, proxy_enabled, proxy_last_status,
                    created_at, updated_at
                ) VALUES (
                    'Kota Yedek', '+90 ***', 'enc2', 'session2', 'Kota Yedek',
                    'active', 1, 'success', ?, ?
                )
                """,
                (now, now),
            ).lastrowid

        target = Chat(200, "Hedef", ChatPhotoEmpty(), 0, datetime.now(UTC), 1, creator=True)
        user = User(77, access_hash=777001, first_name="Rızalı", username="rizali_uye")

        class ReadyClient:
            async def __call__(self, request):
                if isinstance(request, GetUsersRequest):
                    return [user]
                if isinstance(request, (InviteToChannelRequest, AddChatUserRequest)):
                    return None
                raise AssertionError(type(request).__name__)

            async def disconnect(self):
                return None

        client_factory = AsyncMock(return_value=ReadyClient())
        with patch("app.telegram_service._client_for", new=client_factory), patch(
            "app.telegram_service._resolve_entity",
            new=AsyncMock(return_value=target),
        ):
            asyncio.run(execute_invite_job(self.job_id))

        self.assertEqual(client_factory.await_count, 1)
        self.assertEqual(client_factory.await_args.args[0], replacement_id)
        with get_connection() as connection:
            job = connection.execute(
                "SELECT status, session_id, succeeded FROM transfer_jobs WHERE id=?",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["session_id"], replacement_id)
        self.assertEqual(job["succeeded"], 1)

    def test_selector_round_robins_and_ignores_operation_locks(self):
        from app.database import get_connection, utc_now
        from app.telegram_service import select_next_available_session

        now = utc_now()
        with get_connection() as connection:
            session_ids = [self.session_id]
            for label in ("İkinci", "Üçüncü"):
                session_ids.append(
                    connection.execute(
                        """
                        INSERT INTO telegram_sessions(
                            label, phone_masked, phone_encrypted, session_encrypted,
                            display_name, status, proxy_enabled, proxy_last_status,
                            created_at, updated_at
                        ) VALUES (?, '+90 ***', 'enc', 'session', ?, 'active', 1, 'success', ?, ?)
                        """,
                        (label, label, now, now),
                    ).lastrowid
                )
            today = datetime.now(UTC).date().isoformat()
            self.assertEqual(
                select_next_available_session(
                    connection, session_ids[0], today, 30
                ).session_id,
                session_ids[1],
            )
            self.assertEqual(
                select_next_available_session(
                    connection, session_ids[1], today, 30
                ).session_id,
                session_ids[2],
            )
            self.assertEqual(
                select_next_available_session(
                    connection, session_ids[2], today, 30
                ).session_id,
                session_ids[0],
            )
            connection.execute(
                """
                INSERT INTO session_operation_locks(
                    session_id, operation_type, operation_key,
                    operation_label, owner_token, acquired_at
                ) VALUES (?, 'test', 'test:lock', 'Test lock', 'owner', ?)
                """,
                (session_ids[1], now),
            )
            self.assertEqual(
                select_next_available_session(
                    connection,
                    session_ids[0],
                    today,
                    30,
                    preferred_session_id=session_ids[1],
                ).session_id,
                session_ids[2],
            )

    def test_selector_logs_every_candidate_and_exact_rejection_reason(self):
        from app.database import get_connection, utc_now
        from app.telegram_service import select_next_available_session

        now = datetime.now(UTC)
        today = now.date().isoformat()
        with get_connection() as connection:
            flood_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, flood_wait_until,
                    proxy_enabled, proxy_last_status, created_at, updated_at
                ) VALUES (
                    'Flood', '+90 ***', 'enc2', 'session2', 'Flood',
                    'flood_wait', ?, 1, 'success', ?, ?
                )
                """,
                ((now + timedelta(hours=1)).isoformat(), utc_now(), utc_now()),
            ).lastrowid
            proxy_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, proxy_enabled, proxy_last_status,
                    proxy_last_error, created_at, updated_at
                ) VALUES (
                    'Proxy', '+90 ***', 'enc3', 'session3', 'Proxy',
                    'active', 1, 'failed', 'Proxy bağlantısı reddedildi', ?, ?
                )
                """,
                (utc_now(), utc_now()),
            ).lastrowid

            selection = select_next_available_session(
                connection,
                None,
                today,
                30,
                now=now,
                job_id=self.job_id,
            )
            logs = connection.execute(
                """
                SELECT session_id, level, message
                FROM system_logs
                WHERE category='invite_selector' AND job_id=?
                ORDER BY id
                """,
                (self.job_id,),
            ).fetchall()

        self.assertEqual(selection.session_id, self.session_id)
        self.assertTrue(
            any(
                row["session_id"] == self.session_id
                and "KABUL" in row["message"]
                and "status=active" in row["message"]
                for row in logs
            )
        )
        self.assertTrue(
            any(
                row["session_id"] == flood_id
                and "RED" in row["message"]
                and "FloodWait devam ediyor" in row["message"]
                for row in logs
            )
        )
        self.assertTrue(
            any(
                row["session_id"] == proxy_id
                and "RED" in row["message"]
                and "Proxy bağlantısı reddedildi" in row["message"]
                for row in logs
            )
        )

    def test_selector_uses_earliest_real_resume_and_recovers_expired_batch_wait(self):
        from app.database import get_connection, utc_now
        from app.telegram_service import select_next_available_session

        now = datetime.now(UTC)
        today = now.date().isoformat()
        batch_until = now + timedelta(minutes=20)
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE telegram_sessions
                SET status='flood_wait', flood_wait_until=?
                WHERE id=?
                """,
                ((now + timedelta(minutes=10)).isoformat(), self.session_id),
            )
            connection.execute(
                """
                INSERT INTO session_invite_usage_daily(
                    session_id, usage_date, invite_count, last_used_at
                ) VALUES (?, ?, 30, ?)
                """,
                (self.session_id, today, utc_now()),
            )
            batch_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, proxy_enabled, proxy_last_status,
                    batch_cooldown_until, created_at, updated_at
                ) VALUES (
                    'Parti', '+90 ***', 'enc2', 'session2', 'Parti',
                    'batch_wait', 1, 'success', ?, ?, ?
                )
                """,
                (batch_until.isoformat(), utc_now(), utc_now()),
            ).lastrowid

            selection = select_next_available_session(
                connection,
                self.session_id,
                today,
                30,
                working_start="00:00",
                working_end="00:00",
                now=now,
            )
            self.assertIsNone(selection.session_id)
            self.assertEqual(selection.resume_at, batch_until)

            connection.execute(
                "UPDATE telegram_sessions SET batch_cooldown_until=? WHERE id=?",
                ((now - timedelta(seconds=1)).isoformat(), batch_id),
            )
            recovered = select_next_available_session(
                connection,
                self.session_id,
                today,
                30,
                now=now,
            )
            self.assertEqual(recovered.session_id, batch_id)
            session = connection.execute(
                "SELECT status, batch_cooldown_until FROM telegram_sessions WHERE id=?",
                (batch_id,),
            ).fetchone()
            self.assertEqual(session["status"], "active")
            self.assertIsNone(session["batch_cooldown_until"])

    def test_three_successful_adds_schedule_remaining_candidates_when_all_sessions_wait(self):
        from app.database import get_connection, utc_now
        from app.telegram_service import execute_invite_job

        now = utc_now()
        with get_connection() as connection:
            connection.executemany(
                """
                INSERT INTO job_candidates(
                    job_id, telegram_user_id, display_name, username, access_hash,
                    status, reason, selected, created_at
                ) VALUES (?, ?, ?, ?, ?, 'eligible', 'Onaylı', 1, ?)
                """,
                [
                    (self.job_id, user_id, f"Üye {user_id}", f"uye_{user_id}", 700000 + user_id, now)
                    for user_id in (78, 79, 80)
                ],
            )

        target = Chat(200, "Hedef", ChatPhotoEmpty(), 0, datetime.now(UTC), 1, creator=True)

        class BatchClient:
            async def __call__(self, request):
                if isinstance(request, GetUsersRequest):
                    input_user = request.id[0]
                    return [
                        User(
                            input_user.user_id,
                            access_hash=input_user.access_hash,
                            first_name=f"Üye {input_user.user_id}",
                        )
                    ]
                if isinstance(request, (InviteToChannelRequest, AddChatUserRequest)):
                    return None
                raise AssertionError(type(request).__name__)

            async def disconnect(self):
                return None

        before = datetime.now(UTC)
        with patch(
            "app.telegram_service._client_for", new=AsyncMock(return_value=BatchClient())
        ), patch(
            "app.telegram_service._resolve_entity", new=AsyncMock(return_value=target)
        ), patch("app.telegram_service.asyncio.sleep", new=AsyncMock()):
            asyncio.run(execute_invite_job(self.job_id))

        with get_connection() as connection:
            job = connection.execute(
                "SELECT * FROM transfer_jobs WHERE id=?", (self.job_id,)
            ).fetchone()
            session = connection.execute(
                "SELECT * FROM telegram_sessions WHERE id=?", (self.session_id,)
            ).fetchone()
            candidates = connection.execute(
                "SELECT status FROM job_candidates WHERE job_id=? ORDER BY id",
                (self.job_id,),
            ).fetchall()
        self.assertEqual(job["status"], "scheduled")
        self.assertEqual(job["succeeded"], 3)
        self.assertEqual([row["status"] for row in candidates], ["invited", "invited", "invited", "eligible"])
        self.assertEqual(session["status"], "batch_wait")
        self.assertEqual(session["batch_success_count"], 0)
        cooldown_until = datetime.fromisoformat(session["batch_cooldown_until"])
        self.assertGreater(cooldown_until, before + timedelta(minutes=19))
        self.assertLess(cooldown_until, before + timedelta(minutes=21))

    def test_batch_limit_hands_remaining_candidates_to_next_session_without_rescan(self):
        from app.database import get_connection, utc_now
        from app.telegram_service import execute_invite_job

        now = utc_now()
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE telegram_sessions
                SET proxy_enabled=1, proxy_last_status='success',
                    invite_batch_limit=1, invite_cooldown_minutes=20
                WHERE id=?
                """,
                (self.session_id,),
            )
            second_session_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, proxy_enabled, proxy_last_status,
                    invite_batch_limit, invite_cooldown_minutes,
                    created_at, updated_at
                ) VALUES ('Davet 2', '+90 ***', 'enc2', 'session2', 'Davet 2',
                          'active', 1, 'success', 3, 20, ?, ?)
                """,
                (now, now),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO job_candidates(
                    job_id, telegram_user_id, display_name, username, access_hash,
                    status, reason, selected, created_at
                ) VALUES (?, 78, 'Üye 78', 'uye_78', 777002,
                          'eligible', 'Onaylı', 1, ?)
                """,
                (self.job_id, now),
            )

        target = Chat(200, "Hedef", ChatPhotoEmpty(), 0, datetime.now(UTC), 1, creator=True)

        class HandoffClient:
            def __init__(self):
                self.added = []

            async def __call__(self, request):
                if isinstance(request, GetUsersRequest):
                    input_user = request.id[0]
                    return [
                        User(
                            input_user.user_id,
                            access_hash=input_user.access_hash,
                            first_name=f"Üye {input_user.user_id}",
                        )
                    ]
                if isinstance(request, (InviteToChannelRequest, AddChatUserRequest)):
                    self.added.append(request)
                    return None
                raise AssertionError(type(request).__name__)

            async def iter_participants(self, entity, limit=None, filter=None):
                raise AssertionError("Session devrinde kaynak grup yeniden taranmamalı")
                yield entity

            async def iter_messages(self, entity, limit=None):
                raise AssertionError("Session devrinde kaynak mesajlar yeniden taranmamalı")
                yield entity

            async def disconnect(self):
                return None

        first_client = HandoffClient()
        second_client = HandoffClient()
        client_factory = AsyncMock(side_effect=[first_client, second_client])
        with patch("app.telegram_service._client_for", new=client_factory), patch(
            "app.telegram_service._resolve_entity",
            new=AsyncMock(return_value=target),
        ), patch("app.telegram_service.asyncio.sleep", new=AsyncMock()):
            asyncio.run(execute_invite_job(self.job_id))

        with get_connection() as connection:
            completed = connection.execute(
                "SELECT status, session_id, succeeded FROM transfer_jobs WHERE id=?",
                (self.job_id,),
            ).fetchone()
            candidate_states = connection.execute(
                "SELECT status FROM job_candidates WHERE job_id=? ORDER BY id",
                (self.job_id,),
            ).fetchall()
            first_session = connection.execute(
                "SELECT status FROM telegram_sessions WHERE id=?",
                (self.session_id,),
            ).fetchone()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["session_id"], second_session_id)
        self.assertEqual(completed["succeeded"], 2)
        self.assertEqual([row["status"] for row in candidate_states], ["invited", "invited"])
        self.assertEqual(first_session["status"], "batch_wait")
        self.assertEqual(client_factory.await_count, 2)

    def test_proxy_failure_pauses_job_without_consuming_candidate(self):
        from app.database import get_connection
        from app.telegram_service import ProxyUnavailableError, execute_invite_job

        with patch(
            "app.telegram_service._client_for",
            new=AsyncMock(side_effect=ProxyUnavailableError("Proxy yanıt vermiyor; ana IP kullanılmadı.")),
        ):
            asyncio.run(execute_invite_job(self.job_id))

        with get_connection() as connection:
            job = connection.execute(
                "SELECT status, last_error, processed FROM transfer_jobs WHERE id=?",
                (self.job_id,),
            ).fetchone()
            candidate = connection.execute(
                "SELECT status, processed_at FROM job_candidates WHERE job_id=?",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(job["status"], "proxy_error")
        self.assertIn("ana IP kullanılmadı", job["last_error"])
        self.assertEqual(job["processed"], 0)
        self.assertEqual(candidate["status"], "eligible")
        self.assertIsNone(candidate["processed_at"])

    def test_proxy_test_auto_detects_http_when_socks5_is_wrong(self):
        from app.database import get_connection
        from app.telegram_service import test_session_proxy

        with get_connection() as connection:
            connection.execute(
                """
                UPDATE telegram_sessions
                SET proxy_enabled=1, proxy_type='socks5', proxy_host='proxy.local',
                    proxy_port=3129, status='proxy_pending'
                WHERE id=?
                """,
                (self.session_id,),
            )

        with patch("app.telegram_service.decrypt", return_value="session"), patch(
            "app.telegram_service._test_proxy_connection",
            new=AsyncMock(return_value=("http", 42)),
        ):
            result = asyncio.run(test_session_proxy(self.session_id))

        self.assertTrue(result["ok"])
        self.assertTrue(result["auto_detected"])
        self.assertEqual(result["proxy_type"], "http")
        with get_connection() as connection:
            session = connection.execute(
                "SELECT proxy_type, proxy_last_status, status FROM telegram_sessions WHERE id=?",
                (self.session_id,),
            ).fetchone()
        self.assertEqual(session["proxy_type"], "http")
        self.assertEqual(session["proxy_last_status"], "success")
        self.assertEqual(session["status"], "active")

    def test_session_without_proxy_never_constructs_a_telegram_client(self):
        from app.database import get_connection
        from app.telegram_service import ProxyUnavailableError, _client_for

        with get_connection() as connection:
            connection.execute(
                """
                UPDATE telegram_sessions
                SET proxy_enabled=0, proxy_last_status=NULL
                WHERE id=?
                """,
                (self.session_id,),
            )

        with (
            patch("app.telegram_service._credentials", return_value=(12345, "hash")),
            patch("app.telegram_service.TelegramClient") as telegram_client,
            self.assertRaises(ProxyUnavailableError) as raised,
        ):
            asyncio.run(_client_for(self.session_id))

        telegram_client.assert_not_called()
        self.assertIn("proxy atanmamış", str(raised.exception))
        with get_connection() as connection:
            session = connection.execute(
                "SELECT status, last_error FROM telegram_sessions WHERE id=?",
                (self.session_id,),
            ).fetchone()
        self.assertEqual(session["status"], "proxy_error")
        self.assertIn("Toplu Proxy Ekle", session["last_error"])

    def test_stored_access_hash_resolves_hidden_candidate(self):
        from app.database import get_connection
        from app.telegram_service import execute_invite_job

        target = Chat(200, "Hedef", ChatPhotoEmpty(), 0, datetime.now(UTC), 1, creator=True)
        with get_connection() as connection:
            connection.execute(
                "UPDATE job_candidates SET access_hash=123456789 WHERE job_id=?",
                (self.job_id,),
            )

        class HiddenClient:
            async def iter_participants(self, entity, limit=None, filter=None):
                if False:
                    yield entity

            async def iter_messages(self, entity, limit=None):
                if False:
                    yield entity

            async def __call__(self, request):
                if isinstance(request, GetUsersRequest):
                    return [User(77, access_hash=123456789, first_name="Rızalı")]
                self.last_request = request
                return None

            async def disconnect(self):
                return None

        client = HiddenClient()
        with patch(
            "app.telegram_service._client_for", new=AsyncMock(return_value=client)
        ), patch(
            "app.telegram_service._resolve_entity",
            new=AsyncMock(return_value=target),
        ), patch("app.telegram_service.asyncio.sleep", new=AsyncMock()):
            asyncio.run(execute_invite_job(self.job_id))

        invited_user = client.last_request.user_id
        self.assertIsInstance(invited_user, InputUser)
        self.assertEqual(invited_user.user_id, 77)
        self.assertEqual(invited_user.access_hash, 123456789)

    def test_message_context_refreshes_min_user_before_direct_add(self):
        from app.database import get_connection
        from app.telegram_service import execute_invite_job

        target = Chat(200, "Hedef", ChatPhotoEmpty(), 0, datetime.now(UTC), 1, creator=True)
        with get_connection() as connection:
            connection.execute(
                "UPDATE job_candidates SET access_hash=111, source_message_id=555 WHERE job_id=?",
                (self.job_id,),
            )

        class ContextClient:
            def __init__(self):
                self.invite_request = None

            async def get_input_entity(self, entity):
                return InputPeerChannel(100, 999)

            async def __call__(self, request):
                if isinstance(request, GetUsersRequest):
                    return [User(77, access_hash=888777, first_name="Rızalı")]
                if isinstance(request, AddChatUserRequest):
                    self.invite_request = request
                    return None
                raise AssertionError(type(request).__name__)

            async def disconnect(self):
                return None

        client = ContextClient()
        with patch(
            "app.telegram_service._client_for", new=AsyncMock(return_value=client)
        ), patch(
            "app.telegram_service._resolve_entity",
            new=AsyncMock(side_effect=[target, object()]),
        ), patch("app.telegram_service.asyncio.sleep", new=AsyncMock()):
            asyncio.run(execute_invite_job(self.job_id))

        self.assertIsNotNone(client.invite_request)
        invited_user = client.invite_request.user_id
        self.assertIsInstance(invited_user, InputUser)
        self.assertEqual(invited_user.user_id, 77)
        self.assertEqual(invited_user.access_hash, 888777)

    def test_session_connection_error_marks_job_failed(self):
        from app.database import get_connection
        from app.telegram_service import execute_invite_job

        with patch(
            "app.telegram_service._client_for",
            new=AsyncMock(side_effect=RuntimeError("Bağlantı kurulamadı")),
        ):
            asyncio.run(execute_invite_job(self.job_id))

        with get_connection() as connection:
            job = connection.execute(
                "SELECT status, last_error FROM transfer_jobs WHERE id=?",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(job["status"], "failed")
        self.assertIn("Bağlantı kurulamadı", job["last_error"])


if __name__ == "__main__":
    unittest.main()
