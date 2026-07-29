import base64
import hashlib
import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.updater import (
    _safe_extract,
    _updater_script,
    is_newer_version,
    mark_update_healthy,
    verify_manifest,
)


class UpdaterTests(unittest.TestCase):
    def test_semantic_version_comparison(self):
        self.assertTrue(is_newer_version("0.3.0", "0.2.9"))
        self.assertTrue(is_newer_version("v1.0.1", "1.0.0"))
        self.assertFalse(is_newer_version("0.3.0", "0.3.0"))
        self.assertFalse(is_newer_version("0.2.9", "0.3.0"))

    def test_signed_manifest_is_verified(self):
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        payload = {
            "product": "pawgram",
            "channel": "stable",
            "version": "0.3.0",
            "asset_url": "https://github.com/pawaard/Pawgram/releases/download/v0.3.0/Pawgram.zip",
            "sha256": hashlib.sha256(b"archive").hexdigest(),
            "archive_root": "Pawgram",
        }
        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        signature = base64.urlsafe_b64encode(private_key.sign(canonical)).decode("ascii").rstrip("=")
        with patch("app.updater.UPDATE_PUBLIC_KEY_PEM", public_key):
            self.assertEqual(verify_manifest({"payload": payload, "signature": signature}), payload)
            altered = {**payload, "version": "9.9.9"}
            with self.assertRaisesRegex(ValueError, "imzası geçersiz"):
                verify_manifest({"payload": altered, "signature": signature})

    def test_update_archive_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("../outside.txt", "unsafe")
            destination = root / "extract"
            destination.mkdir()
            with self.assertRaisesRegex(ValueError, "güvenli olmayan"):
                _safe_extract(archive, destination, "Pawgram")

    def test_update_archive_rejects_unsafe_manifest_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "package.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("Pawgram/Pawgram.exe", "exe")
                handle.writestr("Pawgram/_internal/runtime.txt", "runtime")
            destination = root / "extract"
            destination.mkdir()
            with self.assertRaisesRegex(ValueError, "arşiv kökü güvenli değil"):
                _safe_extract(archive, destination, "../outside")

    def test_update_archive_rejects_symbolic_links(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "links.zip"
            link = zipfile.ZipInfo("Pawgram/link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr(link, "../outside")
            destination = root / "extract"
            destination.mkdir()
            with self.assertRaisesRegex(ValueError, "sembolik bağlantı"):
                _safe_extract(archive, destination, "Pawgram")

    def test_update_archive_rejects_suspicious_compression_ratio(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "bomb.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
                handle.writestr("Pawgram/large.bin", b"0" * (11 * 1024 * 1024))
            destination = root / "extract"
            destination.mkdir()
            with self.assertRaisesRegex(ValueError, "sıkıştırma oranı"):
                _safe_extract(archive, destination, "Pawgram")

    def test_running_application_writes_update_health_marker(self):
        update_root = Path(tempfile.mkdtemp(prefix="PawgramUpdate-"))
        marker = update_root / "startup-health.json"
        try:
            with patch.dict(os.environ, {"PAWGRAM_UPDATE_HEALTH_FILE": str(marker)}):
                mark_update_healthy()
            payload = json.loads(marker.read_text(encoding="utf-8"))
            self.assertTrue(payload["ok"])
            self.assertGreater(payload["pid"], 0)
        finally:
            import shutil

            shutil.rmtree(update_root, ignore_errors=True)

    def test_installer_waits_for_health_marker_before_deleting_backup(self):
        script = _updater_script()
        health_check = script.index("Test-Path -LiteralPath $healthFile")
        backup_delete = script.index("Remove-Item -LiteralPath $backup -Recurse -Force")
        self.assertLess(health_check, backup_delete)
        self.assertIn("$newProcess.HasExited", script)
        self.assertIn("45 saniye içinde başlangıç doğrulaması vermedi", script)

    def test_installer_only_replaces_runtime_and_preserves_customer_storage(self):
        script = _updater_script()
        self.assertIn('$targets = @("Pawgram.exe", "_internal")', script)
        self.assertNotIn('$targets = @("Pawgram.exe", "_internal", ".env")', script)
        self.assertNotIn('$targets = @("Pawgram.exe", "_internal", "data")', script)
        self.assertIn("data klasörü korundu", script)

    def test_installer_waits_and_retries_locked_runtime_files(self):
        script = _updater_script()
        self.assertIn("function Wait-ForProcessExit", script)
        self.assertIn("function Invoke-FileOperationWithRetry", script)
        self.assertIn("Eski Pawgram işlemi kapandı", script)
        self.assertIn("Move-Item -LiteralPath $current -Destination $saved -ErrorAction Stop", script)
        self.assertNotIn("Wait-Process -Id $RunningProcessId", script)

    def test_installer_only_rolls_back_targets_that_were_moved(self):
        script = _updater_script()
        self.assertIn("$movedTargets.Add($name)", script)
        self.assertIn("foreach ($name in $movedTargets)", script)
