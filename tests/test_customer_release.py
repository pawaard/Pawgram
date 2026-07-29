import sqlite3
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.database import initialize_database
from app.release_history import (
    acknowledge_release_notes,
    initialize_release_tracking,
    release_notes_overview,
)
from app.schemas import (
    ActivityTransferRequest,
    CandidateSelectionRequest,
    JobCreateRequest,
)
from scripts.verify_customer_release import verify_release_folder, verify_release_zip


class CustomerReleaseTests(unittest.TestCase):
    def test_session_login_modal_scrolls_on_short_screens(self):
        static_root = Path(__file__).resolve().parent.parent / "static"
        css = (static_root / "features.css").read_text(encoding="utf-8")
        html = (static_root / "index.html").read_text(encoding="utf-8")
        javascript = (static_root / "app.js").read_text(encoding="utf-8")
        self.assertIn("#session-modal .modal", css)
        self.assertIn("--pawgram-viewport-height", css)
        self.assertIn("overflow-y: auto", css)
        self.assertIn("#login-step-phone { display: grid", css)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", css)
        self.assertIn("@media (max-height: 800px)", css)
        self.assertIn('/static/features.css?v=0.4.4', html)
        self.assertIn('/static/app.js?v=0.4.4', html)
        self.assertNotIn('id="activity-transfer-max" type="number" min="1" max="1000"', html)
        self.assertIn("syncViewportMetrics", javascript)
        self.assertIn('root.dataset.viewportMode', javascript)
        self.assertIn("body.modal-open", css)
        self.assertIn(".modal-close { position: sticky", css)
        self.assertIn("syncModalPageState", javascript)
        self.assertIn('closeModal(event.currentTarget.closest(".modal-backdrop"))', javascript)
        self.assertNotIn("event.target === item) closeModals()", javascript)

    def test_member_preparation_limit_is_not_capped_at_one_thousand(self):
        activity = ActivityTransferRequest(target_ref="@target", max_users=4273)
        job = JobCreateRequest(
            name="Büyük aktarım",
            session_id=1,
            source_ref="@source",
            target_ref="@target",
            max_users=4273,
        )
        self.assertEqual(activity.max_users, 4273)
        self.assertEqual(job.max_users, 4273)
        selection = CandidateSelectionRequest(candidate_ids=list(range(1, 4274)))
        self.assertEqual(len(selection.candidate_ids), 4273)

    def test_customer_ui_exposes_runtime_and_update_controls(self):
        static_root = Path(__file__).resolve().parent.parent / "static"
        html = (static_root / "index.html").read_text(encoding="utf-8")
        javascript = (static_root / "app.js").read_text(encoding="utf-8")
        for element_id in ("shutdown-app", "install-settings-update", "runtime-overlay"):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("installSettingsUpdate", javascript)
        self.assertIn("shutdownApplication", javascript)

    def test_customer_ui_describes_current_invite_rotation(self):
        html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "FloodWait veya PeerFlood alan hesap beklemeye alınır ve sıradaki uygun hesapla devam edilir.",
            html,
        )
        self.assertNotIn("FloodWait başka hesapla aşılmaz", html)
        self.assertNotIn("Hata veya FloodWait içinde hesap değiştirilmez", html)

    def test_release_notes_are_hidden_on_first_install_and_shown_once_after_upgrade(self):
        settings: dict[str, str] = {}

        def get_setting(key: str) -> str | None:
            return settings.get(key)

        def set_setting(key: str, value: str) -> None:
            settings[key] = value

        with (
            patch("app.release_history.get_app_setting", side_effect=get_setting),
            patch("app.release_history.set_app_setting", side_effect=set_setting),
        ):
            initialize_release_tracking("0.3.0")
            self.assertIsNone(release_notes_overview("0.3.0")["pending_version"])

            initialize_release_tracking("0.4.0")
            self.assertEqual(release_notes_overview("0.4.0")["pending_version"], "0.4.0")

            acknowledge_release_notes("0.4.0", "0.4.0")
            self.assertIsNone(release_notes_overview("0.4.0")["pending_version"])
            initialize_release_tracking("0.4.0")
            self.assertIsNone(release_notes_overview("0.4.0")["pending_version"])

    def test_safe_invite_defaults_are_consistent(self):
        job = JobCreateRequest(
            name="Default test",
            session_id=1,
            source_ref="@source",
            target_ref="@target",
        )
        transfer = ActivityTransferRequest(target_ref="@target")
        self.assertEqual((job.min_delay_seconds, job.max_delay_seconds), (20, 40))
        self.assertEqual((transfer.min_delay_seconds, transfer.max_delay_seconds), (20, 40))

    def test_fresh_database_has_safe_job_and_heartbeat_defaults(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            database_path = Path(temp) / "customer.db"
            settings = SimpleNamespace(database_path=database_path)
            with patch("app.database.get_settings", return_value=settings):
                initialize_database()

            with sqlite3.connect(database_path) as connection:
                columns = {
                    row[1]: row[4]
                    for row in connection.execute("PRAGMA table_info(transfer_jobs)").fetchall()
                }
                app_settings = dict(
                    connection.execute("SELECT key, value FROM app_settings").fetchall()
                )

            self.assertEqual(columns["min_delay_seconds"], "20")
            self.assertEqual(columns["max_delay_seconds"], "40")
            self.assertEqual(app_settings["heartbeat_enabled"], "false")
            self.assertEqual(app_settings["heartbeat_interval_minutes"], "60")
            self.assertEqual(app_settings["heartbeat_group_id"], "")
            self.assertEqual(app_settings["heartbeat_message_template"], "Merhabaa")

    def test_release_verifier_accepts_clean_customer_package(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Pawgram"
            self._write_release_fixture(root)
            archive = Path(temp) / "Pawgram.zip"
            self._zip_release(root, archive)

            result = verify_release_folder(root, "0.4.0", [])
            verify_release_zip(archive, "Pawgram")
            self.assertEqual(result["version"], "0.4.0")

    def test_release_verifier_rejects_runtime_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Pawgram"
            self._write_release_fixture(root)
            (root / "_internal" / "customer.db").write_bytes(b"database")
            with self.assertRaisesRegex(ValueError, "geçici/geliştirme"):
                verify_release_folder(root, "0.4.0", [])

    @staticmethod
    def _write_release_fixture(root: Path) -> None:
        internal = root / "_internal"
        static = internal / "static"
        static.mkdir(parents=True)
        (root / ".env").write_text(
            """APP_ENV=production
CUSTOMER_RELEASE=true
TELEGRAM_API_ID=12345
TELEGRAM_API_HASH=hash
DEFAULT_PROXY_TYPE=socks5
DEFAULT_PROXY_HOST=proxy.example
DEFAULT_PROXY_PORT=1080
""",
            encoding="utf-8",
        )
        executable = bytearray(222)
        executable[:2] = b"MZ"
        struct.pack_into("<I", executable, 0x3C, 128)
        executable[128:132] = b"PE\0\0"
        struct.pack_into("<H", executable, 128 + 4 + 20 + 68, 2)
        (root / "Pawgram.exe").write_bytes(executable)
        (internal / "VERSION").write_text("0.4.0\n", encoding="utf-8")
        (internal / "RELEASE_NOTES.json").write_text("[]\n", encoding="utf-8")
        (internal / "base_library.zip").write_bytes(b"runtime archive")
        (static / "index.html").write_text("<!doctype html>\n", encoding="utf-8")

    @staticmethod
    def _zip_release(root: Path, archive: Path) -> None:
        with zipfile.ZipFile(archive, "w") as handle:
            for path in root.rglob("*"):
                handle.write(path, Path(root.name) / path.relative_to(root))


if __name__ == "__main__":
    unittest.main()
