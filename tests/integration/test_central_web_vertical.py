from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from zdecision.capture.models import CandidateContent
from zdecision.central.api import create_app
from zdecision.central.auth import DemoIdentityProvider
from zdecision.central.decision_spaces import (
    EnabledRepository,
    LeafDecisionSpace,
    RepositoryDecisionRoute,
)
from zdecision.central.service import CaptureRequestService
from zdecision.central.store import CentralStore
from zdecision.central.web.application import CentralWebApplication
from zdecision.central.web.queries import CentralWebQueries
from zdecision.central.web.store import CentralWebStore
from zdecision.ids import (
    candidate_revision_id,
    decision_space_id,
    product_id,
    repository_route_id,
)
from zdecision.jsonio import canonical_json_bytes
from zdecision.registry.catalog import RegistryCatalog
from zdecision.registry.git import GitRegistryAdapter
from zdecision.registry.models import RootRegistry
from zdecision.registry.query import RegistryQuery
from zdecision.sync.contracts import (
    CandidateBatchUpload,
    CandidateRevisionUpload,
    RepositoryView,
)


PRODUCT_NAME = "ZDecision Vertical"
PRODUCT_ID = product_id(PRODUCT_NAME)
REPOSITORY_ID = "repo_" + "8" * 32
DEVICE_TOKEN = "vertical-device-token"
NOW = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)
FAMILY_ID = "cfm_" + "a" * 32
REJECTED_FAMILY_ID = "cfm_" + "b" * 32
SKIPPED_FAMILY_ID = "cfm_" + "c" * 32
ACCEPTED_CLAIM = "Only explicitly accepted Candidate content is published."
REJECTED_CLAIM = "REJECTED_CLAIM_MUST_NEVER_REACH_GIT_71583"
SKIPPED_CLAIM = "SKIPPED_CLAIM_MUST_NEVER_REACH_GIT_62049"

FORBIDDEN_FIELDS = {
    "organization_id": "PRIVATE_RAW_PROMPT_SENTINEL_0d964",
    "actor_id": "PRIVATE_SOURCE_CODE_SENTINEL_2ac17",
    "product_name": "PRIVATE_DIFF_SENTINEL_7bb31",
    "registry_path": "PRIVATE_CREDENTIAL_SENTINEL_9e415",
    "commit_message": "PRIVATE_LOCAL_PATH_SENTINEL_7fa26",
    "decision_bytes": "PRIVATE_DECISION_BYTES_SENTINEL_288cd",
    "session_id": "PRIVATE_SESSION_SENTINEL_64f90",
    "prompt": "PRIVATE_PROMPT_SENTINEL_c503b",
}


class InjectedCrash(Exception):
    pass


class CentralWebVerticalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.remote = self.root / "origin.git"
        self.repository = self.root / "repository"
        self.database_path = self.root / "central.sqlite3"
        self.http_fixture_json: list[str] = []
        self.client: TestClient | None = None
        self.store: CentralStore | None = None

        self._git("init", "--bare", str(self.remote), repository=self.root)
        self._git("init", "-b", "main", str(self.repository), repository=self.root)
        self._git("config", "user.email", "vertical@example.com")
        self._git("config", "user.name", "ZDecision Vertical Test")
        registry = self.repository / "decision-registry"
        registry.mkdir()
        (registry / "registry.json").write_bytes(
            canonical_json_bytes(RootRegistry({}).to_dict())
        )
        self._git("add", "decision-registry")
        self._git("commit", "-m", "initial registry")
        self._git("remote", "add", "origin", str(self.remote.resolve()))
        self._git("push", "-u", "origin", "main")

        initial = CentralStore.open(self.database_path)
        initial.put_repository_mapping(
            "org_demo",
            RepositoryView(
                REPOSITORY_ID, PRODUCT_ID, PRODUCT_NAME, True
            ),
        )
        initial.put_repository(
            "org_demo", EnabledRepository(REPOSITORY_ID, True)
        )
        leaf_id = decision_space_id("product", PRODUCT_ID)
        initial.put_decision_space(
            "org_demo",
            LeafDecisionSpace(
                decision_space_id=leaf_id,
                kind="product",
                display_name=PRODUCT_NAME,
                compatibility_product_id=PRODUCT_ID,
                compatibility_product_name=PRODUCT_NAME,
                catalog_group_id=None,
                catalog_breadcrumb=(),
                source_root=".",
                package_name=None,
                asset_type=None,
                enabled=True,
            ),
        )
        initial.replace_trusted_route_heads(
            "org_demo",
            REPOSITORY_ID,
            (
                RepositoryDecisionRoute(
                    route_id=repository_route_id(REPOSITORY_ID, leaf_id),
                    repository_id=REPOSITORY_ID,
                    decision_space_id=leaf_id,
                    path_prefixes=(".",),
                    excluded_prefixes=(),
                    enabled=True,
                    configuration_version=1,
                ),
            ),
        )
        initial.close()
        self.restart_central_service()
        self.addCleanup(self._close_central_service)

    @property
    def active_client(self) -> TestClient:
        assert self.client is not None
        return self.client

    @property
    def active_store(self) -> CentralStore:
        assert self.store is not None
        return self.store

    @property
    def authorization(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {DEVICE_TOKEN}"}

    def _git(
        self,
        *arguments: str,
        repository: Path | None = None,
    ) -> bytes:
        return subprocess.run(
            ("git", "-C", str(repository or self.repository), *arguments),
            check=True,
            capture_output=True,
        ).stdout

    def _record(self, response):
        self.http_fixture_json.append(response.text)
        return response

    def _close_central_service(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.store is not None:
            self.store.close()
            self.store = None

    def restart_central_service(self) -> None:
        self._close_central_service()
        self.store = CentralStore.open(self.database_path)
        identity = DemoIdentityProvider(
            organization_id="org_demo",
            user_id="user_demo",
            device_id="device_demo",
            device_token_sha256=hashlib.sha256(
                DEVICE_TOKEN.encode("utf-8")
            ).hexdigest(),
        )
        git = GitRegistryAdapter(
            self.repository, expected_origin=str(self.remote.resolve())
        )
        web = CentralWebApplication(
            store=CentralWebStore(self.store.connection),
            queries=CentralWebQueries(
                self.store.connection, RegistryQuery(self.repository, git)
            ),
            catalog=RegistryCatalog(self.repository),
            git=git,
        )
        self.client = TestClient(
            create_app(
                CaptureRequestService(self.store),
                identity,
                clock=lambda: NOW,
                web_application=web,
            )
        )

    def candidate(
        self, family_id: str, claim: str
    ) -> CandidateRevisionUpload:
        content = CandidateContent(
            product=PRODUCT_NAME,
            claim=claim,
            future_action="Keep the explicit Review and publication boundary.",
            scope_summary="Central Web vertical acceptance",
            repositories=("disposable-zdecision",),
            paths=("src/zdecision/central/web/",),
            invalidation_conditions=("The Central Web contract changes.",),
        )
        content_digest = hashlib.sha256(
            canonical_json_bytes(content.to_dict())
        ).hexdigest()
        return CandidateRevisionUpload(
            family_id=family_id,
            revision_id=candidate_revision_id(family_id, 1, content_digest),
            revision=1,
            content=content,
            content_digest=content_digest,
            evidence_digest="e" * 64,
        )

    def create_capture_request(self) -> str:
        response = self._record(
            self.active_client.post(
                "/api/v1/capture-requests",
                json={
                    "repository_id": REPOSITORY_ID,
                    "template_id": "business",
                    "capture_scope": "all_valid_sessions",
                    "client_action_id": "web_action_vertical-capture",
                },
            )
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()["request_id"]

    def agent_upload_and_complete(
        self,
        request_id: str,
        candidates: tuple[CandidateRevisionUpload, ...],
    ) -> None:
        claimed = self._record(
            self.active_client.post(
                "/api/v1/agent/capture-requests/claim",
                headers=self.authorization,
                json={},
            )
        )
        self.assertEqual(200, claimed.status_code, claimed.text)
        lease_token = claimed.json()["lease_token"]
        started = self._record(
            self.active_client.post(
                f"/api/v1/agent/capture-requests/{request_id}/start",
                headers=self.authorization,
                json={"lease_token": lease_token},
            )
        )
        self.assertEqual(200, started.status_code, started.text)
        batch_digest = hashlib.sha256(
            canonical_json_bytes(
                {"items": [item.to_dict() for item in candidates]}
            )
        ).hexdigest()
        batch = CandidateBatchUpload(
            request_id=request_id,
            repository_id=REPOSITORY_ID,
            items=candidates,
            batch_digest=batch_digest,
        )
        uploaded = self._record(
            self.active_client.post(
                f"/api/v1/agent/capture-requests/{request_id}/candidates",
                headers=self.authorization,
                json={"lease_token": lease_token, "batch": batch.to_dict()},
            )
        )
        self.assertEqual(200, uploaded.status_code, uploaded.text)
        completed = self._record(
            self.active_client.post(
                f"/api/v1/agent/capture-requests/{request_id}/complete",
                headers=self.authorization,
                json={
                    "lease_token": lease_token,
                    "batch_digest": batch.batch_digest,
                },
            )
        )
        self.assertEqual(200, completed.status_code, completed.text)
        self.assertEqual("succeeded", completed.json()["state"])

    @staticmethod
    def draft_items(draft: dict[str, object]) -> list[dict[str, object]]:
        return [
            {
                "family_id": item["family_id"],
                "repository_id": item["repository_id"],
                "revision_id": item["revision_id"],
                "revision": item["revision"],
                "content_digest": item["content_digest"],
                "action": {
                    FAMILY_ID: "accept",
                    REJECTED_FAMILY_ID: "reject",
                    SKIPPED_FAMILY_ID: "skip",
                }[item["family_id"]],
                "effective_content": None,
                "note": None,
            }
            for item in draft["items"]
        ]

    def save_accept_draft(
        self, draft: dict[str, object]
    ) -> dict[str, object]:
        response = self._record(
            self.active_client.put(
                f"/api/v1/web/products/{PRODUCT_ID}/review-draft",
                json={
                    "expected_version": draft["version"],
                    "items": self.draft_items(draft),
                },
            )
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def submit_review(
        self, saved: dict[str, object]
    ) -> dict[str, object]:
        response = self._record(
            self.active_client.post(
                f"/api/v1/web/products/{PRODUCT_ID}/reviews",
                json={
                    "client_action_id": "web_action_review_vertical",
                    "expected_draft_version": saved["version"],
                    "items": saved["items"],
                },
            )
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def create_preview(self, review_batch_id: str) -> dict[str, object]:
        response = self._record(
            self.active_client.post(
                f"/api/v1/web/reviews/{review_batch_id}/previews",
                json={"client_action_id": "web_action_preview_vertical"},
            )
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def publish(self, preview_id: str) -> dict[str, object]:
        response = self._record(
            self.active_client.post(
                f"/api/v1/web/publication-previews/{preview_id}/publish",
                json={"client_action_id": "web_action_publish_vertical"},
            )
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def assert_forbidden_fields_rejected(
        self, method: str, path: str, valid_body: dict[str, object]
    ) -> None:
        response = self._record(
            self.active_client.request(
                method,
                path,
                json={**valid_body, **FORBIDDEN_FIELDS},
            )
        )
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual({"error": "invalid_request"}, response.json())
        for sentinel in FORBIDDEN_FIELDS.values():
            self.assertNotIn(sentinel, response.text)

    def publication_commit_count(self, preview_id: str) -> int:
        messages = self._git("log", "--format=%B", "--all").decode("utf-8")
        return messages.count(f"ZDecision-Preview: {preview_id}")

    def receipt_count(self, family_id: str) -> int:
        return int(
            self.active_store.connection.execute(
                "SELECT COUNT(*) FROM web_candidate_receipts WHERE family_id = ?",
                (family_id,),
            ).fetchone()[0]
        )

    def remote_main_contains(self, commit_sha: str) -> bool:
        main_ref = "refs/heads/main"
        verified = subprocess.run(
            ("git", "-C", str(self.remote), "rev-parse", "--verify", main_ref),
            capture_output=True,
        )
        if verified.returncode != 0:
            return False
        result = subprocess.run(
            (
                "git",
                "-C",
                str(self.remote),
                "merge-base",
                "--is-ancestor",
                commit_sha,
                main_ref,
            ),
            capture_output=True,
        )
        return result.returncode == 0

    def create_unreferenced_remote_commit(self) -> str:
        environment = {
            **os.environ,
            "GIT_AUTHOR_NAME": "ZDecision Vertical Test",
            "GIT_AUTHOR_EMAIL": "vertical@example.com",
            "GIT_COMMITTER_NAME": "ZDecision Vertical Test",
            "GIT_COMMITTER_EMAIL": "vertical@example.com",
        }
        return subprocess.run(
            (
                "git",
                "-C",
                str(self.remote),
                "commit-tree",
                "refs/heads/main^{tree}",
                "-p",
                "refs/heads/main",
            ),
            check=True,
            input=b"unreferenced remote commit\n",
            capture_output=True,
            env=environment,
        ).stdout.decode("ascii").strip()

    def all_git_blob_bytes(self) -> bytes:
        object_lines = self._git("rev-list", "--objects", "--all").splitlines()
        blobs: list[bytes] = []
        for line in object_lines:
            object_id = line.split(maxsplit=1)[0].decode("ascii")
            if self._git("cat-file", "-t", object_id).strip() == b"blob":
                blobs.append(self._git("cat-file", "blob", object_id))
        return b"\n".join(blobs)

    def test_candidate_to_product_registry_decision_and_history(self) -> None:
        self.assert_forbidden_fields_rejected(
            "POST",
            "/api/v1/capture-requests",
            {
                "repository_id": REPOSITORY_ID,
                "template_id": "business",
                "capture_scope": "all_valid_sessions",
                "client_action_id": "web_action_forbidden-capture",
            },
        )
        request_id = self.create_capture_request()
        self.agent_upload_and_complete(
            request_id,
            (
                self.candidate(FAMILY_ID, ACCEPTED_CLAIM),
                self.candidate(REJECTED_FAMILY_ID, REJECTED_CLAIM),
                self.candidate(SKIPPED_FAMILY_ID, SKIPPED_CLAIM),
            ),
        )

        draft_response = self._record(
            self.active_client.get(
                f"/api/v1/web/products/{PRODUCT_ID}/review-draft"
            )
        )
        self.assertEqual(200, draft_response.status_code, draft_response.text)
        draft = draft_response.json()
        inbox = self._record(
            self.active_client.get(
                f"/api/v1/web/products/{PRODUCT_ID}/candidates"
            )
        )
        self.assertEqual(200, inbox.status_code, inbox.text)
        draft = {**draft, "items": inbox.json()["items"]}
        valid_draft_body = {
            "expected_version": draft["version"],
            "items": self.draft_items(draft),
        }
        self.assert_forbidden_fields_rejected(
            "PUT",
            f"/api/v1/web/products/{PRODUCT_ID}/review-draft",
            valid_draft_body,
        )
        saved = self.save_accept_draft(draft)

        self.restart_central_service()
        restored_response = self._record(
            self.active_client.get(
                f"/api/v1/web/products/{PRODUCT_ID}/review-draft"
            )
        )
        self.assertEqual(200, restored_response.status_code, restored_response.text)
        restored = restored_response.json()
        self.assertEqual(
            "accept",
            next(
                item["action"]
                for item in restored["items"]
                if item["family_id"] == FAMILY_ID
            ),
        )

        review_body = {
            "client_action_id": "web_action_forbidden-review",
            "expected_draft_version": restored["version"],
            "items": restored["items"],
        }
        self.assert_forbidden_fields_rejected(
            "POST",
            f"/api/v1/web/products/{PRODUCT_ID}/reviews",
            review_body,
        )
        review = self.submit_review(saved)
        preview_body = {"client_action_id": "web_action_forbidden-preview"}
        self.assert_forbidden_fields_rejected(
            "POST",
            f"/api/v1/web/reviews/{review['review_batch_id']}/previews",
            preview_body,
        )
        preview = self.create_preview(review["review_batch_id"])

        self.restart_central_service()
        self.assert_forbidden_fields_rejected(
            "POST",
            f"/api/v1/web/publication-previews/{preview['preview_id']}/publish",
            {"client_action_id": "web_action_forbidden-publish"},
        )
        publications = self.active_client.app.state.web_application.publications
        publications.checkpoint = lambda name: (
            (_ for _ in ()).throw(InjectedCrash(name))
            if name == "after_commit"
            else None
        )
        with self.assertRaises(InjectedCrash):
            self.publish(preview["preview_id"])

        self.restart_central_service()
        published = self.publish(preview["preview_id"])
        self.assertEqual("completed", published["state"])
        self.assertEqual(1, self.publication_commit_count(preview["preview_id"]))
        self.assertEqual(1, self.receipt_count(FAMILY_ID))

        self.assert_forbidden_fields_rejected(
            "POST",
            f"/api/v1/web/publications/{published['publication_id']}/resume",
            {"client_action_id": "web_action_forbidden-resume"},
        )
        history = self._record(
            self.active_client.get(
                "/api/v1/web/publications", params={"product_id": PRODUCT_ID}
            )
        )
        self.assertEqual(200, history.status_code, history.text)
        self.assertEqual([published["publication_id"]], [
            item["publication_id"] for item in history.json()["items"]
        ])
        publication_detail = self._record(
            self.active_client.get(
                f"/api/v1/web/publications/{published['publication_id']}"
            )
        )
        self.assertEqual(200, publication_detail.status_code, publication_detail.text)

        decision_id = published["decision_ids"][0]
        detail = self._record(
            self.active_client.get(
                f"/api/v1/web/products/{PRODUCT_ID}/decisions/{decision_id}"
            )
        )
        self.assertEqual(200, detail.status_code, detail.text)
        self.assertEqual(PRODUCT_ID, detail.json()["product_id"])
        unreferenced_commit = self.create_unreferenced_remote_commit()
        self.assertTrue(
            subprocess.run(
                (
                    "git",
                    "-C",
                    str(self.remote),
                    "cat-file",
                    "-e",
                    f"{unreferenced_commit}^{{commit}}",
                ),
                capture_output=True,
            ).returncode
            == 0
        )
        self.assertFalse(self.remote_main_contains(unreferenced_commit))
        self.assertTrue(self.remote_main_contains(published["commit_sha"]))
        self.assertEqual(
            published["commit_sha"],
            self._git(
                "rev-parse", "--verify", "refs/heads/main", repository=self.remote
            )
            .decode("ascii")
            .strip(),
        )
        product_path = (
            self.repository
            / "decision-registry"
            / "products"
            / PRODUCT_ID
            / "product.json"
        )
        self.assertTrue(product_path.is_file())
        self.assertEqual(
            canonical_json_bytes(
                {
                    "format": "zdecision-product/v1",
                    "schema_version": 1,
                    "product_id": PRODUCT_ID,
                    "name": PRODUCT_NAME,
                }
            ),
            product_path.read_bytes(),
        )
        registry_path = (
            self.repository
            / "decision-registry"
            / "products"
            / PRODUCT_ID
            / "decisions"
            / decision_id
            / "r0001.json"
        )
        self.assertTrue(registry_path.is_file())

        self.active_store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        sqlite_bytes = self.database_path.read_bytes()
        response_bytes = "\n".join(self.http_fixture_json).encode("utf-8")
        git_blobs = self.all_git_blob_bytes()
        for sentinel in FORBIDDEN_FIELDS.values():
            encoded = sentinel.encode("utf-8")
            self.assertNotIn(encoded, sqlite_bytes)
            self.assertNotIn(encoded, response_bytes)
            self.assertNotIn(encoded, git_blobs)
        self.assertNotIn(REJECTED_CLAIM.encode("utf-8"), git_blobs)
        self.assertNotIn(SKIPPED_CLAIM.encode("utf-8"), git_blobs)
        self.vertical_ids = {
            "capture_request_id": request_id,
            "review_batch_id": review["review_batch_id"],
            "preview_id": preview["preview_id"],
            "publication_id": published["publication_id"],
            "decision_id": decision_id,
            "commit_sha": published["commit_sha"],
        }


if __name__ == "__main__":
    unittest.main()
