import importlib
import os
from pathlib import Path
import tempfile
import unittest

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

        import license_server.config as config
        config.get_license_server_settings.cache_clear()
        import license_server.signing as signing
        importlib.reload(signing)
        signing.generate_signing_keys(root / "signing_key.pem", root / "public_key.pem")
        import license_server.database as database
        importlib.reload(database)
        import license_server.main as main
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

    def test_admin_login_uses_httponly_session_cookie(self):
        login = self.client.post(
            "/v1/admin/login",
            json={"admin_key": os.environ["LICENSE_ADMIN_API_KEY"]},
        )
        self.assertEqual(login.status_code, 200)
        self.assertIn("HttpOnly", login.headers["set-cookie"])
        authorized = self.client.get("/v1/admin/licenses")
        self.assertEqual(authorized.status_code, 200)


if __name__ == "__main__":
    unittest.main()
