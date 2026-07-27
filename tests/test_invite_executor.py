import asyncio
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telethon.errors import PeerFloodError, UserPrivacyRestrictedError
from telethon.tl.functions.channels import InviteToChannelRequest
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
                    display_name, status, created_at, updated_at
                ) VALUES ('Davet', '+90 ***', 'enc', 'session', 'Davet', 'active', ?, ?)
                """,
                (now, now),
            ).lastrowid
            self.session_id = session_id
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

        self.assertIsInstance(fake_client.last_request, InviteToChannelRequest)
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

    def test_peer_flood_preserves_remaining_candidate_and_pauses_job(self):
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
                    display_name, status, created_at, updated_at
                ) VALUES ('Yedek', '+90 ***', 'enc', 'session', 'Yedek', 'active', ?, ?)
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
        self.assertEqual(job["status"], "flood_wait")
        self.assertEqual(job["session_id"], self.session_id)
        self.assertEqual(candidate["status"], "eligible")
        self.assertEqual(session["status"], "flood_wait")
        flood_until = datetime.fromisoformat(session["flood_wait_until"])
        self.assertGreater(flood_until, before + timedelta(hours=23, minutes=59))
        self.assertLess(flood_until, before + timedelta(hours=24, minutes=1))
        self.assertIsNone(invite_usage)
        with get_connection() as connection:
            replacement = connection.execute(
                "SELECT status FROM telegram_sessions WHERE id=?", (replacement_id,)
            ).fetchone()
        self.assertEqual(replacement["status"], "active")

    def test_three_successful_adds_pause_remaining_candidates_for_thirty_minutes(self):
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
                if isinstance(request, InviteToChannelRequest):
                    return None
                raise AssertionError(type(request).__name__)

            async def disconnect(self):
                return None

        before = datetime.now(UTC)
        with patch(
            "app.telegram_service._client_for", new=AsyncMock(return_value=BatchClient())
        ), patch(
            "app.telegram_service._resolve_entity", new=AsyncMock(return_value=target)
        ):
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
        self.assertEqual(job["status"], "paused_batch")
        self.assertEqual(job["succeeded"], 3)
        self.assertEqual([row["status"] for row in candidates], ["invited", "invited", "invited", "eligible"])
        self.assertEqual(session["status"], "batch_wait")
        self.assertEqual(session["batch_success_count"], 0)
        cooldown_until = datetime.fromisoformat(session["batch_cooldown_until"])
        self.assertGreater(cooldown_until, before + timedelta(minutes=29))
        self.assertLess(cooldown_until, before + timedelta(minutes=31))

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

        class FailedProxy:
            async def connect(self, **kwargs):
                raise OSError("wrong protocol")

        class Socket:
            def close(self):
                return None

        class WorkingProxy:
            async def connect(self, **kwargs):
                return Socket()

        with patch(
            "app.telegram_service.Proxy.create",
            side_effect=[FailedProxy(), WorkingProxy()],
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

        with patch("app.telegram_service._credentials", return_value=(12345, "hash")), patch(
            "app.telegram_service.TelegramClient"
        ) as telegram_client:
            with self.assertRaises(ProxyUnavailableError) as raised:
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

        invited_user = client.last_request.users[0]
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
                if isinstance(request, InviteToChannelRequest):
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
        invited_user = client.invite_request.users[0]
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
