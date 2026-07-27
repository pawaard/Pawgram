import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.config import get_settings


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = str(Path(cls.temp_dir.name) / "test.db")
        get_settings.cache_clear()
        from app.main import app
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        cls.temp_dir.cleanup()

    def test_health(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_job_creation_with_schedule(self):
        from app.database import get_connection, utc_now

        now = utc_now()
        with get_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                ("Test", "+90 ***", "encrypted", "session", "Test User", now, now),
            )
            session_id = cursor.lastrowid
        resolved = [
            {"id": 1001, "title": "Kaynak", "username": "kaynak", "kind": "megagroup", "participants_count": 10, "creator": True, "admin_rights": True},
            {"id": 1002, "title": "Hedef", "username": "hedef", "kind": "megagroup", "participants_count": 5, "creator": True, "admin_rights": True},
        ]
        with patch("app.main.resolve_group", new=AsyncMock(side_effect=resolved)):
            response = self.client.post("/api/jobs", json={
                "name": "Planlı test",
                "session_id": session_id,
                "source_ref": "@kaynak",
                "target_ref": "@hedef",
                "scheduled_at": "2026-07-27T12:00:00+00:00",
                "working_start": "09:00",
                "working_end": "18:00",
            })
        self.assertEqual(response.status_code, 200, response.text)

    def test_proxy_settings_are_session_specific_and_encrypted(self):
        from app.database import get_connection, utc_now

        now = utc_now()
        with get_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                ("Proxy Test", "+90 ***", "encrypted", "session", "Proxy User", now, now),
            )
            session_id = cursor.lastrowid

        response = self.client.put(f"/api/sessions/{session_id}/proxy", json={
            "enabled": True,
            "proxy_type": "socks5",
            "host": "127.0.0.1",
            "port": 1080,
            "username": "proxy-user",
            "password": "proxy-secret",
        })
        self.assertEqual(response.status_code, 200, response.text)
        config = self.client.get(f"/api/sessions/{session_id}/proxy").json()
        self.assertTrue(config["enabled"])
        self.assertEqual(config["username"], "proxy-user")
        self.assertTrue(config["password_configured"])
        self.assertNotIn("password", config)
        with get_connection() as connection:
            stored = connection.execute(
                "SELECT proxy_username_encrypted, proxy_password_encrypted FROM telegram_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        self.assertNotEqual(stored["proxy_username_encrypted"], "proxy-user")
        self.assertNotEqual(stored["proxy_password_encrypted"], "proxy-secret")

        with patch("app.main.test_session_proxy", new=AsyncMock(return_value={"ok": True, "status": "success", "latency_ms": 42})):
            tested = self.client.post(f"/api/sessions/{session_id}/proxy/test")
        self.assertEqual(tested.status_code, 200, tested.text)
        self.assertEqual(tested.json()["latency_ms"], 42)

    def test_dashboard_is_empty_initially(self):
        from app.database import get_connection

        with get_connection() as connection:
            expected = connection.execute("SELECT COUNT(*) count FROM telegram_sessions").fetchone()["count"]
        response = self.client.get("/api/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sessions_total"], expected)

    def test_activity_scan_creation(self):
        response = self.client.post("/api/activity-scans", json={
            "name": "Son 24 saat",
            "session_id": None,
            "group_ref": "@ornekgrup",
            "window_hours": 24,
            "recurring": True,
            "interval_minutes": 1440,
        })
        self.assertEqual(response.status_code, 200, response.text)
        scans = self.client.get("/api/activity-scans")
        self.assertEqual(scans.status_code, 200)
        self.assertEqual(scans.json()[0]["window_hours"], 24)

    def test_approval_requires_selection_and_history_waits_for_invite(self):
        from app.database import get_connection, utc_now

        now = utc_now()
        with get_connection() as connection:
            session_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, created_at, updated_at
                ) VALUES ('Onay', '+90 ***', 'enc', 'session', 'Onay', 'active', ?, ?)
                """,
                (now, now),
            ).lastrowid
            job_id = connection.execute(
                """
                INSERT INTO transfer_jobs(
                    name, session_id, source_ref, source_id, target_ref, target_id,
                    status, previewed_at, candidate_count, created_at, updated_at
                ) VALUES ('Onay işi', ?, '@source', 100, '@target', 200, 'previewed', ?, 1, ?, ?)
                """,
                (session_id, now, now, now),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO job_candidates(
                    job_id, telegram_user_id, display_name, username, status, reason, created_at
                ) VALUES (?, 98765, 'Geçmiş Aday', 'gecmis_aday', 'eligible', 'Uygun', ?)
                """,
                (job_id, now),
            )
            connection.execute(
                "UPDATE job_candidates SET selected=1 WHERE job_id=?",
                (job_id,),
            )

        response = self.client.post(f"/api/jobs/{job_id}/approve")
        self.assertEqual(response.status_code, 200, response.text)
        with get_connection() as connection:
            history = connection.execute(
                "SELECT * FROM member_history WHERE telegram_user_id=98765"
            ).fetchone()
        self.assertIsNone(history)

    def test_rejects_same_source_and_target(self):
        response = self.client.post("/api/jobs", json={
            "name": "Test job",
            "session_id": 1,
            "source_ref": "@same",
            "target_ref": "@same",
        })
        self.assertEqual(response.status_code, 400)

    def test_telegram_settings_can_be_saved_from_panel(self):
        response = self.client.post("/api/settings/telegram", json={
            "api_id": 123456,
            "api_hash": "0123456789abcdef0123456789abcdef",
        })
        self.assertEqual(response.status_code, 200)
        settings = self.client.get("/api/settings/telegram").json()
        self.assertTrue(settings["configured"])
        self.assertEqual(settings["api_id"], 123456)
        self.assertNotIn("0123456789abcdef", settings["api_hash_masked"])

    def test_rotation_quota_can_be_saved_from_panel(self):
        response = self.client.post("/api/settings/rotation", json={"daily_quota": 25})
        self.assertEqual(response.status_code, 200, response.text)
        settings = self.client.get("/api/settings/rotation")
        self.assertEqual(settings.status_code, 200)
        self.assertEqual(settings.json()["mode"], "round_robin")
        self.assertEqual(settings.json()["daily_quota"], 25)
        self.assertFalse(settings.json()["switch_on_error"])
        self.assertFalse(settings.json()["switch_on_flood_wait"])

    def test_zz_admin_authentication(self):
        setup = self.client.post("/api/auth/setup", json={"password": "secure-test-password"})
        self.assertEqual(setup.status_code, 200)
        logout = self.client.post("/api/auth/logout")
        self.assertEqual(logout.status_code, 200)
        protected = self.client.get("/api/dashboard")
        self.assertEqual(protected.status_code, 401)
        login = self.client.post("/api/auth/login", json={"password": "secure-test-password"})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(self.client.get("/api/dashboard").status_code, 200)


if __name__ == "__main__":
    unittest.main()
