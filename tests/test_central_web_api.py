from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
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
from zdecision.capture.models import CandidateContent
from zdecision.ids import candidate_revision_id, product_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.registry.catalog import RegistryCatalog
from zdecision.registry.git import GitRegistryAdapter
from zdecision.registry.models import ProductMetadata, ProductRegistry, RootRegistry
from zdecision.registry.query import RegistrySnapshot
from zdecision.sync.contracts import CandidateRevisionUpload, RepositoryView


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
        remote = root / "remote.git"
        repository = root / "repository"
        subprocess.run(
            ("git", "init", "--bare", str(remote)),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ("git", "init", "-b", "main", str(repository)),
            check=True,
            capture_output=True,
        )
        for key, value in (
            ("user.email", "tests@example.com"),
            ("user.name", "ZDecision Tests"),
        ):
            subprocess.run(
                ("git", "-C", str(repository), "config", key, value),
                check=True,
            )
        registry = repository / "decision-registry"
        registry.mkdir()
        (registry / "registry.json").write_bytes(
            canonical_json_bytes(RootRegistry({}).to_dict())
        )
        for command in (
            ("add", "decision-registry"),
            ("commit", "-m", "initial registry"),
            ("remote", "add", "origin", str(remote.resolve())),
            ("push", "-u", "origin", "main"),
        ):
            subprocess.run(
                ("git", "-C", str(repository), *command),
                check=True,
                capture_output=True,
            )
        self.store = CentralStore.open(root / "central.sqlite3")
        self.addCleanup(self.store.close)
        self.store.put_repository_mapping(
            "org_demo",
            RepositoryView(
                REPOSITORY_ID, PRODUCT_ID, PRODUCT_NAME, True
            ),
        )
        content = CandidateContent(
            product=PRODUCT_NAME,
            claim="Review the current product Candidate.",
            future_action="Keep the explicit Review boundary.",
            scope_summary="Central Web",
            repositories=("zdecision",),
            paths=("src/zdecision/central/web/",),
            invalidation_conditions=("The Web boundary changes.",),
        )
        content_digest = hashlib.sha256(
            canonical_json_bytes(content.to_dict())
        ).hexdigest()
        family_id = "cfm_" + "a" * 32
        self.revision = CandidateRevisionUpload(
            family_id=family_id,
            revision_id=candidate_revision_id(family_id, 1, content_digest),
            revision=1,
            content=content,
            content_digest=content_digest,
            evidence_digest="e" * 64,
        )
        record = canonical_json_bytes(self.revision.to_dict())
        with self.store.connection:
            self.store.connection.execute(
                """
                INSERT INTO candidate_revisions(
                    organization_id, repository_id, family_id, revision,
                    revision_id, record_json, record_digest
                ) VALUES ('org_demo', ?, ?, 1, ?, ?, ?)
                """,
                (
                    REPOSITORY_ID,
                    family_id,
                    self.revision.revision_id,
                    record.decode("utf-8"),
                    hashlib.sha256(record).hexdigest(),
                ),
            )
            self.store.connection.execute(
                """
                INSERT INTO candidate_family_heads(
                    organization_id, repository_id, family_id, revision,
                    revision_id
                ) VALUES ('org_demo', ?, ?, 1, ?)
                """,
                (REPOSITORY_ID, family_id, self.revision.revision_id),
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
        git = GitRegistryAdapter(
            repository, expected_origin=str(remote.resolve())
        )
        web = CentralWebApplication(
            store=CentralWebStore(self.store.connection),
            queries=CentralWebQueries(
                self.store.connection, _RegistryQuery()
            ),
            catalog=RegistryCatalog(repository),
            git=git,
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
                    "pending_candidate_count": 1,
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
                        "pending_candidate_count": 1,
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

    def draft_body(
        self, *, expected_version: int = 0, action: str = "accept"
    ) -> dict[str, object]:
        return {
            "expected_version": expected_version,
            "items": [
                {
                    "family_id": self.revision.family_id,
                    "repository_id": REPOSITORY_ID,
                    "revision_id": self.revision.revision_id,
                    "revision": self.revision.revision,
                    "content_digest": self.revision.content_digest,
                    "action": action,
                    "effective_content": None,
                    "note": None,
                }
            ],
        }

    def test_candidate_and_draft_routes_restore_the_saved_product_draft(
        self,
    ) -> None:
        saved = self.client.put(
            f"/api/v1/web/products/{PRODUCT_ID}/review-draft",
            json=self.draft_body(),
        )
        inbox = self.client.get(
            f"/api/v1/web/products/{PRODUCT_ID}/candidates"
        )
        restored = self.client.get(
            f"/api/v1/web/products/{PRODUCT_ID}/review-draft"
        )

        self.assertEqual(200, saved.status_code, saved.text)
        self.assertEqual(1, saved.json()["version"])
        self.assertEqual(200, inbox.status_code, inbox.text)
        self.assertEqual("accept", inbox.json()["items"][0]["draft_action"])
        self.assertEqual(saved.json(), restored.json())
        self.assertNotIn("session", json.dumps(inbox.json()).lower())

    def test_draft_route_maps_cas_and_malformed_edits_to_stable_errors(
        self,
    ) -> None:
        first = self.client.put(
            f"/api/v1/web/products/{PRODUCT_ID}/review-draft",
            json=self.draft_body(),
        )
        conflict = self.client.put(
            f"/api/v1/web/products/{PRODUCT_ID}/review-draft",
            json=self.draft_body(action="reject"),
        )
        malformed_body = self.draft_body(expected_version=1, action="edit_accept")
        malformed_body["items"][0]["effective_content"] = {
            **self.revision.content.to_dict(),
            "product": "Client-selected product",
        }
        malformed = self.client.put(
            f"/api/v1/web/products/{PRODUCT_ID}/review-draft",
            json=malformed_body,
        )

        self.assertEqual(200, first.status_code)
        self.assertEqual(409, conflict.status_code)
        self.assertEqual({"error": "review_draft_conflict"}, conflict.json())
        self.assertEqual(422, malformed.status_code)
        self.assertEqual({"error": "invalid_request"}, malformed.json())

    def test_unknown_product_and_cross_product_repository_are_not_empty_reads(
        self,
    ) -> None:
        unknown = self.client.get(
            "/api/v1/web/products/prod_ffffffffffffffffffffffffffffffff/candidates"
        )
        other_name = "Other Product"
        other_id = product_id(other_name)
        other_repository = "repo_" + "2" * 32
        self.store.put_repository_mapping(
            "org_demo",
            RepositoryView(other_repository, other_id, other_name, True),
        )
        cross_product = self.client.get(
            f"/api/v1/web/products/{PRODUCT_ID}/candidates",
            params={"repository_id": other_repository},
        )

        self.assertEqual(404, unknown.status_code)
        self.assertEqual(409, cross_product.status_code)
        self.assertEqual(
            {"error": "product_ownership_conflict"}, cross_product.json()
        )

    def test_review_route_returns_only_safe_submission_results(self) -> None:
        draft = self.client.put(
            f"/api/v1/web/products/{PRODUCT_ID}/review-draft",
            json=self.draft_body(action="accept"),
        )
        body = {
            "client_action_id": "web_action_api-review",
            "expected_draft_version": draft.json()["version"],
            "items": draft.json()["items"],
        }

        submitted = self.client.post(
            f"/api/v1/web/products/{PRODUCT_ID}/reviews", json=body
        )

        self.assertEqual(200, submitted.status_code, submitted.text)
        self.assertEqual(
            {
                "review_batch_id",
                "items",
                "preview_eligible",
                "remaining_pending_count",
                "draft_version",
            },
            set(submitted.json()),
        )
        self.assertTrue(submitted.json()["preview_eligible"])
        self.assertEqual(0, submitted.json()["remaining_pending_count"])
        self.assertEqual(2, submitted.json()["draft_version"])
        self.assertEqual("accept", submitted.json()["items"][0]["action"])
        self.assertNotIn("note", submitted.json()["items"][0])
        self.assertNotIn("effective_content", submitted.json()["items"][0])
        self.assertEqual(
            0,
            self.store.connection.execute(
                "SELECT COUNT(*) FROM web_publication_previews"
            ).fetchone()[0],
        )

    def test_preview_routes_return_exact_safe_artifact_and_replay(self) -> None:
        draft = self.client.put(
            f"/api/v1/web/products/{PRODUCT_ID}/review-draft",
            json=self.draft_body(action="accept"),
        ).json()
        review = self.client.post(
            f"/api/v1/web/products/{PRODUCT_ID}/reviews",
            json={
                "client_action_id": "web_action_api-preview-review",
                "expected_draft_version": draft["version"],
                "items": draft["items"],
            },
        ).json()
        body = {"client_action_id": "web_action_api-preview"}

        created = self.client.post(
            f"/api/v1/web/reviews/{review['review_batch_id']}/previews",
            json=body,
        )
        replay = self.client.post(
            f"/api/v1/web/reviews/{review['review_batch_id']}/previews",
            json=body,
        )
        loaded = self.client.get(
            f"/api/v1/web/publication-previews/{created.json()['preview_id']}"
        )

        self.assertEqual(200, created.status_code, created.text)
        self.assertEqual(created.json(), replay.json())
        self.assertEqual(created.json(), loaded.json())
        payload = created.json()
        self.assertEqual("publishable", payload["publishability"])
        self.assertIsNone(payload["publication_id"])
        self.assertEqual(COMMIT_SHA, _RegistryQuery().snapshot().commit_sha)
        self.assertRegex(payload["base_commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(1, len(payload["decisions"]))
        decision = payload["decisions"][0]
        changed_decision = next(
            file for file in payload["changed_files"]
            if file["path"] == decision["path"]
        )
        self.assertEqual(self.revision.content.claim, decision["claim"])
        self.assertEqual(decision["canonical_json"], changed_decision["content"])
        self.assertNotIn("note", json.dumps(payload))
        self.assertNotIn("session", json.dumps(payload).lower())
        self.assertEqual(
            0,
            self.store.connection.execute(
                "SELECT COUNT(*) FROM web_publications"
            ).fetchone()[0],
        )

    def test_preview_routes_are_strict_and_do_not_create_publications(self) -> None:
        malformed = self.client.post(
            "/api/v1/web/reviews/rvb_" + "a" * 32 + "/previews",
            json={"client_action_id": "not-an-action", "actor_id": "user_demo"},
        )
        missing = self.client.get(
            "/api/v1/web/publication-previews/pub_" + "f" * 32
        )

        self.assertEqual(422, malformed.status_code)
        self.assertEqual({"error": "invalid_request"}, malformed.json())
        self.assertEqual(404, missing.status_code)
        self.assertEqual({"error": "not_found"}, missing.json())
        self.assertEqual(
            0,
            self.store.connection.execute(
                "SELECT COUNT(*) FROM web_publications"
            ).fetchone()[0],
        )

    def test_review_route_is_strict_and_reports_only_stale_family_ids(
        self,
    ) -> None:
        draft = self.client.put(
            f"/api/v1/web/products/{PRODUCT_ID}/review-draft",
            json=self.draft_body(action="reject"),
        ).json()
        malformed = self.client.post(
            f"/api/v1/web/products/{PRODUCT_ID}/reviews",
            json={
                "client_action_id": "not-a-web-action",
                "expected_draft_version": draft["version"],
                "items": draft["items"],
                "actor_id": "untrusted_actor",
            },
        )
        with self.store.connection:
            self.store.connection.execute(
                """
                UPDATE candidate_family_heads
                SET revision_id = ?
                WHERE organization_id = 'org_demo' AND repository_id = ?
                  AND family_id = ?
                """,
                ("crv_" + "f" * 32, REPOSITORY_ID, self.revision.family_id),
            )
        stale = self.client.post(
            f"/api/v1/web/products/{PRODUCT_ID}/reviews",
            json={
                "client_action_id": "web_action_api-stale",
                "expected_draft_version": draft["version"],
                "items": draft["items"],
            },
        )

        self.assertEqual(422, malformed.status_code)
        self.assertEqual({"error": "invalid_request"}, malformed.json())
        self.assertEqual(409, stale.status_code, stale.text)
        self.assertEqual(
            {"error": "review_stale", "family_ids": [self.revision.family_id]},
            stale.json(),
        )
        self.assertEqual(
            0,
            self.store.connection.execute(
                "SELECT COUNT(*) FROM web_review_batches"
            ).fetchone()[0],
        )

    def test_publication_routes_require_one_explicit_action_and_return_safe_history(
        self,
    ) -> None:
        draft = self.client.put(
            f"/api/v1/web/products/{PRODUCT_ID}/review-draft",
            json=self.draft_body(action="accept"),
        ).json()
        review = self.client.post(
            f"/api/v1/web/products/{PRODUCT_ID}/reviews",
            json={
                "client_action_id": "web_action_api-publish-review",
                "expected_draft_version": draft["version"],
                "items": draft["items"],
            },
        ).json()
        preview = self.client.post(
            f"/api/v1/web/reviews/{review['review_batch_id']}/previews",
            json={"client_action_id": "web_action_api-publish-preview"},
        ).json()
        before = self.store.connection.execute(
            "SELECT COUNT(*) FROM web_publications"
        ).fetchone()[0]

        malformed = self.client.post(
            f"/api/v1/web/publication-previews/{preview['preview_id']}/publish",
            json={
                "client_action_id": "web_action_api-publish",
                "actor_id": "browser_claimed_authority",
            },
        )
        published = self.client.post(
            f"/api/v1/web/publication-previews/{preview['preview_id']}/publish",
            json={"client_action_id": "web_action_api-publish"},
        )
        publication_id = published.json()["publication_id"]
        replay = self.client.post(
            f"/api/v1/web/publication-previews/{preview['preview_id']}/publish",
            json={"client_action_id": "web_action_api-publish"},
        )
        resumed = self.client.post(
            f"/api/v1/web/publications/{publication_id}/resume",
            json={"client_action_id": "web_action_api-resume"},
        )
        history = self.client.get("/api/v1/web/publications")
        product_history = self.client.get(
            "/api/v1/web/publications", params={"product_id": PRODUCT_ID}
        )
        detail = self.client.get(
            f"/api/v1/web/publications/{publication_id}"
        )

        self.assertEqual(0, before)
        self.assertEqual(422, malformed.status_code)
        self.assertEqual({"error": "invalid_request"}, malformed.json())
        self.assertEqual(200, published.status_code, published.text)
        self.assertEqual("completed", published.json()["state"])
        self.assertEqual(published.json(), replay.json())
        self.assertEqual(published.json(), resumed.json())
        self.assertEqual(200, history.status_code, history.text)
        self.assertEqual(history.json(), product_history.json())
        self.assertEqual(1, history.json()["total"])
        row = history.json()["items"][0]
        self.assertEqual(PRODUCT_NAME, row["product_name"])
        self.assertEqual("user_demo", row["actor_id"])
        self.assertEqual("completed", row["state"])
        self.assertEqual([self.revision.family_id], [self.revision.family_id])
        self.assertEqual(row, {
            key: detail.json()[key] for key in row
        })
        encoded = json.dumps(history.json())
        self.assertNotIn("note", encoded)
        self.assertNotIn(self.revision.content.claim, encoded)
        self.assertEqual(1, len(detail.json()["decision_ids"]))

    def test_ambiguous_publication_resume_is_a_stable_409(self) -> None:
        draft = self.client.put(
            f"/api/v1/web/products/{PRODUCT_ID}/review-draft",
            json=self.draft_body(action="accept"),
        ).json()
        review = self.client.post(
            f"/api/v1/web/products/{PRODUCT_ID}/reviews",
            json={
                "client_action_id": "web_action_api-ambiguous-review",
                "expected_draft_version": draft["version"],
                "items": draft["items"],
            },
        ).json()
        preview = self.client.post(
            f"/api/v1/web/reviews/{review['review_batch_id']}/previews",
            json={"client_action_id": "web_action_api-ambiguous-preview"},
        ).json()

        class InjectedCrash(Exception):
            pass

        publications = self.client.app.state.web_application.publications
        publications.checkpoint = lambda name: (
            (_ for _ in ()).throw(InjectedCrash())
            if name == "after_confirmation" else None
        )
        with self.assertRaises(InjectedCrash):
            self.client.post(
                f"/api/v1/web/publication-previews/{preview['preview_id']}/publish",
                json={"client_action_id": "web_action_api-ambiguous-publish"},
            )
        publications.checkpoint = lambda _: None
        web_store = CentralWebStore(self.store.connection)
        confirmed = web_store.get_publication_by_preview(
            "org_demo", preview["preview_id"]
        )
        web_store.replace_publication(
            confirmed, replace(confirmed, recovery_code="ambiguous")
        )

        response = self.client.post(
            f"/api/v1/web/publications/{confirmed.publication_id}/resume",
            json={"client_action_id": "web_action_api-ambiguous-resume"},
        )

        self.assertEqual(409, response.status_code)
        self.assertEqual({"error": "publication_ambiguous"}, response.json())


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
