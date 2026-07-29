import importlib
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from fastapi.testclient import TestClient


class LicenseServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        os.environ["LICENSE_ADMIN_API_KEY"] = "test-admin-key-with-enough-entropy"
        os.environ["LICENSE_DATABASE_PATH"] = str(root / "licenses.db")
        os.environ["LICENSE_SIGNING_KEY_PATH"] = str(root / "signing_key.pem")
        os.environ["LICENSE_PUBLIC_KEY_PATH"] = str(root / "public_key.pem")
        os.environ["LICENSE_LEASE_HOURS"] = "24"

        from license_server import config
        config.get_license_server_settings.cache_clear()
        from license_server import signing
        importlib.reload(signing)
        signing.generate_signing_keys(root / "signing_key.pem", root / "public_key.pem")
        from license_server import database
        importlib.reload(database)
        from license_server import main
        self.main = importlib.reload(main)
        self.client_context = TestClient(self.main.app)
        self.client = self.client_context.__enter__()
        self.headers = {"X-Admin-Key": os.environ["LICENSE_ADMIN_API_KEY"]}

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()
        for key in (
            "LICENSE_ADMIN_API_KEY", "LICENSE_DATABASE_PATH", "LICENSE_SIGNING_KEY_PATH",
            "LICENSE_PUBLIC_KEY_PATH", "LICENSE_LEASE_HOURS",
        ):
            os.environ.pop(key, None)

    def test_create_activate_validate_and_revoke(self):
        created = self.client.post(
            "/v1/admin/licenses",
            headers=self.headers,
            json={"customer_label": "Test customer", "duration_days": 7, "max_devices": 1},
        )
        self.assertEqual(created.status_code, 200)
        license_data = created.json()
        self.assertIsNone(license_data["expires_at"])

        activation = self.client.post(
            "/v1/activate",
            json={
                "license_key": license_data["license_key"],
                "device_id": "d" * 64,
                "installation_id": "installation-test-1234",
                "app_version": "0.1.0",
            },
        )
        self.assertEqual(activation.status_code, 200)
        lease = activation.json()["lease_token"]
        self.assertTrue(activation.json()["license_expires_at"])

        validation = self.client.post(
            "/v1/validate",
            json={"lease_token": lease, "device_id": "d" * 64, "app_version": "0.1.0"},
        )
        self.assertEqual(validation.status_code, 200)

        other_device = self.client.post(
            "/v1/activate",
            json={
                "license_key": license_data["license_key"],
                "device_id": "e" * 64,
                "installation_id": "installation-test-5678",
                "app_version": "0.1.0",
            },
        )
        self.assertEqual(other_device.status_code, 409)

        reset = self.client.post(
            f"/v1/admin/licenses/{license_data['id']}/reset-devices", headers=self.headers
        )
        self.assertEqual(reset.status_code, 200)
        old_lease_denied = self.client.post(
            "/v1/validate",
            json={"lease_token": lease, "device_id": "d" * 64, "app_version": "0.1.0"},
        )
        self.assertEqual(old_lease_denied.status_code, 403)
        reactivation = self.client.post(
            "/v1/activate",
            json={
                "license_key": license_data["license_key"],
                "device_id": "d" * 64,
                "installation_id": "installation-test-9999",
                "app_version": "0.1.0",
            },
        )
        self.assertEqual(reactivation.status_code, 200)
        lease = reactivation.json()["lease_token"]

        revoked = self.client.post(
            f"/v1/admin/licenses/{license_data['id']}/revoke", headers=self.headers
        )
        self.assertEqual(revoked.status_code, 200)
        denied = self.client.post(
            "/v1/validate",
            json={"lease_token": lease, "device_id": "d" * 64, "app_version": "0.1.0"},
        )
        self.assertEqual(denied.status_code, 403)

    def test_admin_api_rejects_wrong_key(self):
        response = self.client.get("/v1/admin/licenses", headers={"X-Admin-Key": "wrong"})
        self.assertEqual(response.status_code, 401)

    def test_one_day_and_unlimited_licenses(self):
        one_day = self.client.post(
            "/v1/admin/licenses",
            headers=self.headers,
            json={"customer_label": "One day", "duration_days": 1, "max_devices": 1},
        )
        self.assertEqual(one_day.status_code, 200)

        unlimited = self.client.post(
            "/v1/admin/licenses",
            headers=self.headers,
            json={"customer_label": "Unlimited", "duration_days": -1, "max_devices": 1},
        )
        self.assertEqual(unlimited.status_code, 200)
        license_data = unlimited.json()
        activation = self.client.post(
            "/v1/activate",
            json={
                "license_key": license_data["license_key"],
                "device_id": "u" * 64,
                "installation_id": "installation-unlimited-1234",
                "app_version": "0.4.4",
            },
        )
        self.assertEqual(activation.status_code, 200)
        self.assertIsNone(activation.json()["license_expires_at"])
        claims = self.main.verify_token(activation.json()["lease_token"])
        self.assertTrue(claims["unlimited"])

        validation = self.client.post(
            "/v1/validate",
            json={
                "lease_token": activation.json()["lease_token"],
                "device_id": "u" * 64,
                "app_version": "0.4.4",
            },
        )
        self.assertEqual(validation.status_code, 200)
        self.assertIsNone(validation.json()["license_expires_at"])

        extension = self.client.post(
            f"/v1/admin/licenses/{license_data['id']}/extend",
            headers=self.headers,
            json={"duration_days": 30},
        )
        self.assertEqual(extension.status_code, 409)

        invalid_zero = self.client.post(
            "/v1/admin/licenses",
            headers=self.headers,
            json={"customer_label": "Invalid", "duration_days": 0, "max_devices": 1},
        )
        self.assertEqual(invalid_zero.status_code, 422)

    def test_concurrent_activation_cannot_exceed_device_limit(self):
        created = self.client.post(
            "/v1/admin/licenses",
            headers=self.headers,
            json={"customer_label": "Concurrent", "duration_days": 7, "max_devices": 1},
        ).json()
        barrier = Barrier(2)

        def activate_device(character: str):
            barrier.wait(timeout=5)
            return self.client.post(
                "/v1/activate",
                json={
                    "license_key": created["license_key"],
                    "device_id": character * 64,
                    "installation_id": f"installation-{character * 16}",
                    "app_version": "0.3.0",
                },
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(activate_device, ("a", "b")))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])

    def test_admin_login_uses_httponly_session_cookie(self):
        login = self.client.post(
            "/v1/admin/login",
            json={"admin_key": os.environ["LICENSE_ADMIN_API_KEY"]},
        )
        self.assertEqual(login.status_code, 200)
        self.assertIn("HttpOnly", login.headers["set-cookie"])
        authorized = self.client.get("/v1/admin/licenses")
        self.assertEqual(authorized.status_code, 200)

    def test_admin_login_is_rate_limited(self):
        for _ in range(5):
            response = self.client.post(
                "/v1/admin/login",
                json={"admin_key": "wrong-admin-key-value"},
            )
            self.assertEqual(response.status_code, 401)
        limited = self.client.post(
            "/v1/admin/login",
            json={"admin_key": "wrong-admin-key-value"},
        )
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers["retry-after"], "60")

    def test_activation_is_rate_limited_without_unbounded_attempts(self):
        payload = {
            "license_key": "PAWG-XXXXX-XXXXX-XXXXX-XXXXX",
            "device_id": "d" * 64,
            "installation_id": "installation-test-1234",
            "app_version": "0.3.0",
        }
        for _ in range(10):
            response = self.client.post("/v1/activate", json=payload)
            self.assertEqual(response.status_code, 403)
        limited = self.client.post("/v1/activate", json=payload)
        self.assertEqual(limited.status_code, 429)


if __name__ == "__main__":
    unittest.main()
