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
from zdecision.central.decision_spaces import (
    CatalogGroup,
    EnabledRepository,
    LeafDecisionSpace,
    RepositoryDecisionRoute,
)
from zdecision.central.registry_projection import RegistryProjectionStore
from zdecision.central.service import CaptureRequestService
from zdecision.central.store import CentralStore
from zdecision.central.web.application import CentralWebApplication
from zdecision.central.web.queries import CentralWebQueries
from zdecision.central.web.store import CentralWebStore
from zdecision.capture.models import CandidateContent, SourceCheckpoint
from zdecision.capture.reviews import ApprovalRef
from zdecision.ids import (
    candidate_revision_id,
    catalog_group_id,
    decision_id,
    decision_space_id,
    product_id,
    repository_route_id,
)
from zdecision.jsonio import canonical_json_bytes
from zdecision.registry.catalog import RegistryCatalog
from zdecision.registry.git import GitRegistryAdapter
from zdecision.registry.models import (
    DecisionHead,
    DecisionRevision,
    DecisionSeed,
    ProductMetadata,
    ProductRegistry,
    RootRegistry,
)
from zdecision.registry.query import RegistrySnapshot
from zdecision.sync.contracts import (
    CandidateOwnershipSnapshot,
    CandidateRevisionUpload,
    RepositoryView,
)


PRODUCT_NAME = "ZDecision"
PRODUCT_ID = product_id(PRODUCT_NAME)
PRODUCT_SPACE_ID = decision_space_id("product", PRODUCT_ID)
REPOSITORY_ID = "repo_" + "1" * 32
COMMIT_SHA = "b" * 40


