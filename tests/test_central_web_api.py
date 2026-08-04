from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from zdecision.central.api import create_app
from zdecision.central.auth import DemoIdentityProvider
from zdecision.central.cli import CentralCliError, _registry_repository_root
from zdecision.central.service import CaptureRequestService
from zdecision.central.store import CentralStore
from zdecision.central.web.application import CentralWebApplication
from zdecision.central.web.queries import CentralWebQueries
from zdecision.central.web.store import CentralWebStore
from zdecision.ids import product_id
from zdecision.registry.models import ProductMetadata, ProductRegistry
from zdecision.registry.query import RegistrySnapshot
from zdecision.sync.contracts import RepositoryView


PRODUCT_NAME = "ZDecision"
PRODUCT_ID = product_id(PRODUCT_NAME)
REPOSITORY_ID = "repo_" + "1" * 32
COMMIT_SHA = "b" * 40


class _RegistryQuery:
    def snapshot(self) -> RegistrySnapshot:
        return RegistrySnapshot(
            COMMIT_SHA,
            {PRODUCT_ID: ProductMetadata(PRODUCT_ID, PRODUCT_NAME)},
            {PRODUCT_ID: ProductRegistry(PRODUCT_ID, {})},
            {},
        )


class CentralWebApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.store = CentralStore.open(root / "central.sqlite3")
        self.addCleanup(self.store.close)
        self.store.put_repository_mapping(
            "org_demo",
            RepositoryView(
                REPOSITORY_ID, PRODUCT_ID, PRODUCT_NAME, True
            ),
        )
        static_root = root / "static"
        (static_root / "assets").mkdir(parents=True)
        (static_root / "index.html").write_text(
            "<!doctype html><title>central shell</title>", "utf-8"
        )
        (static_root / "assets" / "shell.css").write_text(
            "body{}", "utf-8"
        )
        identity = DemoIdentityProvider(
            organization_id="org_demo",
            user_id="user_demo",
            device_id="device_demo",
            device_token_sha256=hashlib.sha256(b"token").hexdigest(),
        )
        web = CentralWebApplication(
            store=CentralWebStore(self.store.connection),
            queries=CentralWebQueries(
                self.store.connection, _RegistryQuery()
            ),
        )
        self.client = TestClient(
            create_app(
                CaptureRequestService(self.store),
                identity,
                web_application=web,
                static_root=static_root,
            )
        )
        self.addCleanup(self.client.close)

    def test_dashboard_is_serialized_by_the_web_transport(self) -> None:
        response = self.client.get("/api/v1/web/dashboard")

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(
            {
                "metrics": {
                    "product_count": 1,
                    "pending_candidate_count": 0,
                    "active_decision_count": 0,
                    "completed_this_week": 0,
                },
                "registry": {
                    "state": "available",
                    "commit_sha": COMMIT_SHA,
                },
                "products": [
                    {
                        "product_id": PRODUCT_ID,
                        "product_name": PRODUCT_NAME,
                        "repository_ids": [REPOSITORY_ID],
                        "pending_candidate_count": 0,
                        "active_decision_count": 0,
                        "last_activity_at": None,
                    }
                ],
                "recent_publications": [],
            },
            response.json(),
        )

    def test_spa_fallback_serves_browser_routes_but_never_api_misses(
        self,
    ) -> None:
        browser = self.client.get(f"/products/{PRODUCT_ID}/reviews")
        api = self.client.get("/api/v1/web/not-a-route")
        asset = self.client.get("/assets/shell.css")

        self.assertEqual(200, browser.status_code)
        self.assertIn("central shell", browser.text)
        self.assertEqual(404, api.status_code)
        self.assertEqual({"detail": "Not Found"}, api.json())
        self.assertEqual("body{}", asset.text)


class CentralWebCliCompositionTest(unittest.TestCase):
    def test_registry_root_must_be_an_absolute_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(
                CentralCliError, "registry_repository_root_not_absolute"
            ):
                _registry_repository_root("relative")
            with self.assertRaisesRegex(
                CentralCliError, "registry_repository_root_invalid"
            ):
                _registry_repository_root(str(root / "missing"))
            with self.assertRaisesRegex(
                CentralCliError, "registry_repository_root_not_git"
            ):
                _registry_repository_root(str(root))
            subprocess.run(
                ("git", "init", "-b", "main", str(root)),
                check=True,
                capture_output=True,
            )
            self.assertEqual(root.resolve(), _registry_repository_root(str(root)))


if __name__ == "__main__":
    unittest.main()
