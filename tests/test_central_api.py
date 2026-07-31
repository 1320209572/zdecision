from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from zdecision.central.auth import DemoIdentityProvider
from zdecision.central.service import CaptureRequestService
from zdecision.central.store import CentralStore
from zdecision.sync.contracts import RepositoryView

try:
    from fastapi.testclient import TestClient
    from zdecision.central.api import create_app
except (ImportError, ModuleNotFoundError) as error:
    API_IMPORT_ERROR: ImportError | ModuleNotFoundError | None = error
else:
    API_IMPORT_ERROR = None


REPOSITORY_ID = "repo_" + "1" * 32
PRODUCT_ID = "prod_4d7b16e1616dd4cd1aeb2411836fd687"
DEVICE_TOKEN = "demo-device-token"
NOW = datetime(2026, 7, 31, 2, 0, tzinfo=UTC)
EMPTY_BATCH_DIGEST = (
    "e813d564bccbeefe1db875d1c9abb55d63c52b639acc61134a5f1d19cc489b67"
)


class CentralApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNone(
            API_IMPORT_ERROR,
            f"Central API dependencies are missing: {API_IMPORT_ERROR}",
        )
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "central.sqlite3"
        self.store = CentralStore.open(self.database_path)
        self.store.put_repository_mapping(
            "org_demo",
            RepositoryView(
                repository_id=REPOSITORY_ID,
                product_id=PRODUCT_ID,
                product_name="ZDecision",
                enabled=True,
            ),
        )
        service = CaptureRequestService(self.store)
        identity = DemoIdentityProvider(
            organization_id="org_demo",
            user_id="user_demo",
            device_id="device_demo",
            device_token_sha256=hashlib.sha256(
                DEVICE_TOKEN.encode("utf-8")
            ).hexdigest(),
        )
        self.client = TestClient(
            create_app(service, identity, clock=lambda: NOW)
        )

    def tearDown(self) -> None:
        if hasattr(self, "client"):
            self.client.close()
        if hasattr(self, "store"):
            self.store.close()
        if hasattr(self, "temporary_directory"):
            self.temporary_directory.cleanup()

    @property
    def authorization(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {DEVICE_TOKEN}"}

    def create_request(self) -> str:
        response = self.client.post(
            "/api/v1/capture-requests",
            json={
                "repository_id": REPOSITORY_ID,
                "template_id": "business",
                "client_action_id": "web_action_001",
            },
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()["request_id"]

    def test_browser_routes_derive_identity_and_return_registered_repositories(
        self,
    ) -> None:
        repositories = self.client.get("/api/v1/repositories")

        self.assertEqual(200, repositories.status_code)
        self.assertEqual(
            [REPOSITORY_ID],
            [
                item["repository_id"]
                for item in repositories.json()["repositories"]
            ],
        )
        request_id = self.create_request()
        request = self.client.get(f"/api/v1/capture-requests/{request_id}")
        self.assertEqual("queued", request.json()["state"])

    def test_create_rejects_unknown_identity_or_source_fields(self) -> None:
        response = self.client.post(
            "/api/v1/capture-requests",
            json={
                "repository_id": REPOSITORY_ID,
                "template_id": "business",
                "client_action_id": "web_action_001",
                "organization_id": "forbidden",
                "session_id": "forbidden",
            },
        )

        self.assertEqual(422, response.status_code)
        self.assertNotIn("org_demo", response.text)

    def test_refresh_reconnects_from_event_cursor(self) -> None:
        request_id = self.create_request()
        claimed = self.client.post(
            "/api/v1/agent/capture-requests/claim",
            headers=self.authorization,
            json={},
        )
        self.assertEqual(200, claimed.status_code, claimed.text)

        response = self.client.get(
            f"/api/v1/capture-requests/{request_id}/events",
            params={"after_sequence": 1},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            [2],
            [item["sequence"] for item in response.json()["events"]],
        )

    def test_device_endpoint_requires_configured_bearer_token(self) -> None:
        missing = self.client.post(
            "/api/v1/agent/capture-requests/claim", json={}
        )
        wrong = self.client.post(
            "/api/v1/agent/capture-requests/claim",
            headers={"Authorization": "Bearer wrong-token"},
            json={},
        )

        self.assertEqual(401, missing.status_code)
        self.assertEqual({"error": "device_authentication_failed"}, missing.json())
        self.assertEqual(401, wrong.status_code)
        self.assertNotIn(DEVICE_TOKEN, wrong.text)

    def test_device_routes_expose_only_bounded_lifecycle_values(self) -> None:
        request_id = self.create_request()
        claimed_response = self.client.post(
            "/api/v1/agent/capture-requests/claim",
            headers=self.authorization,
            json={},
        )
        claimed = claimed_response.json()
        lease_token = claimed["lease_token"]
        start = self.client.post(
            f"/api/v1/agent/capture-requests/{request_id}/start",
            headers=self.authorization,
            json={"lease_token": lease_token},
        )
        progress = self.client.post(
            f"/api/v1/agent/capture-requests/{request_id}/progress",
            headers=self.authorization,
            json={
                "lease_token": lease_token,
                "code": "extracting_candidates",
            },
        )
        complete = self.client.post(
            f"/api/v1/agent/capture-requests/{request_id}/complete",
            headers=self.authorization,
            json={
                "lease_token": lease_token,
                "batch_digest": EMPTY_BATCH_DIGEST,
            },
        )

        self.assertEqual("running", start.json()["state"])
        self.assertEqual("extracting_candidates", progress.json()["code"])
        self.assertEqual("succeeded_no_candidates", complete.json()["state"])
        for response in (start, progress, complete):
            self.assertNotIn("session_id", response.text)
            self.assertNotIn("turn_id", response.text)
            self.assertNotIn(lease_token, response.text)


if __name__ == "__main__":
    unittest.main()
