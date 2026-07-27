import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.updater import _safe_extract, is_newer_version, verify_manifest


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
