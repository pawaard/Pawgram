import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.config import Settings
from app.licensing import (
    LICENSE_RUNTIME_BLOCK_MESSAGE,
    LICENSE_RUNTIME_BLOCK_STATUS,
    local_license_status,
    refresh_license,
)


class CommercialLicensingTests(unittest.TestCase):
    def test_commercial_settings_cannot_disable_or_redirect_licensing(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("app.config.COMMERCIAL_EDITION", True),
        ):
            settings = Settings(
                _env_file=None,
                license_required=False,
                license_server_url="http://127.0.0.1:9999",
            )
            self.assertTrue(settings.licensing_enforced)
            self.assertTrue(settings.licensing_online_required)
            self.assertEqual(
                settings.effective_license_server_url,
                "https://license.rewmarket.com",
            )
            self.assertEqual(settings.license_refresh_interval_seconds, 60)

    def test_persistent_runtime_block_wins_even_after_lease_is_cleared(self):
        values = {
            LICENSE_RUNTIME_BLOCK_STATUS: "revoked",
            LICENSE_RUNTIME_BLOCK_MESSAGE: "Bu lisans iptal edildi.",
            "license_lease_token": "",
        }
        settings = SimpleNamespace(
            licensing_enforced=True,
            licensing_online_required=True,
        )
        with (
            patch("app.licensing.get_settings", return_value=settings),
            patch("app.licensing.get_app_setting", side_effect=values.get),
        ):
            status = local_license_status()
        self.assertFalse(status["valid"])
        self.assertEqual(status["status"], "revoked")
        self.assertEqual(status["message"], "Bu lisans iptal edildi.")

    def test_commercial_network_failure_blocks_instead_of_using_offline_lease(self):
        settings = SimpleNamespace(
            licensing_online_required=True,
            license_request_timeout=2.0,
            effective_license_server_url="https://license.rewmarket.com",
        )
        request = httpx.Request("POST", "https://license.rewmarket.com/v1/validate")
        with (
            patch(
                "app.licensing.local_license_status",
                return_value={"required": True, "valid": True, "status": "active"},
            ),
            patch("app.licensing.get_app_setting", return_value="signed-lease"),
            patch("app.licensing.get_settings", return_value=settings),
            patch(
                "app.licensing.httpx.AsyncClient",
                side_effect=httpx.ConnectError("offline", request=request),
            ),
            patch("app.licensing._set_runtime_block") as set_runtime_block,
        ):
            status = asyncio.run(refresh_license())
        self.assertFalse(status["valid"])
        self.assertFalse(status["offline"])
        self.assertEqual(status["status"], "server_unreachable")
        set_runtime_block.assert_called_once()

    def test_structured_server_error_is_stored_as_safe_text(self):
        settings = SimpleNamespace(
            licensing_online_required=True,
            license_request_timeout=2.0,
            effective_license_server_url="https://license.rewmarket.com",
        )
        response = SimpleNamespace(
            status_code=422,
            json=lambda: {"detail": [{"loc": ["body", "lease_token"], "msg": "invalid"}]},
        )
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = response
        with (
            patch(
                "app.licensing.local_license_status",
                return_value={"required": True, "valid": True, "status": "active"},
            ),
            patch("app.licensing.get_app_setting", return_value="malformed-lease"),
            patch("app.licensing.get_settings", return_value=settings),
            patch("app.licensing.httpx.AsyncClient", return_value=client),
            patch("app.licensing.set_app_setting") as set_app_setting,
        ):
            status = asyncio.run(refresh_license())
        self.assertFalse(status["valid"])
        self.assertEqual(status["status"], "revoked")
        self.assertEqual(status["message"], "Lisans sunucu tarafından reddedildi.")
        set_app_setting.assert_any_call(
            LICENSE_RUNTIME_BLOCK_MESSAGE,
            "Lisans sunucu tarafından reddedildi.",
        )


if __name__ == "__main__":
    unittest.main()
