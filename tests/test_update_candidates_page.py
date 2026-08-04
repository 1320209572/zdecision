from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from zdecision.central.auth import DemoIdentityProvider
from zdecision.central.service import CaptureRequestService
from zdecision.central.store import CentralStore
from zdecision.sync.contracts import RepositoryView

try:
    from fastapi.testclient import TestClient
    from zdecision.central.api import create_app
except (ImportError, ModuleNotFoundError) as error:
    PAGE_IMPORT_ERROR: ImportError | ModuleNotFoundError | None = error
else:
    PAGE_IMPORT_ERROR = None


class UpdateCandidatesPageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNone(
            PAGE_IMPORT_ERROR,
            f"Update Candidates page dependencies are missing: {PAGE_IMPORT_ERROR}",
        )
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = CentralStore.open(
            Path(self.temporary_directory.name) / "central.sqlite3"
        )
        self.store.put_repository_mapping(
            "org_demo",
            RepositoryView(
                repository_id="repo_" + "1" * 32,
                product_id="prod_4d7b16e1616dd4cd1aeb2411836fd687",
                product_name="ZDecision",
                enabled=True,
            ),
        )
        identity = DemoIdentityProvider(
            organization_id="org_demo",
            user_id="user_demo",
            device_id="device_demo",
            device_token_sha256=hashlib.sha256(b"device-token").hexdigest(),
        )
        self.client = TestClient(
            create_app(CaptureRequestService(self.store), identity)
        )

    def tearDown(self) -> None:
        if hasattr(self, "client"):
            self.client.close()
        if hasattr(self, "store"):
            self.store.close()
        if hasattr(self, "temporary_directory"):
            self.temporary_directory.cleanup()

    @staticmethod
    def capture_body() -> dict[str, object]:
        return {
            "repository_id": "repo_" + "1" * 32,
            "template_id": "business",
            "capture_scope": "all_valid_sessions",
            "client_action_id": "web_action_page-test",
        }

    def test_spa_build_and_capture_api_keep_the_explicit_boundary(self) -> None:
        html = self.client.get("/").text

        self.assertIn('<div id="root"></div>', html)
        self.assertNotIn("session_id", html.lower())
        response = self.client.post(
            "/api/v1/capture-requests", json=self.capture_body()
        )
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(
            "all_valid_sessions", response.json()["capture_scope"]
        )


if __name__ == "__main__":
    unittest.main()
