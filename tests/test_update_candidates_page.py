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

    def test_page_contains_one_action_and_cursor_reconnect(self) -> None:
        response = self.client.get("/")
        html = response.text
        lowered = html.lower()

        self.assertEqual(200, response.status_code)
        self.assertIn("更新候选决策", html)
        self.assertIn("等待本地设备", html)
        self.assertIn("after_sequence", html)
        self.assertIn("localStorage", html)
        self.assertIn("textContent", html)
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("session_id", lowered)
        self.assertNotIn("prompt", lowered)
        self.assertNotIn("review", lowered)
        self.assertNotIn("publish", lowered)

    def test_page_posts_only_the_four_capture_request_fields(self) -> None:
        html = self.client.get("/").text

        self.assertRegex(
            html,
            r"JSON\.stringify\(\{\s*repository_id,\s*"
            r"template_id:\s*['\"]business['\"],\s*"
            r"capture_scope:\s*['\"]all_valid_sessions['\"],\s*"
            r"client_action_id",
        )

    def test_terminal_request_refreshes_safe_candidate_fields(
        self,
    ) -> None:
        html = self.client.get("/").text

        self.assertIn("loadCandidates", html)
        self.assertIn(
            "/api/v1/repositories/${repositoryId}/candidates",
            html,
        )
        for field in (
            "claim",
            "future_action",
            "scope_summary",
            "invalidation_conditions",
        ):
            self.assertIn(field, html)
        self.assertIn("candidateList.replaceChildren()", html)
        self.assertIn("textContent", html)
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("acceptCandidate", html)
        self.assertNotIn("rejectCandidate", html)
        self.assertNotIn("publishCandidate", html)


if __name__ == "__main__":
    unittest.main()