def _registry_snapshot(decision: DecisionRevision) -> RegistrySnapshot:
    product_id = decision.product_id
    product_name = decision.product_name
    return RegistrySnapshot(
        COMMIT_SHA,
        {product_id: ProductMetadata(product_id, product_name)},
        {
            product_id: ProductRegistry(
                product_id,
                {
                    decision.decision_id: DecisionHead(
                        1,
                        "active",
                        f"decisions/{decision.decision_id}/r0001.json",
                    )
                },
            )
        },
        {(product_id, decision.decision_id): decision},
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
        self.store.put_repository(
            "org_demo", EnabledRepository(REPOSITORY_ID, True)
        )
        self.store.put_decision_space(
            "org_demo",
            LeafDecisionSpace(
                PRODUCT_SPACE_ID,
                "product",
                PRODUCT_NAME,
                PRODUCT_ID,
                PRODUCT_NAME,
                None,
                (),
                ".",
                None,
                None,
                True,
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
        formal_candidate_id = "cand_" + "4" * 32 + "_01"
        self.formal_decision = DecisionRevision.from_seed(
            DecisionSeed(
                candidate_id=formal_candidate_id,
                decision_id=decision_id(formal_candidate_id, PRODUCT_ID),
                product_id=PRODUCT_ID,
                product_name=PRODUCT_NAME,
                content=content,
                source=SourceCheckpoint("opaque-source", "opaque-checkpoint"),
                review_approval=ApprovalRef(
                    "user",
                    "review-thread",
                    "review-turn",
                    "2026-08-03T09:00:00Z",
                ),
            ),
            "pub_" + "4" * 32,
        )
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
        ownership = CandidateOwnershipSnapshot(
            repository_id=REPOSITORY_ID,
            route_id=repository_route_id(REPOSITORY_ID, PRODUCT_SPACE_ID),
            route_configuration_version=1,
            decision_space_id=PRODUCT_SPACE_ID,
            decision_space_kind="product",
            display_name=PRODUCT_NAME,
            catalog_breadcrumb=(),
            source_root=".",
            compatibility_product_id=PRODUCT_ID,
            compatibility_product_name=PRODUCT_NAME,
            source_boundary_digest="9" * 64,
        )
        ownership_bytes = canonical_json_bytes(ownership.to_dict())
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
            self.store.connection.execute(
                """
                INSERT INTO candidate_revision_ownership(
                    organization_id, repository_id, family_id, revision,
                    decision_space_id, route_id, route_configuration_version,
                    ownership_json, ownership_digest
                ) VALUES ('org_demo', ?, ?, 1, ?, ?, 1, ?, ?)
                """,
                (
                    REPOSITORY_ID,
                    family_id,
                    PRODUCT_SPACE_ID,
                    ownership.route_id,
                    ownership_bytes.decode("utf-8"),
                    hashlib.sha256(ownership_bytes).hexdigest(),
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
        git = GitRegistryAdapter(
            repository, expected_origin=str(remote.resolve())
        )
        self.registry_projection = RegistryProjectionStore(self.store.connection)
        self._install_registry_projection()
        web = CentralWebApplication(
            store=CentralWebStore(self.store.connection),
            queries=CentralWebQueries(
                self.store.connection, self.registry_projection
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

    def _install_registry_projection(self) -> None:
        snapshot = _registry_snapshot(self.formal_decision)
        self.registry_projection.mark_syncing(
            "org_demo", COMMIT_SHA, "1" * 40,
            "2026-08-06T10:00:00Z", "2026-08-06T10:00:00Z",
        )
        self.registry_projection.install(
            "org_demo", "1" * 40, snapshot,
            "2026-08-06T10:00:00Z", "2026-08-06T10:00:00Z",
        )

    def test_dashboard_is_serialized_by_the_web_transport(self) -> None:
        response = self.client.get("/api/v1/web/dashboard")

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(
            {
                "metrics": {
                    "product_count": 1,
                    "pending_candidate_count": 1,
                    "active_decision_count": 1,
                    "completed_this_week": 0,
                },
                "registry": {
                    "state": "available",
                    "commit_sha": COMMIT_SHA,
                    "verified_at": "2026-08-06T10:00:00Z",
                },
                "products": [
                    {
                        "decision_space_id": PRODUCT_SPACE_ID,
                        "kind": "product",
                        "display_name": PRODUCT_NAME,
                        "breadcrumb": [PRODUCT_NAME],
                        "source_root": ".",
                        "package_name": None,
                        "asset_type": None,
                        "repository_ids": [REPOSITORY_ID],
                        "pending_candidate_count": 1,
                        "active_decision_count": 1,
                        "last_activity_at": None,
                    }
                ],
                "shared_tree": None,
                "recent_publications": [],
            },
            response.json(),
        )

    def test_dashboard_counts_products_and_nests_shared_leaves(self) -> None:
        shared = CatalogGroup(
            catalog_group_id(("Shared",)), None, "Shared", ("Shared",), None, 20
        )
        self.store.put_catalog_group("org_demo", shared)
        specifications = (
            ("packages/products/shared", "zcf-audit", "cross_product_module"),
            ("packages/shared", "theme", "library"),
            ("packages", "design", "component_library"),
        )
        routes = []
        for order, (directory, name, asset_type) in enumerate(specifications, 1):
            group = CatalogGroup(
                catalog_group_id(("Shared", directory)),
                shared.catalog_group_id,
                directory,
                ("Shared", directory),
                directory,
                order,
            )
            self.store.put_catalog_group("org_demo", group)
            compatibility_name = f"Shared / {directory}/{name}"
            compatibility_id = product_id(compatibility_name)
            space = LeafDecisionSpace(
                decision_space_id("shared_unit", compatibility_id),
                "shared_unit",
                name,
                compatibility_id,
                compatibility_name,
                group.catalog_group_id,
                group.breadcrumb,
                f"{directory}/{name}",
                f"@zstack/{name}",
                asset_type,
                True,
            )
            self.store.put_decision_space("org_demo", space)
            routes.append(
                RepositoryDecisionRoute(
                    repository_route_id(REPOSITORY_ID, space.decision_space_id),
                    REPOSITORY_ID,
                    space.decision_space_id,
                    (space.source_root,),
                    (),
                    True,
                    1,
                )
            )
        self.store.replace_trusted_route_heads(
            "org_demo", REPOSITORY_ID, tuple(routes)
        )

        response = self.client.get("/api/v1/web/dashboard")

        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual(1, body["metrics"]["product_count"])
        self.assertEqual("Shared", body["shared_tree"]["display_name"])

        def leaf_names(node: dict[str, object]) -> list[str]:
            children = node["children"]
            assert isinstance(children, list)
            if not children:
                return [str(node["display_name"])]
            return [name for child in children for name in leaf_names(child)]

        self.assertEqual(
            ["design", "theme", "zcf-audit"],
            sorted(leaf_names(body["shared_tree"])),
        )

    def test_catalog_group_cannot_open_candidate_inbox(self) -> None:
        shared_group_id = catalog_group_id(("Shared",))
        self.store.put_catalog_group(
            "org_demo",
            CatalogGroup(
                shared_group_id, None, "Shared", ("Shared",), None, 20
            ),
        )

        response = self.client.get(
            f"/api/v1/web/spaces/{shared_group_id}/candidates"
        )

        self.assertEqual(404, response.status_code)
        self.assertEqual("decision_space_not_leaf", response.json()["error"])

    def test_repository_spaces_returns_only_enabled_server_leaves(self) -> None:
        private_space_id = decision_space_id(
            "shared_unit", product_id("Registry Only Private")
        )
        self.store.put_decision_space(
            "org_demo",
            LeafDecisionSpace(
                private_space_id,
                "shared_unit",
                "private",
                product_id("Registry Only Private"),
                "Registry Only Private",
                None,
                (),
                "private/package",
                "@private/package",
                "library",
                True,
            ),
        )

        response = self.client.get(
            f"/api/v1/web/repositories/{REPOSITORY_ID}/spaces"
        )

        self.assertEqual(200, response.status_code, response.text)
        identifiers = {
            item["decision_space_id"] for item in response.json()["spaces"]
        }
        self.assertEqual({PRODUCT_SPACE_ID}, identifiers)
        self.assertNotIn(private_space_id, response.text)

    def test_shared_canonical_flows_expose_leaf_identity_not_v1_partition(
        self,
    ) -> None:
        group = CatalogGroup(
            catalog_group_id(("Shared", "packages/shared")),
            None,
            "packages/shared",
            ("Shared", "packages/shared"),
            "packages/shared",
            1,
        )
        self.store.put_catalog_group("org_demo", group)
        compatibility_name = "Shared / packages/shared/theme"
        compatibility_id = product_id(compatibility_name)
        space = LeafDecisionSpace(
            decision_space_id("shared_unit", compatibility_id),
            "shared_unit",
            "theme",
            compatibility_id,
            compatibility_name,
            group.catalog_group_id,
            group.breadcrumb,
            "packages/shared/theme",
            "@zstack/theme",
            "library",
            True,
        )
        self.store.put_decision_space("org_demo", space)
        route = RepositoryDecisionRoute(
            repository_route_id(REPOSITORY_ID, space.decision_space_id),
            REPOSITORY_ID,
            space.decision_space_id,
            (space.source_root,),
            (),
            True,
            1,
        )
        self.store.replace_trusted_route_heads(
            "org_demo", REPOSITORY_ID, (route,)
        )
        shared_content = replace(
            self.revision.content, product=compatibility_name
        )
        content_digest = hashlib.sha256(
            canonical_json_bytes(shared_content.to_dict())
        ).hexdigest()
        shared_revision = CandidateRevisionUpload(
            family_id=self.revision.family_id,
            revision_id=candidate_revision_id(
                self.revision.family_id, 1, content_digest
            ),
            revision=1,
            content=shared_content,
            content_digest=content_digest,
            evidence_digest=self.revision.evidence_digest,
        )
        revision_bytes = canonical_json_bytes(shared_revision.to_dict())
        ownership = CandidateOwnershipSnapshot(
            repository_id=REPOSITORY_ID,
            route_id=route.route_id,
            route_configuration_version=1,
            decision_space_id=space.decision_space_id,
            decision_space_kind="shared_unit",
            display_name=space.display_name,
            catalog_breadcrumb=space.catalog_breadcrumb,
            source_root=space.source_root,
            compatibility_product_id=compatibility_id,
            compatibility_product_name=compatibility_name,
            source_boundary_digest="8" * 64,
        )
        ownership_bytes = canonical_json_bytes(ownership.to_dict())
        with self.store.connection:
            self.store.connection.execute(
                """
                UPDATE candidate_revisions
                SET revision_id = ?, record_json = ?, record_digest = ?
                WHERE organization_id = 'org_demo' AND repository_id = ?
                  AND family_id = ? AND revision = 1
                """,
                (
                    shared_revision.revision_id,
                    revision_bytes.decode("utf-8"),
                    hashlib.sha256(revision_bytes).hexdigest(),
                    REPOSITORY_ID,
                    shared_revision.family_id,
                ),
            )
            self.store.connection.execute(
                """
                UPDATE candidate_family_heads SET revision_id = ?
                WHERE organization_id = 'org_demo' AND repository_id = ?
                  AND family_id = ?
                """,
                (
                    shared_revision.revision_id,
                    REPOSITORY_ID,
                    shared_revision.family_id,
                ),
            )
            self.store.connection.execute(
                """
                UPDATE candidate_revision_ownership
                SET decision_space_id = ?, route_id = ?, ownership_json = ?,
                    ownership_digest = ?
                WHERE organization_id = 'org_demo' AND repository_id = ?
                  AND family_id = ? AND revision = 1
                """,
                (
                    space.decision_space_id,
                    route.route_id,
                    ownership_bytes.decode("utf-8"),
                    hashlib.sha256(ownership_bytes).hexdigest(),
                    REPOSITORY_ID,
                    shared_revision.family_id,
                ),
            )
        self.revision = shared_revision
        formal_candidate_id = "cand_" + "7" * 32 + "_01"
        self.formal_decision = DecisionRevision.from_seed(
            DecisionSeed(
                candidate_id=formal_candidate_id,
                decision_id=decision_id(formal_candidate_id, compatibility_id),
                product_id=compatibility_id,
                product_name=compatibility_name,
                content=shared_content,
                source=SourceCheckpoint("opaque-shared", "shared-checkpoint"),
                review_approval=ApprovalRef(
                    "user",
                    "shared-review-thread",
                    "shared-review-turn",
                    "2026-08-03T09:00:00Z",
                ),
            ),
            "pub_" + "7" * 32,
        )
        self._install_registry_projection()
        expected_space = {
            "decision_space_id": space.decision_space_id,
            "kind": "shared_unit",
            "display_name": "theme",
            "breadcrumb": ["Shared", "packages/shared", "theme"],
            "source_root": "packages/shared/theme",
            "package_name": "@zstack/theme",
            "asset_type": "library",
        }

        inbox = self.client.get(
            f"/api/v1/web/spaces/{space.decision_space_id}/candidates"
        )
        catalog = self.client.get(
            f"/api/v1/web/spaces/{space.decision_space_id}/decisions"
        )
        detail = self.client.get(
            f"/api/v1/web/spaces/{space.decision_space_id}/decisions/"
            f"{self.formal_decision.decision_id}"
        )
        draft = self.client.put(
            f"/api/v1/web/spaces/{space.decision_space_id}/review-draft",
            json=self.draft_body(action="accept"),
        ).json()
        review = self.client.post(
            f"/api/v1/web/spaces/{space.decision_space_id}/reviews",
            json={
                "client_action_id": "web_action_shared-review",
                "expected_draft_version": draft["version"],
                "items": draft["items"],
            },
        ).json()
        preview = self.client.post(
            f"/api/v1/web/reviews/{review['review_batch_id']}/previews",
            json={"client_action_id": "web_action_shared-preview"},
        )

        self.assertEqual(200, inbox.status_code, inbox.text)
        self.assertEqual(expected_space, inbox.json()["space"])
        self.assertEqual(200, catalog.status_code, catalog.text)
        self.assertEqual(expected_space, catalog.json()["items"][0]["space"])
        self.assertEqual(200, detail.status_code, detail.text)
        self.assertEqual(expected_space, detail.json()["space"])
        self.assertEqual(200, preview.status_code, preview.text)
        self.assertEqual(expected_space, preview.json()["space"])
        canonical_payloads = (
            inbox.json(),
            catalog.json()["items"][0],
            detail.json(),
            preview.json(),
        )
        for payload in canonical_payloads:
            self.assertNotIn("product_id", payload)
            self.assertNotIn("product_name", payload)
        formal_detail = json.loads(detail.json()["canonical_json"])
        self.assertEqual(compatibility_id, formal_detail["product_id"])
        self.assertEqual(compatibility_name, formal_detail["product_name"])
        self.assertEqual(
            compatibility_id, preview.json()["decisions"][0]["product_id"]
        )

        published = self.client.post(
            f"/api/v1/web/publication-previews/"
            f"{preview.json()['preview_id']}/publish",
            json={"client_action_id": "web_action_shared-publish"},
        )
        history = self.client.get(
            f"/api/v1/web/spaces/{space.decision_space_id}/publications"
        )
        publication = self.client.get(
            f"/api/v1/web/publications/{published.json()['publication_id']}"
        )
        self.assertEqual(200, published.status_code, published.text)
        self.assertEqual(expected_space, published.json()["space"])
        self.assertEqual(200, history.status_code, history.text)
        self.assertEqual(expected_space, history.json()["items"][0]["space"])
        self.assertEqual(200, publication.status_code, publication.text)
        self.assertEqual(expected_space, publication.json()["space"])
        for payload in (
            published.json(),
            history.json()["items"][0],
            publication.json(),
        ):
            self.assertNotIn("product_id", payload)
            self.assertNotIn("product_name", payload)

        self.store.replace_trusted_route_heads(
            "org_demo",
            REPOSITORY_ID,
            (replace(route, enabled=False, configuration_version=2),),
        )
        with self.store.connection:
            self.store.connection.execute(
                """UPDATE decision_spaces SET enabled = 0
                WHERE organization_id = 'org_demo'
                  AND decision_space_id = ?""",
                (space.decision_space_id,),
            )

        disabled_history = self.client.get(
            f"/api/v1/web/spaces/{space.decision_space_id}/publications"
        )
        disabled_publication = self.client.get(
            f"/api/v1/web/publications/{published.json()['publication_id']}"
        )
        all_history = self.client.get("/api/v1/web/publications")
        disabled_candidate = self.client.get(
            f"/api/v1/web/spaces/{space.decision_space_id}/candidates"
        )
        disabled_review = self.client.post(
            f"/api/v1/web/spaces/{space.decision_space_id}/reviews",
            json={
                "client_action_id": "web_action_disabled-review",
                "expected_draft_version": draft["version"],
                "items": draft["items"],
            },
        )

        self.assertEqual(200, disabled_history.status_code, disabled_history.text)
        self.assertEqual(1, disabled_history.json()["total"])
        self.assertEqual(
            expected_space, disabled_history.json()["items"][0]["space"]
        )
        self.assertEqual(
            200, disabled_publication.status_code, disabled_publication.text
        )
        self.assertEqual(expected_space, disabled_publication.json()["space"])
        self.assertEqual(200, all_history.status_code, all_history.text)
        self.assertEqual(expected_space, all_history.json()["items"][0]["space"])
        for payload in (
            disabled_history.json()["items"][0],
            disabled_publication.json(),
            all_history.json()["items"][0],
        ):
            self.assertNotIn("product_id", payload)
            self.assertNotIn("product_name", payload)
        self.assertEqual(404, disabled_candidate.status_code)
        self.assertEqual({"error": "not_found"}, disabled_candidate.json())
        self.assertEqual(404, disabled_review.status_code)
        self.assertEqual({"error": "not_found"}, disabled_review.json())

    def test_disabled_leaf_rejects_preview_creation(self) -> None:
        draft_response = self.client.put(
            f"/api/v1/web/spaces/{PRODUCT_SPACE_ID}/review-draft",
            json=self.draft_body(action="accept"),
        )
        self.assertEqual(200, draft_response.status_code, draft_response.text)
        draft = draft_response.json()
        review_response = self.client.post(
            f"/api/v1/web/spaces/{PRODUCT_SPACE_ID}/reviews",
            json={
                "client_action_id": "web_action_before-disable",
                "expected_draft_version": draft["version"],
                "items": draft["items"],
            },
        )
        self.assertEqual(200, review_response.status_code, review_response.text)
        review = review_response.json()
        self.assertTrue(review["preview_eligible"])
        with self.store.connection:
            self.store.connection.execute(
                """UPDATE decision_spaces SET enabled = 0
                WHERE organization_id = 'org_demo'
                  AND decision_space_id = ?""",
                (PRODUCT_SPACE_ID,),
            )

        response = self.client.post(
            f"/api/v1/web/reviews/{review['review_batch_id']}/previews",
            json={"client_action_id": "web_action_after-disable"},
        )

        self.assertEqual(409, response.status_code)
        self.assertEqual(
            {"error": "no_accepted_items"},
            response.json(),
        )

    def test_decision_catalog_and_detail_are_complete_read_only_views(self) -> None:
        catalog = self.client.get(
            "/api/v1/web/decisions", params={"search": "explicit Review"}
        )
        detail = self.client.get(
            f"/api/v1/web/products/{PRODUCT_ID}/decisions/"
            f"{self.formal_decision.decision_id}"
        )

        self.assertEqual(200, catalog.status_code, catalog.text)
        self.assertEqual(COMMIT_SHA, catalog.json()["registry_commit"])
        self.assertEqual(1, catalog.json()["total"])
        self.assertEqual(
            self.formal_decision.decision_id,
            catalog.json()["items"][0]["decision_id"],
        )
        self.assertEqual(200, detail.status_code, detail.text)
        payload = detail.json()
        self.assertEqual(self.formal_decision.to_dict()["source"], payload["source"])
        self.assertEqual(
            self.formal_decision.to_dict(), json.loads(payload["canonical_json"])
        )
        self.assertEqual(COMMIT_SHA, payload["registry_commit"])
        self.assertTrue(
            {"publication_id", "published_at", "commit_sha"}.issubset(payload)
        )
        self.assertTrue(
            {"update", "delete", "supersede", "retire"}.isdisjoint(payload)
        )

    def test_decision_routes_distinguish_mismatch_unavailable_and_invalid_filters(
        self,
    ) -> None:
        other_name = "Other Product"
        other_id = product_id(other_name)
        self.store.put_repository_mapping(
            "org_demo",
            RepositoryView(
                "repo_" + "9" * 32, other_id, other_name, True
            ),
        )
        mismatch = self.client.get(
            f"/api/v1/web/products/{other_id}/decisions/"
            f"{self.formal_decision.decision_id}"
        )
        invalid = self.client.get(
            "/api/v1/web/decisions", params={"search": "界" * 67}
        )
        self.registry_projection.mark_unavailable(
            "org_demo", None, None, None,
            "2026-08-06T10:00:00Z", "git_proof_failed",
        )
        unavailable = self.client.get("/api/v1/web/decisions")

        self.assertEqual(404, mismatch.status_code)
        self.assertEqual({"error": "not_found"}, mismatch.json())
        self.assertEqual(422, invalid.status_code)
        self.assertEqual({"error": "invalid_request"}, invalid.json())
        self.assertEqual(503, unavailable.status_code)
        self.assertEqual({"error": "registry_unavailable"}, unavailable.json())

    def test_spa_fallback_serves_browser_routes_but_never_api_misses(
        self,
    ) -> None:
        browser_routes = (
            "/",
            "/reviews",
            f"/products/{PRODUCT_ID}/candidates",
            "/publication-previews/pub_" + "2" * 32,
            "/decisions",
            f"/products/{PRODUCT_ID}/decisions/{self.formal_decision.decision_id}",
            "/publications",
            "/publications/plb_" + "3" * 32,
        )
        browsers = [self.client.get(path) for path in browser_routes]
        api = self.client.get("/api/v1/web/not-a-route")
        asset = self.client.get("/assets/shell.css")

        self.assertTrue(all(response.status_code == 200 for response in browsers))
        self.assertEqual(
            {"<!doctype html><title>central shell</title>"},
            {response.text for response in browsers},
        )
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
            {"error": "decision_space_ownership_conflict"}, cross_product.json()
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
        active = self.registry_projection.load_active("org_demo")
        self.assertIsNotNone(active)
        self.assertEqual(COMMIT_SHA, active.commit_sha)
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
        self.assertEqual(published.json(), detail.json())
        self.assertTrue(
            {
                "organization_id",
                "confirm_action_id",
                "confirm_request_digest",
                "approval",
            }.isdisjoint(published.json())
        )
        self.assertEqual(200, history.status_code, history.text)
        self.assertEqual(1, history.json()["total"])
        row = history.json()["items"][0]
        self.assertNotIn("product_name", row)
        self.assertNotIn("product_id", row)
        self.assertEqual(
            PRODUCT_NAME,
            product_history.json()["items"][0]["product_name"],
        )
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
