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

    def test_commercial_api_is_locked_without_valid_license(self):
        invalid = {
            "required": True,
            "valid": False,
            "status": "not_activated",
            "message": "Pawgram lisansı etkinleştirilmedi.",
        }
        with patch("app.main.local_license_status", return_value=invalid):
            response = self.client.get("/api/dashboard")
        self.assertEqual(response.status_code, 402)
        self.assertTrue(response.json()["license_required"])

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
        with get_connection() as connection:
            pending = connection.execute(
                "SELECT status FROM telegram_sessions WHERE id=?", (session_id,)
            ).fetchone()
        self.assertEqual(pending["status"], "proxy_pending")
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

        deleted = self.client.delete(f"/api/sessions/{session_id}/proxy")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertTrue(deleted.json()["fail_closed"])
        with get_connection() as connection:
            cleared = connection.execute(
                """
                SELECT proxy_enabled, proxy_type, proxy_host, proxy_port,
                       proxy_username_encrypted, proxy_password_encrypted,
                       proxy_last_status, proxy_latency_ms, proxy_last_error,
                       proxy_last_test_at, status, last_error
                FROM telegram_sessions WHERE id=?
                """,
                (session_id,),
            ).fetchone()
        self.assertEqual(cleared["proxy_enabled"], 0)
        self.assertEqual(cleared["status"], "proxy_error")
        self.assertIn("ana IP", cleared["last_error"])
        for field in (
            "proxy_type", "proxy_host", "proxy_port", "proxy_username_encrypted",
            "proxy_password_encrypted", "proxy_last_status", "proxy_latency_ms",
            "proxy_last_error", "proxy_last_test_at",
        ):
            self.assertIsNone(cleared[field], field)

    def test_proxy_health_distinguishes_pending_error_and_batch_wait(self):
        from app.database import get_connection, utc_now

        now = utc_now()
        with get_connection() as connection:
            ids = []
            for label, status, enabled, last_status in (
                ("Pending", "proxy_pending", 1, None),
                ("Failed", "proxy_error", 1, "failed"),
                ("Waiting", "batch_wait", 1, "success"),
            ):
                ids.append(connection.execute(
                    """
                    INSERT INTO telegram_sessions(
                        label, phone_masked, phone_encrypted, session_encrypted,
                        display_name, status, proxy_enabled, proxy_type, proxy_host,
                        proxy_port, proxy_last_status, batch_cooldown_until,
                        created_at, updated_at
                    ) VALUES (?, '+90 ***', 'enc', 'session', ?, ?, ?, 'http',
                              'proxy.local', 8080, ?, '2099-01-01T00:00:00+00:00', ?, ?)
                    """,
                    (label, label, status, enabled, last_status, now, now),
                ).lastrowid)
        sessions = {item["id"]: item for item in self.client.get("/api/sessions").json()}
        self.assertEqual(sessions[ids[0]]["health_score"], 50)
        self.assertEqual(sessions[ids[0]]["health_label"], "Proxy testi bekliyor")
        self.assertEqual(sessions[ids[1]]["health_score"], 0)
        self.assertEqual(sessions[ids[1]]["health_label"], "Proxy çalışmıyor")
        self.assertEqual(sessions[ids[2]]["health_score"], 85)
        self.assertEqual(sessions[ids[2]]["health_label"], "Parti beklemesi")

    def test_bulk_proxy_import_assigns_only_empty_sessions(self):
        from app.database import get_connection, utc_now

        now = utc_now()
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE telegram_sessions
                SET proxy_enabled=1, proxy_type='socks5', proxy_host='occupied.local', proxy_port=9999
                WHERE TRIM(COALESCE(proxy_host, ''))=''
                """
            )
            fixed_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, proxy_enabled, proxy_type, proxy_host, proxy_port,
                    created_at, updated_at
                ) VALUES ('Sabit', '+90 ***', 'enc', 'session', 'Sabit', 'proxy_error',
                          0, 'socks5', 'keep.proxy', 1080, ?, ?)
                """,
                (now, now),
            ).lastrowid
            empty_ids = [
                connection.execute(
                    """
                    INSERT INTO telegram_sessions(
                        label, phone_masked, phone_encrypted, session_encrypted,
                        display_name, status, created_at, updated_at
                    ) VALUES (?, '+90 ***', 'enc', 'session', ?, 'proxy_error', ?, ?)
                    """,
                    (f"Boş {index}", f"Boş {index}", now, now),
                ).lastrowid
                for index in (1, 2)
            ]

        response = self.client.post(
            "/api/proxies/bulk-assign",
            json={
                "default_proxy_type": "socks5",
                "content": (
                    "10.0.0.1:1080:user1:pass1\n"
                    "user2:pass2@10.0.0.2:1081\n"
                    "http://user3:pass3@10.0.0.3:8080\n"
                    "gecersiz-satir\n"
                ),
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["assigned_count"], 2)
        self.assertEqual(len(result["invalid_lines"]), 1)
        self.assertEqual(result["unused_proxy_count"], 1)
        self.assertEqual(result["unassigned_session_count"], 0)
        with get_connection() as connection:
            fixed = connection.execute(
                "SELECT proxy_host, proxy_port FROM telegram_sessions WHERE id=?",
                (fixed_id,),
            ).fetchone()
            assigned = connection.execute(
                """
                SELECT id, status, proxy_host, proxy_port,
                       proxy_username_encrypted, proxy_password_encrypted
                FROM telegram_sessions WHERE id IN (?, ?) ORDER BY id
                """,
                tuple(empty_ids),
            ).fetchall()
        self.assertEqual(fixed["proxy_host"], "keep.proxy")
        self.assertEqual(fixed["proxy_port"], 1080)
        self.assertEqual([row["proxy_host"] for row in assigned], ["10.0.0.1", "10.0.0.2"])
        self.assertTrue(all(row["status"] == "proxy_pending" for row in assigned))
        self.assertNotEqual(assigned[0]["proxy_username_encrypted"], "user1")
        self.assertNotEqual(assigned[0]["proxy_password_encrypted"], "pass1")

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

    def test_completed_activity_scan_prepares_transfer_and_selects_candidates(self):
        from app.database import get_connection, utc_now

        now = utc_now()
        with get_connection() as connection:
            session_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, created_at, updated_at
                ) VALUES ('Akış', '+90 ***', 'enc', 'session', 'Akış', 'active', ?, ?)
                """,
                (now, now),
            ).lastrowid
            scan_id = connection.execute(
                """
                INSERT INTO activity_scans(
                    name, session_id, group_ref, group_id, group_title, window_hours,
                    status, last_run_at, unique_users, created_at, updated_at
                ) VALUES ('Hızlı tarama', ?, '-100111', 111, 'Kaynak', 24,
                          'completed', ?, 250, ?, ?)
                """,
                (session_id, now, now, now),
            ).lastrowid

        async def fake_preview(job):
            with get_connection() as connection:
                connection.execute(
                    """
                    INSERT INTO job_candidates(
                        job_id, telegram_user_id, display_name, access_hash,
                        status, reason, created_at
                    ) VALUES (?, 77, 'Aktif Üye', 123456, 'eligible', 'Uygun', ?)
                    """,
                    (job["id"], now),
                )
                connection.execute(
                    "UPDATE transfer_jobs SET status='previewed', previewed_at=?, candidate_count=1 WHERE id=?",
                    (now, job["id"]),
                )
            return {"eligible": 1, "permissions": {"can_invite_users": True}}

        target = {
            "id": 222,
            "title": "Hedef",
            "username": "hedef",
            "kind": "megagroup",
            "participants_count": 5,
            "creator": True,
            "admin_rights": True,
        }
        with patch("app.main.resolve_group", new=AsyncMock(return_value=target)), patch(
            "app.main.preview_job_candidates", new=AsyncMock(side_effect=fake_preview)
        ):
            response = self.client.post(
                f"/api/activity-scans/{scan_id}/prepare-transfer",
                json={"target_ref": "@hedef", "max_users": 100},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["selected_count"], 1)
        with get_connection() as connection:
            job = connection.execute(
                "SELECT * FROM transfer_jobs WHERE id=?", (response.json()["job_id"],)
            ).fetchone()
            candidate = connection.execute(
                "SELECT * FROM job_candidates WHERE job_id=?", (job["id"],)
            ).fetchone()
        self.assertEqual(job["max_users"], 100)
        self.assertEqual(candidate["selected"], 1)

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

    def test_execute_job_runs_directly_from_the_button_request(self):
        from app.database import get_connection, utc_now

        now = utc_now()
        with get_connection() as connection:
            session_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, created_at, updated_at
                ) VALUES ('Worker', '+90 ***', 'enc', 'session', 'Worker', 'active', ?, ?)
                """,
                (now, now),
            ).lastrowid
            job_id = connection.execute(
                """
                INSERT INTO transfer_jobs(
                    name, session_id, source_ref, target_ref, status, previewed_at,
                    approved_at, candidate_count, created_at, updated_at
                ) VALUES ('Worker işi', ?, '@source', '@target', 'approved', ?, ?, 1, ?, ?)
                """,
                (session_id, now, now, now, now),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO job_candidates(
                    job_id, telegram_user_id, display_name, access_hash, source_message_id,
                    status, reason, selected, created_at
                ) VALUES (?, 12345, 'Worker Adayı', 987654, 321, 'eligible', 'Uygun', 1, ?)
                """,
                (job_id, now),
            )

        async def fake_execute(requested_job_id):
            with get_connection() as connection:
                connection.execute(
                    """
                    UPDATE transfer_jobs
                    SET status='completed', processed=1, succeeded=1, updated_at=?
                    WHERE id=?
                    """,
                    (utc_now(), requested_job_id),
                )

        executor = AsyncMock(side_effect=fake_execute)
        with patch("app.main.execute_invite_job", executor):
            response = self.client.post(f"/api/jobs/{job_id}/execute")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "completed")
        self.assertEqual(response.json()["succeeded"], 1)
        executor.assert_awaited_once_with(job_id)
        with get_connection() as connection:
            job = connection.execute(
                "SELECT status FROM transfer_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        self.assertEqual(job["status"], "completed")

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
