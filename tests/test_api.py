import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.config import get_settings


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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

    def test_runtime_shutdown_and_manual_update_endpoints(self):
        update_status = {
            "reachable": True,
            "checked_at": "2026-07-29T12:00:00+00:00",
            "current_version": "0.4.2",
            "latest_version": "0.4.3",
            "update_available": True,
            "channel": "stable",
            "message": "0.4.3 sürümü kullanılabilir.",
        }
        with (
            patch("app.main.shutdown_available", return_value=True),
            patch("app.main.fetch_update_status", new=AsyncMock(return_value=update_status)),
            patch("app.main.check_and_stage_update", return_value=True) as stage_update,
            patch("app.main.schedule_shutdown", return_value=True) as schedule,
        ):
            response = self.client.post("/api/settings/update-install")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["started"])
        self.assertEqual(response.json()["latest_version"], "0.4.3")
        stage_update.assert_called_once_with(raise_errors=True)
        schedule.assert_called_once_with()

        with patch("app.main.schedule_shutdown", return_value=True) as schedule:
            response = self.client.post("/api/system/shutdown")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["closing"])
        schedule.assert_called_once_with()

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

        with (
            patch("app.main.local_license_status", return_value=invalid),
            patch("app.main.schedule_shutdown", return_value=True) as schedule,
        ):
            response = self.client.post("/api/system/shutdown")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["closing"])
        schedule.assert_called_once_with()

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
        scans = self.client.get("/api/activity-scans")
        self.assertEqual(scans.status_code, 200)
        self.assertEqual(scans.json()[0]["window_hours"], 24)
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

        reused = self.client.put(f"/api/sessions/{session_id}/proxy", json={
            "enabled": True,
            "proxy_type": "http",
            "host": "proxy-reused.local",
            "port": 3128,
            "username": "reused-user",
            "password": "reused-secret",
        })
        self.assertEqual(reused.status_code, 200, reused.text)
        reused_config = self.client.get(f"/api/sessions/{session_id}/proxy").json()
        self.assertTrue(reused_config["enabled"])
        self.assertEqual(reused_config["proxy_type"], "http")
        self.assertEqual(reused_config["host"], "proxy-reused.local")
        self.assertEqual(reused_config["port"], 3128)
        self.assertEqual(reused_config["username"], "reused-user")
        self.assertTrue(reused_config["password_configured"])

    def test_session_invite_policy_is_saved_per_account(self):
        from app.database import get_connection, utc_now

        now = utc_now()
        with get_connection() as connection:
            session_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, created_at, updated_at
                ) VALUES ('Devir Test', '+90 ***', 'encrypted', 'session',
                          'Devir Test', 'active', ?, ?)
                """,
                (now, now),
            ).lastrowid

        saved = self.client.put(
            f"/api/sessions/{session_id}/invite-policy",
            json={"batch_limit": 4, "cooldown_minutes": 20},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["automatic_handoff"])
        self.assertTrue(saved.json()["reuse_candidates"])

        policy = self.client.get(f"/api/sessions/{session_id}/invite-policy")
        self.assertEqual(policy.status_code, 200, policy.text)
        self.assertEqual(policy.json()["batch_limit"], 4)
        self.assertEqual(policy.json()["cooldown_minutes"], 20)
        self.assertTrue(policy.json()["switch_on_error"])
        self.assertTrue(policy.json()["switch_on_flood_wait"])

        unlimited = self.client.put(
            f"/api/sessions/{session_id}/invite-policy",
            json={"batch_limit": 0, "cooldown_minutes": 0},
        )
        self.assertEqual(unlimited.status_code, 200, unlimited.text)
        policy = self.client.get(f"/api/sessions/{session_id}/invite-policy")
        self.assertEqual(policy.json()["batch_limit"], 0)
        self.assertEqual(policy.json()["cooldown_minutes"], 0)

    def test_session_list_includes_read_only_usage_and_recent_event_summary(self):
        from app.database import get_connection, utc_now

        now = utc_now()
        today = now[:10]
        session_id = None
        try:
            with get_connection() as connection:
                session_id = connection.execute(
                    """
                    INSERT INTO telegram_sessions(
                        label, phone_masked, phone_encrypted, session_encrypted,
                        display_name, username, status, proxy_enabled,
                        proxy_type, proxy_host, proxy_port, proxy_last_status,
                        batch_success_count, invite_batch_limit,
                        invite_cooldown_minutes, created_at, updated_at
                    ) VALUES (
                        'Session Özet', '+90 *** 7777', 'encrypted', 'session',
                        'Özet Kullanıcı', 'ozet_user', 'active', 1,
                        'socks5', 'proxy.test', 1080, 'success',
                        2, 3, 20, ?, ?
                    )
                    """,
                    (now, now),
                ).lastrowid
                connection.execute(
                    """
                    INSERT INTO session_invite_usage_daily(
                        session_id, usage_date, invite_count, last_used_at
                    ) VALUES (?, ?, 5, ?)
                    """,
                    (session_id, today, now),
                )
                connection.execute(
                    """
                    INSERT INTO session_usage_daily(
                        session_id, usage_date, operation_count, last_used_at
                    ) VALUES (?, ?, 7, ?)
                    """,
                    (session_id, today, now),
                )
                connection.execute(
                    """
                    INSERT INTO system_logs(level, category, message, session_id, created_at)
                    VALUES ('success', 'session_summary_test', 'Salt okunur özet olayı', ?, ?)
                    """,
                    (session_id, now),
                )
                connection.execute(
                    """
                    INSERT INTO session_operation_locks(
                        session_id, operation_type, operation_key,
                        operation_label, owner_token, acquired_at
                    ) VALUES (?, 'activity', 'summary-test', 'Test taraması', 'test-owner', ?)
                    """,
                    (session_id, now),
                )

            response = self.client.get("/api/sessions")
            self.assertEqual(response.status_code, 200, response.text)
            session = next(item for item in response.json() if item["id"] == session_id)
            self.assertEqual(session["today_invite_count"], 5)
            self.assertEqual(session["today_activity_count"], 7)
            self.assertEqual(session["batch_success_count"], 2)
            self.assertEqual(session["invite_batch_limit"], 3)
            self.assertEqual(session["last_successful_invite_at"], now)
            self.assertEqual(session["last_activity_at"], now)
            self.assertEqual(session["last_event_category"], "session_summary_test")
            self.assertEqual(session["last_event_message"], "Salt okunur özet olayı")
            self.assertEqual(session["operation_label"], "Test taraması")
            with get_connection() as connection:
                stored = connection.execute(
                    """
                    SELECT status, batch_success_count, invite_batch_limit,
                           invite_cooldown_minutes FROM telegram_sessions WHERE id=?
                    """,
                    (session_id,),
                ).fetchone()
            self.assertEqual(stored["status"], "active")
            self.assertEqual(stored["batch_success_count"], 2)
            self.assertEqual(stored["invite_batch_limit"], 3)
            index = self.client.get("/")
            self.assertEqual(index.status_code, 200)
            self.assertIn('id="session-search"', index.text)
            self.assertIn('id="session-status-filter"', index.text)
            self.assertIn('id="session-detail-modal"', index.text)
        finally:
            if session_id:
                with get_connection() as connection:
                    connection.execute("DELETE FROM system_logs WHERE session_id=?", (session_id,))
                    connection.execute("DELETE FROM telegram_sessions WHERE id=?", (session_id,))

    def test_group_access_batch_api_creates_ordered_session_queue(self):
        from app.database import get_connection, utc_now

        now = utc_now()
        session_ids = []
        with get_connection() as connection:
            for number in (1, 2):
                session_ids.append(connection.execute(
                    """
                    INSERT INTO telegram_sessions(
                        label, phone_masked, phone_encrypted, session_encrypted,
                        display_name, status, created_at, updated_at
                    ) VALUES (?, '+90 ***', 'enc', 'session', ?, 'active', ?, ?)
                    """,
                    (f"Hazırlık {number}", f"Hazırlık {number}", now, now),
                ).lastrowid)

        with patch("app.main.start_group_access_batch") as starter:
            response = self.client.post("/api/group-access-batches", json={
                "group_ref": "https://t.me/+PrivateInviteHash",
                "purpose": "target",
                "session_ids": session_ids,
                "min_delay_seconds": 15,
                "max_delay_seconds": 30,
            })
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["batch"]["total_count"], 2)
        self.assertEqual(body["batch"]["purpose"], "target")
        self.assertEqual([item["session_id"] for item in body["items"]], session_ids)
        self.assertEqual([item["position"] for item in body["items"]], [1, 2])
        starter.assert_called_once_with(body["batch"]["id"])

        listed = self.client.get("/api/group-access-batches")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertTrue(any(item["id"] == body["batch"]["id"] for item in listed.json()))

    def test_group_listing_exposes_safe_suitability_fields_and_ui_controls(self):
        import asyncio
        from types import SimpleNamespace

        from app.telegram_service import list_groups

        class FakeClient:
            async def iter_dialogs(self):
                yield SimpleNamespace(
                    is_group=True,
                    is_channel=True,
                    name="Yönetilen Megagroup",
                    unread_count=4,
                    entity=SimpleNamespace(
                        test_id=-1005001,
                        username="yonetilen",
                        megagroup=True,
                        creator=False,
                        admin_rights=SimpleNamespace(invite_users=True),
                        participants_count=250,
                    ),
                )
                yield SimpleNamespace(
                    is_group=False,
                    is_channel=True,
                    name="Yayın Kanalı",
                    unread_count=1,
                    entity=SimpleNamespace(
                        test_id=-1005002,
                        username="yayin",
                        megagroup=False,
                        creator=False,
                        admin_rights=None,
                        participants_count=900,
                    ),
                )

            async def disconnect(self):
                return None

        lease = SimpleNamespace(release=AsyncMock())
        with patch(
            "app.telegram_service.acquire_session_operation",
            new=AsyncMock(return_value=lease),
        ), patch(
            "app.telegram_service._client_for",
            new=AsyncMock(return_value=FakeClient()),
        ), patch(
            "app.telegram_service.utils.get_peer_id",
            side_effect=lambda entity: entity.test_id,
        ):
            groups = asyncio.run(list_groups(1))

        self.assertEqual(groups[0]["kind"], "megagroup")
        self.assertTrue(groups[0]["admin_rights"])
        self.assertTrue(groups[0]["can_invite_users"])
        self.assertTrue(groups[0]["source_suitable"])
        self.assertTrue(groups[0]["target_suitable"])
        self.assertEqual(groups[0]["participants_count"], 250)
        self.assertEqual(groups[1]["kind"], "channel")
        self.assertFalse(groups[1]["source_suitable"])
        self.assertFalse(groups[1]["target_suitable"])
        lease.release.assert_awaited_once()

        index = self.client.get("/")
        self.assertEqual(index.status_code, 200)
        for element_id in (
            "group-search",
            "group-kind-filter",
            "group-suitability-filter",
            "group-sort",
            "group-detail-modal",
            "group-access-result-search",
            "group-access-result-filter",
        ):
            self.assertIn(f'id="{element_id}"', index.text)

    def test_session_health_batch_api_creates_non_destructive_preflight(self):
        from app.database import get_connection, utc_now

        now = utc_now()
        with get_connection() as connection:
            session_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, created_at, updated_at
                ) VALUES ('Sağlık API', '+90 ***', 'enc', 'session',
                          'Sağlık API', 'active', ?, ?)
                """,
                (now, now),
            ).lastrowid

        with patch("app.main.start_session_health_batch") as starter:
            response = self.client.post("/api/session-health-batches", json={
                "session_ids": [session_id],
                "source_ref": "@source",
                "target_ref": "@target",
            })
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["batch"]["total_count"], 1)
        self.assertEqual(body["batch"]["source_ref"], "@source")
        self.assertEqual(body["batch"]["target_ref"], "@target")
        self.assertEqual(body["items"][0]["status"], "queued")
        starter.assert_called_once_with(body["batch"]["id"])

    def test_backup_contains_database_and_required_encryption_key(self):
        from app.security import encrypt

        encrypt("backup-key-seed")
        response = self.client.post("/api/backups")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["name"].endswith(".zip"))
        self.assertTrue(result["secret_included"])
        backup_path = get_settings().database_path.resolve().parent / "backups" / result["name"]
        with zipfile.ZipFile(backup_path) as archive:
            self.assertIn("console.db", archive.namelist())
            self.assertIn(".secret_key", archive.namelist())
            self.assertIn("backup-info.json", archive.namelist())

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

    def test_dashboard_reports_read_only_attention_operations_and_today_summary(self):
        from app.database import get_connection, utc_now

        now = utc_now()
        today = now[:10]
        baseline = self.client.get("/api/dashboard").json()
        ids = {}
        try:
            with get_connection() as connection:
                ids["session"] = connection.execute(
                    """
                    INSERT INTO telegram_sessions(
                        label, phone_masked, phone_encrypted, session_encrypted,
                        display_name, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'proxy_error', ?, ?)
                    """,
                    ("Dashboard Test", "+90 *** 9999", "encrypted", "session", "Panel User", now, now),
                ).lastrowid
                connection.execute(
                    """
                    INSERT INTO session_operation_locks(
                        session_id, operation_type, operation_key,
                        operation_label, owner_token, acquired_at
                    ) VALUES (?, 'invite', 'dashboard-test', 'Test üye ekleme', 'test-owner', ?)
                    """,
                    (ids["session"], now),
                )
                connection.execute(
                    """
                    INSERT INTO session_invite_usage_daily(session_id, usage_date, invite_count, last_used_at)
                    VALUES (?, ?, 4, ?)
                    """,
                    (ids["session"], today, now),
                )
                ids["job"] = connection.execute(
                    """
                    INSERT INTO transfer_jobs(
                        name, session_id, source_ref, target_ref, status, created_at, updated_at
                    ) VALUES ('Dashboard Job', ?, '@source', '@target', 'failed', ?, ?)
                    """,
                    (ids["session"], now, now),
                ).lastrowid
                connection.executemany(
                    """
                    INSERT INTO job_candidates(
                        job_id, telegram_user_id, display_name, status,
                        selected, processed_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (ids["job"], 990000001, "Skipped", "skipped", 0, now, now),
                        (ids["job"], 990000002, "Failed", "failed", 0, now, now),
                        (ids["job"], 990000003, "Waiting", "eligible", 1, None, now),
                    ],
                )
                ids["attention_scan"] = connection.execute(
                    """
                    INSERT INTO activity_scans(
                        name, group_ref, window_hours, status, created_at, updated_at
                    ) VALUES ('Dashboard Attention', '@attention', 24, 'paused', ?, ?)
                    """,
                    (now, now),
                ).lastrowid
                ids["completed_scan"] = connection.execute(
                    """
                    INSERT INTO activity_scans(
                        name, group_ref, window_hours, status, last_run_at, created_at, updated_at
                    ) VALUES ('Dashboard Complete', '@complete', 24, 'completed', ?, ?, ?)
                    """,
                    (now, now, now),
                ).lastrowid
                connection.execute(
                    """
                    INSERT INTO activity_results(
                        scan_id, telegram_user_id, display_name, message_count,
                        last_message_at, created_at
                    ) VALUES (?, 990000004, 'Active User', 3, ?, ?)
                    """,
                    (ids["completed_scan"], now, now),
                )
                ids["group_batch"] = connection.execute(
                    """
                    INSERT INTO group_access_batches(group_ref, status, created_at, updated_at)
                    VALUES ('@dashboard', 'completed', ?, ?)
                    """,
                    (now, now),
                ).lastrowid
                connection.execute(
                    """
                    INSERT INTO group_access_items(batch_id, session_id, position, status)
                    VALUES (?, ?, 1, 'approval_pending')
                    """,
                    (ids["group_batch"], ids["session"]),
                )
                ids["health"] = connection.execute(
                    """
                    INSERT INTO session_health_batches(
                        status, total_count, processed_count, ready_count,
                        warning_count, failed_count, created_at, updated_at, finished_at
                    ) VALUES ('completed', 3, 3, 1, 1, 1, ?, ?, ?)
                    """,
                    (now, now, now),
                ).lastrowid

            response = self.client.get("/api/dashboard")
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(result["alerts"]["proxy_attention"], baseline["alerts"]["proxy_attention"] + 1)
            self.assertEqual(result["alerts"]["pending_group_approvals"], baseline["alerts"]["pending_group_approvals"] + 1)
            self.assertEqual(result["alerts"]["job_attention"], baseline["alerts"]["job_attention"] + 1)
            self.assertEqual(result["alerts"]["activity_attention"], baseline["alerts"]["activity_attention"] + 1)
            operation = next(item for item in result["active_operations"] if item["session_id"] == ids["session"])
            self.assertEqual(operation["operation_label"], "Test üye ekleme")
            self.assertEqual(result["today"]["invited"], baseline["today"]["invited"] + 4)
            self.assertEqual(result["today"]["skipped"], baseline["today"]["skipped"] + 1)
            self.assertEqual(result["today"]["failed"], baseline["today"]["failed"] + 1)
            self.assertEqual(result["today"]["unique_active_users"], baseline["today"]["unique_active_users"] + 1)
            self.assertEqual(result["today"]["completed_scans"], baseline["today"]["completed_scans"] + 1)
            self.assertEqual(result["today"]["remaining_candidates"], baseline["today"]["remaining_candidates"] + 1)
            self.assertEqual(result["latest_health"]["id"], ids["health"])
            self.assertEqual(result["latest_health"]["ready_count"], 1)
        finally:
            with get_connection() as connection:
                if ids.get("group_batch"):
                    connection.execute("DELETE FROM group_access_batches WHERE id=?", (ids["group_batch"],))
                if ids.get("completed_scan"):
                    connection.execute("DELETE FROM activity_scans WHERE id=?", (ids["completed_scan"],))
                if ids.get("attention_scan"):
                    connection.execute("DELETE FROM activity_scans WHERE id=?", (ids["attention_scan"],))
                if ids.get("health"):
                    connection.execute("DELETE FROM session_health_batches WHERE id=?", (ids["health"],))
                if ids.get("job"):
                    connection.execute("DELETE FROM transfer_jobs WHERE id=?", (ids["job"],))
                if ids.get("session"):
                    connection.execute("DELETE FROM telegram_sessions WHERE id=?", (ids["session"],))

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

    def test_activity_metric_counts_unique_users_across_scans_and_delete_recomputes_it(self):
        from app.database import get_connection, utc_now

        now = utc_now()
        with get_connection() as connection:
            baseline = connection.execute(
                "SELECT COUNT(DISTINCT telegram_user_id) count FROM activity_results"
            ).fetchone()["count"]
            first_scan = connection.execute(
                """
                INSERT INTO activity_scans(
                    name, group_ref, window_hours, status, message_count,
                    unique_users, created_at, updated_at
                ) VALUES ('Benzersiz 1', '@unique', 24, 'completed', 10, 2, ?, ?)
                """,
                (now, now),
            ).lastrowid
            second_scan = connection.execute(
                """
                INSERT INTO activity_scans(
                    name, group_ref, window_hours, status, message_count,
                    unique_users, created_at, updated_at
                ) VALUES ('Benzersiz 2', '@unique', 24, 'completed', 12, 2, ?, ?)
                """,
                (now, now),
            ).lastrowid
            connection.executemany(
                """
                INSERT INTO activity_results(
                    scan_id, telegram_user_id, display_name, message_count,
                    last_message_at, created_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                """,
                [
                    (first_scan, 990000001, "Ortak kullanıcı", now, now),
                    (first_scan, 990000002, "Sadece ilk", now, now),
                    (second_scan, 990000001, "Ortak kullanıcı", now, now),
                    (second_scan, 990000003, "Sadece ikinci", now, now),
                ],
            )

        listed = self.client.get("/api/activity-scans")
        self.assertEqual(listed.status_code, 200, listed.text)
        rows = listed.json()
        matching = [row for row in rows if row["id"] in {first_scan, second_scan}]
        self.assertEqual(len(matching), 2)
        self.assertTrue(
            all(row["global_unique_users"] == baseline + 3 for row in matching)
        )

        deleted = self.client.delete(f"/api/activity-scans/{first_scan}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["deleted_results"], 2)
        self.assertEqual(deleted.json()["unique_active_users"], baseline + 2)
        with get_connection() as connection:
            self.assertIsNone(connection.execute(
                "SELECT id FROM activity_scans WHERE id=?", (first_scan,)
            ).fetchone())
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) count FROM activity_results WHERE scan_id=?",
                (first_scan,),
            ).fetchone()["count"], 0)

    def test_running_activity_scan_must_be_paused_before_delete(self):
        from app.database import get_connection, utc_now

        now = utc_now()
        with get_connection() as connection:
            scan_id = connection.execute(
                """
                INSERT INTO activity_scans(
                    name, group_ref, window_hours, status, created_at, updated_at
                ) VALUES ('Silme koruması', '@protected', 24, 'running', ?, ?)
                """,
                (now, now),
            ).lastrowid
        response = self.client.delete(f"/api/activity-scans/{scan_id}")
        self.assertEqual(response.status_code, 409, response.text)
        with get_connection() as connection:
            connection.execute("DELETE FROM activity_scans WHERE id=?", (scan_id,))

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

    def test_execute_job_starts_in_background_from_the_button_request(self):
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
                    approved_at, candidate_count, working_start, working_end,
                    created_at, updated_at
                ) VALUES ('Worker işi', ?, '@source', '@target', 'approved', ?, ?, 1,
                          '00:00', '00:00', ?, ?)
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

        with patch("app.main.start_invite_job") as starter:
            response = self.client.post(f"/api/jobs/{job_id}/execute")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "queued_execution")
        self.assertEqual(response.json()["succeeded"], 0)
        starter.assert_called_once_with(job_id)
        with get_connection() as connection:
            job = connection.execute(
                "SELECT status FROM transfer_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        self.assertEqual(job["status"], "queued_execution")

    def test_future_job_is_scheduled_and_locked_until_its_start_time(self):
        from datetime import UTC, datetime, timedelta

        from app.database import get_connection, utc_now

        now = utc_now()
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        with get_connection() as connection:
            session_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, created_at, updated_at
                ) VALUES ('Plan', '+90 ***', 'enc', 'session', 'Plan', 'active', ?, ?)
                """,
                (now, now),
            ).lastrowid
            job_id = connection.execute(
                """
                INSERT INTO transfer_jobs(
                    name, session_id, source_ref, target_ref, status, previewed_at,
                    approved_at, scheduled_at, working_start, working_end,
                    candidate_count, created_at, updated_at
                ) VALUES ('Planlı worker', ?, '@source', '@target', 'approved', ?, ?, ?,
                          '00:00', '00:00', 1, ?, ?)
                """,
                (session_id, now, now, future, now, now),
            ).lastrowid
            candidate_id = connection.execute(
                """
                INSERT INTO job_candidates(
                    job_id, telegram_user_id, display_name, access_hash, source_message_id,
                    status, reason, selected, created_at
                ) VALUES (?, 54321, 'Planlı Aday', 111222, 456, 'eligible', 'Uygun', 1, ?)
                """,
                (job_id, now),
            ).lastrowid

        with patch("app.main.start_invite_job") as starter:
            response = self.client.post(f"/api/jobs/{job_id}/execute")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "scheduled")
        starter.assert_not_called()
        locked = self.client.put(
            f"/api/jobs/{job_id}/candidates/selection",
            json={"candidate_ids": [candidate_id]},
        )
        self.assertEqual(locked.status_code, 409)
        with get_connection() as connection:
            job = connection.execute(
                "SELECT status, resume_at FROM transfer_jobs WHERE id=?", (job_id,)
            ).fetchone()
        self.assertEqual(job["status"], "scheduled")
        self.assertIsNotNone(job["resume_at"])

    def test_completed_job_cannot_be_repreviewed(self):
        from app.database import get_connection, utc_now

        now = utc_now()
        with get_connection() as connection:
            session_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, status, created_at, updated_at
                ) VALUES ('Tamam', '+90 ***', 'enc', 'session', 'Tamam', 'active', ?, ?)
                """,
                (now, now),
            ).lastrowid
            job_id = connection.execute(
                """
                INSERT INTO transfer_jobs(
                    name, session_id, source_ref, target_ref, status, created_at, updated_at
                ) VALUES ('Tamamlanan', ?, '@source', '@target', 'completed', ?, ?)
                """,
                (session_id, now, now),
            ).lastrowid
        with patch("app.main.preview_job_candidates", new=AsyncMock()) as preview:
            response = self.client.post(f"/api/jobs/{job_id}/preview")
        self.assertEqual(response.status_code, 409)
        preview.assert_not_awaited()

    def test_rejects_same_source_and_target(self):
        response = self.client.post("/api/jobs", json={
            "name": "Test job",
            "session_id": 1,
            "source_ref": "@same",
            "target_ref": "@same",
        })
        self.assertEqual(response.status_code, 400)

    def test_settings_overview_and_diagnostics_are_redacted(self):
        import json

        from app.database import get_connection, utc_now

        now = utc_now()
        secrets = (
            "SECRET_PHONE_5555",
            "SECRET_SESSION_PAYLOAD",
            "secret.proxy.internal",
            "secret_proxy_user",
        )
        with get_connection() as connection:
            session_id = connection.execute(
                """
                INSERT INTO telegram_sessions(
                    label, phone_masked, phone_encrypted, session_encrypted,
                    display_name, username, status, proxy_enabled, proxy_type,
                    proxy_host, proxy_port, proxy_username_encrypted,
                    proxy_password_encrypted, proxy_last_status,
                    created_at, updated_at
                ) VALUES (
                    'Tanılama Test', ?, 'SECRET_PHONE_ENCRYPTED', ?,
                    'Gizli Kullanıcı', ?, 'active', 1, 'socks5',
                    ?, 1080, 'SECRET_PROXY_USER_ENCRYPTED',
                    'SECRET_PROXY_PASSWORD_ENCRYPTED', 'success', ?, ?
                )
                """,
                (secrets[0], secrets[1], secrets[3], secrets[2], now, now),
            ).lastrowid
        try:
            overview_response = self.client.get("/api/settings/overview")
            self.assertEqual(overview_response.status_code, 200, overview_response.text)
            overview = overview_response.json()
            self.assertIn("configuration", overview)
            self.assertIn("sessions", overview)
            self.assertIn("backup", overview)
            self.assertIn("update", overview)
            self.assertFalse(overview["security"]["secrets_included"])
            self.assertGreaterEqual(overview["sessions"]["proxy_healthy"], 1)

            report_response = self.client.get("/api/settings/diagnostics/report")
            self.assertEqual(report_response.status_code, 200, report_response.text)
            self.assertIn("attachment;", report_response.headers["content-disposition"])
            report = report_response.json()
            self.assertFalse(report["report"]["contains_credentials"])
            serialized = json.dumps(report, ensure_ascii=False)
            for secret in secrets:
                self.assertNotIn(secret, serialized)
            self.assertNotIn("SECRET_PROXY_PASSWORD_ENCRYPTED", serialized)
            self.assertNotIn("SECRET_PHONE_ENCRYPTED", serialized)

            update_status = {
                "reachable": True,
                "checked_at": now,
                "current_version": "0.3.0",
                "latest_version": "0.3.1",
                "update_available": True,
                "channel": "stable",
                "message": "0.3.1 sürümü kullanılabilir.",
            }
            with patch(
                "app.main.fetch_update_status",
                new=AsyncMock(return_value=update_status),
            ):
                update_response = self.client.get("/api/settings/update-status")
            self.assertEqual(update_response.status_code, 200, update_response.text)
            self.assertTrue(update_response.json()["update_available"])

            index = self.client.get("/")
            for element_id in (
                "settings-health-api",
                "settings-search",
                "settings-unsaved-warning",
                "settings-validation-summary",
                "check-settings-update",
                "download-diagnostics",
            ):
                self.assertIn(f'id="{element_id}"', index.text)
        finally:
            with get_connection() as connection:
                connection.execute("DELETE FROM telegram_sessions WHERE id=?", (session_id,))

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

    def test_logs_page_filters_and_exports_redacted_records(self):
        from app.database import get_connection, utc_now

        category = "safe_log_export_test"
        secret_proxy = "proxy.example.com:44445:test-user:test-password"
        secret_phone = "+905551112233"
        secret_hash = "0123456789abcdef0123456789abcdef"
        with get_connection() as connection:
            log_id = connection.execute(
                """
                INSERT INTO system_logs(level, category, message, session_id, job_id, created_at)
                VALUES ('error', ?, ?, 77, 88, ?)
                """,
                (
                    category,
                    f"Proxy {secret_proxy} telefon {secret_phone} API Hash={secret_hash}",
                    utc_now(),
                ),
            ).lastrowid
        try:
            filtered = self.client.get(
                "/api/logs",
                params={"category": category, "session_id": 77, "job_id": 88},
            )
            self.assertEqual(filtered.status_code, 200, filtered.text)
            self.assertEqual(len(filtered.json()), 1)
            self.assertEqual(filtered.json()[0]["id"], log_id)

            exported = self.client.get(
                "/api/logs/export",
                params={"format": "json", "category": category},
            )
            self.assertEqual(exported.status_code, 200, exported.text)
            self.assertIn("attachment;", exported.headers["content-disposition"])
            serialized = exported.text
            self.assertNotIn(secret_proxy, serialized)
            self.assertNotIn(secret_phone, serialized)
            self.assertNotIn(secret_hash, serialized)
            self.assertIn("***", serialized)

            csv_export = self.client.get(
                "/api/logs/export",
                params={"format": "csv", "category": category},
            )
            self.assertEqual(csv_export.status_code, 200, csv_export.text)
            self.assertNotIn(secret_proxy, csv_export.text)

            index = self.client.get("/")
            for element_id in (
                "log-search",
                "log-level-filter",
                "toggle-log-refresh",
                "export-logs-json",
                "log-detail-modal",
            ):
                self.assertIn(f'id="{element_id}"', index.text)
            self.assertIn("https://t.me/PawardCode", index.text)
        finally:
            with get_connection() as connection:
                connection.execute("DELETE FROM system_logs WHERE id=?", (log_id,))

    def test_rotation_quota_can_be_saved_from_panel(self):
        response = self.client.post("/api/settings/rotation", json={"daily_quota": 25})
        self.assertEqual(response.status_code, 200, response.text)
        settings = self.client.get("/api/settings/rotation")
        self.assertEqual(settings.status_code, 200)
        self.assertEqual(settings.json()["mode"], "round_robin")
        self.assertEqual(settings.json()["daily_quota"], 25)
        self.assertFalse(settings.json()["switch_on_error"])
        self.assertFalse(settings.json()["switch_on_flood_wait"])

    def test_heartbeat_settings_and_overview_api(self):
        initial = self.client.get("/api/heartbeat")
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertFalse(initial.json()["settings"]["enabled"])
        self.assertEqual(initial.json()["settings"]["message_template"], "Merhabaa")

        missing_group = self.client.post(
            "/api/heartbeat/settings",
            json={
                "enabled": True,
                "interval_minutes": 60,
                "group_id": "",
                "message_template": "Merhabaa",
            },
        )
        self.assertEqual(missing_group.status_code, 400, missing_group.text)

        saved = self.client.post(
            "/api/heartbeat/settings",
            json={
                "enabled": True,
                "interval_minutes": 15,
                "group_id": "-1001234567890",
                "message_template": "Merhabaa",
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        overview = self.client.get("/api/heartbeat")
        self.assertEqual(overview.status_code, 200, overview.text)
        self.assertTrue(overview.json()["settings"]["enabled"])
        self.assertEqual(overview.json()["settings"]["interval_minutes"], 15)
        self.assertEqual(overview.json()["settings"]["group_id"], "-1001234567890")
        for session in overview.json()["sessions"]:
            self.assertIn("last_heartbeat_at", session)
            self.assertIn("last_success_at", session)
            self.assertIn("last_failure_at", session)
            self.assertIn("success_count", session)
            self.assertIn("failure_count", session)
            self.assertIn("current_status", session)
            self.assertIn("next_heartbeat_at", session)

        disabled = self.client.post(
            "/api/heartbeat/settings",
            json={
                "enabled": False,
                "interval_minutes": 60,
                "group_id": "-1001234567890",
                "message_template": "Merhabaa",
            },
        )
        self.assertEqual(disabled.status_code, 200, disabled.text)

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
