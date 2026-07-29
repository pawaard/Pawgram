import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.sessions import StringSession

from app.config import get_settings


class LoginProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["DATABASE_PATH"] = str(Path(self.temp_dir.name) / "login.db")
        get_settings.cache_clear()
        from app.database import initialize_database

        initialize_database()

    def tearDown(self):
        get_settings.cache_clear()
        self.temp_dir.cleanup()

    def test_code_request_uses_tested_proxy_and_persists_it_for_verification(self):
        from app.database import get_connection
        from app.telegram_service import start_login

        class FakeSession:
            def save(self):
                return "pending-session"

        class FakeClient:
            def __init__(self):
                self.session = FakeSession()

            async def connect(self):
                return None

            async def send_code_request(self, phone):
                return SimpleNamespace(phone_code_hash="phone-code-hash")

            async def disconnect(self):
                return None

        fake_client = FakeClient()
        with patch("app.telegram_service._credentials", return_value=(12345, "hash")), patch(
            "app.telegram_service._connect_telegram_through_proxy",
            new=AsyncMock(return_value=(fake_client, "http", 42)),
        ), patch("app.telegram_service.TelegramClient", return_value=fake_client) as telegram_client:
            result = asyncio.run(
                start_login(
                    "+905551112233",
                    "Proxy hesap",
                    "socks5",
                    "proxy.local",
                    8080,
                    "proxy-user",
                    "proxy-pass",
                )
            )

        self.assertEqual(result["proxy_type"], "http")
        telegram_client.assert_not_called()
        with get_connection() as connection:
            pending = connection.execute("SELECT * FROM pending_auth").fetchone()
        self.assertEqual(pending["proxy_type"], "http")
        self.assertEqual(pending["proxy_host"], "proxy.local")
        self.assertNotEqual(pending["proxy_password_encrypted"], "proxy-pass")

        from app.telegram_service import default_login_proxy_public

        saved = default_login_proxy_public()
        self.assertTrue(saved["configured"])
        self.assertEqual(saved["proxy_type"], "http")
        self.assertEqual(saved["host"], "proxy.local")
        self.assertTrue(saved["password_configured"])

        connector = AsyncMock(return_value=(fake_client, "http", 43))
        with patch("app.telegram_service._credentials", return_value=(12345, "hash")), patch(
            "app.telegram_service._connect_telegram_through_proxy",
            new=connector,
        ):
            asyncio.run(
                start_login(
                    "+905551112244",
                    "İkinci hesap",
                    "http",
                    "proxy.local",
                    8080,
                    "proxy-user",
                    None,
                )
            )
        self.assertEqual(connector.await_args.args[2]["password"], "proxy-pass")

    def test_customer_environment_proxy_bootstraps_encrypted_default(self):
        from app.database import get_app_setting
        from app.telegram_service import default_login_proxy_public

        customer_settings = SimpleNamespace(
            default_proxy_type="socks5",
            default_proxy_host="customer.proxy.local",
            default_proxy_port=10000,
            default_proxy_username="customer-user",
            default_proxy_password="customer-pass",
        )
        with patch("app.telegram_service.get_settings", return_value=customer_settings):
            public = default_login_proxy_public()

        self.assertTrue(public["configured"])
        self.assertEqual(public["host"], "customer.proxy.local")
        self.assertEqual(public["port"], 10000)
        self.assertEqual(public["username"], "customer-user")
        self.assertTrue(public["password_configured"])
        encrypted = get_app_setting("default_login_proxy_encrypted")
        self.assertIsNotNone(encrypted)
        self.assertNotIn("customer-pass", encrypted)

    def test_default_proxy_can_be_saved_and_tested_without_a_session(self):
        from app.telegram_service import (
            save_default_login_proxy,
            test_default_login_proxy,
        )

        saved = save_default_login_proxy(
            "socks5",
            "proxy.local",
            10000,
            "proxy-user",
            "proxy-pass",
        )
        self.assertTrue(saved["configured"])

        fake_client = AsyncMock()
        fake_client.disconnect = AsyncMock()
        connector = AsyncMock(return_value=(fake_client, "socks5", 37))
        with patch("app.telegram_service._credentials", return_value=(12345, "hash")), patch(
            "app.telegram_service._connect_telegram_through_proxy",
            new=connector,
        ):
            result = asyncio.run(test_default_login_proxy())

        self.assertTrue(result["ok"])
        self.assertEqual(result["latency_ms"], 37)
        self.assertTrue(result["fail_closed"])
        connector.assert_awaited_once()
        self.assertEqual(connector.await_args.args[2]["password"], "proxy-pass")
        fake_client.disconnect.assert_awaited_once()

    def test_customer_proxy_revision_updates_only_proxy_fields(self):
        from app.database import get_app_setting, get_connection, utc_now
        from app.security import decrypt
        from app.telegram_service import sync_customer_release_proxy

        now = utc_now()
        flood_until = "2999-01-01T00:00:00+00:00"
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    status, flood_wait_until, proxy_enabled, proxy_type,
                    proxy_host, proxy_port, created_at, updated_at
                ) VALUES
                  ('Waiting', '+90 ***', 'phone-1', 'session-1', 'flood_wait', ?,
                   1, 'http', 'old.proxy', 10000, ?, ?),
                  ('Ready', '+90 ***', 'phone-2', 'session-2', 'active', NULL,
                   1, 'http', 'old.proxy', 10000, ?, ?)
                """,
                (flood_until, now, now, now, now),
            )

        environment = {
            "CUSTOMER_RELEASE": "true",
            "DEFAULT_PROXY_TYPE": "http",
            "DEFAULT_PROXY_HOST": "new.proxy",
            "DEFAULT_PROXY_PORT": "10003",
            "DEFAULT_PROXY_USERNAME": "new-user",
            "DEFAULT_PROXY_PASSWORD": "new-pass",
            "DEFAULT_PROXY_REVISION": "proxy-revision-2",
        }
        with patch.dict(os.environ, environment, clear=False):
            get_settings.cache_clear()
            self.assertEqual(sync_customer_release_proxy(), 2)
            self.assertEqual(sync_customer_release_proxy(), 0)
        get_settings.cache_clear()

        with get_connection() as connection:
            sessions = connection.execute(
                "SELECT * FROM telegram_sessions ORDER BY id"
            ).fetchall()
        self.assertEqual(sessions[0]["status"], "flood_wait")
        self.assertEqual(sessions[0]["flood_wait_until"], flood_until)
        self.assertEqual(sessions[1]["status"], "proxy_pending")
        self.assertTrue(all(row["proxy_host"] == "new.proxy" for row in sessions))
        self.assertTrue(all(row["proxy_port"] == 10003 for row in sessions))
        self.assertTrue(
            all(decrypt(row["proxy_password_encrypted"]) == "new-pass" for row in sessions)
        )
        self.assertEqual(
            get_app_setting("default_login_proxy_revision"),
            "proxy-revision-2",
        )

    def test_real_telegram_probe_falls_back_to_http(self):
        from app.telegram_service import _connect_telegram_through_proxy

        class FailedClient:
            async def connect(self):
                raise asyncio.IncompleteReadError(b"", 8)

            async def disconnect(self):
                return None

        class WorkingClient:
            async def connect(self):
                return None

            async def disconnect(self):
                return None

        working = WorkingClient()
        with patch(
            "app.telegram_service._probe_proxy_socket",
            new=AsyncMock(return_value=3),
        ), patch(
            "app.telegram_service.TelegramClient",
            side_effect=[FailedClient(), working],
        ) as telegram_client:
            client, proxy_type, _ = asyncio.run(
                _connect_telegram_through_proxy(
                    12345,
                    "hash",
                    {
                        "proxy_type": "socks5",
                        "addr": "proxy.local",
                        "port": 8080,
                        "rdns": True,
                        "username": "user",
                        "password": "pass",
                    },
                )
            )

        self.assertIs(client, working)
        self.assertEqual(proxy_type, "http")
        self.assertEqual(telegram_client.call_count, 2)
        self.assertEqual(telegram_client.call_args_list[1].kwargs["proxy"]["proxy_type"], "http")

    def test_account_can_be_added_directly_but_stays_blocked_for_jobs_without_proxy(self):
        from app.database import get_connection
        from app.telegram_service import cancel_pending_login, start_login, verify_login

        class FakeSession:
            def save(self):
                return StringSession().save()

        class DirectClient:
            def __init__(self):
                self.session = FakeSession()

            async def connect(self):
                return None

            async def send_code_request(self, phone):
                return SimpleNamespace(phone_code_hash="direct-code-hash")

            async def sign_in(self, **kwargs):
                return None

            async def get_me(self):
                return SimpleNamespace(
                    id=987654,
                    first_name="Doğrudan",
                    last_name="Hesap",
                    username="direct_account",
                )

            async def disconnect(self):
                return None

        start_client = DirectClient()
        restarted_client = DirectClient()
        verify_client = DirectClient()
        with patch("app.telegram_service._credentials", return_value=(12345, "hash")), patch(
            "app.telegram_service.TelegramClient",
            side_effect=[start_client, restarted_client, verify_client],
        ) as telegram_client:
            result = asyncio.run(
                start_login(
                    "+905551119999",
                    "Doğrudan hesap",
                    "socks5",
                    None,
                    None,
                    None,
                    None,
                    use_proxy=False,
                )
            )
            cancelled = cancel_pending_login("+905551119999")
            self.assertTrue(cancelled["deleted"])
            result = asyncio.run(
                start_login(
                    "+905551119999",
                    "Doğrudan hesap",
                    "socks5",
                    None,
                    None,
                    None,
                    None,
                    use_proxy=False,
                )
            )
            verified = asyncio.run(verify_login("+905551119999", "12345", None))

        self.assertFalse(result["used_proxy"])
        self.assertTrue(verified["ok"])
        self.assertNotIn("proxy", telegram_client.call_args_list[0].kwargs)
        self.assertNotIn("proxy", telegram_client.call_args_list[1].kwargs)
        self.assertIsNone(telegram_client.call_args_list[2].kwargs["proxy"])
        with get_connection() as connection:
            session = connection.execute(
                "SELECT proxy_enabled, status, last_error FROM telegram_sessions WHERE telegram_user_id=?",
                (987654,),
            ).fetchone()
            pending_count = connection.execute("SELECT COUNT(*) AS total FROM pending_auth").fetchone()["total"]
        self.assertEqual(session["proxy_enabled"], 0)
        self.assertEqual(session["status"], "proxy_error")
        self.assertIn("proxy olmadan eklendi", session["last_error"])
        self.assertEqual(pending_count, 0)


if __name__ == "__main__":
    unittest.main()
