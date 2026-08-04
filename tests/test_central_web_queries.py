from __future__ import annotations

import tempfile
import subprocess
import unittest
from pathlib import Path

from zdecision.central.auth import Principal
from zdecision.central.store import CentralStore
from zdecision.central.web.queries import CentralWebQueries
from zdecision.ids import product_id
from zdecision.registry.models import ProductMetadata, ProductRegistry
from zdecision.registry.git import GitRegistryAdapter
from zdecision.jsonio import atomic_write_json
from zdecision.registry.query import (
    RegistryQuery,
    RegistryQueryUnavailable,
    RegistrySnapshot,
)
from zdecision.sync.contracts import RepositoryView


PRODUCT_NAME = "ZDecision"
PRODUCT_ID = product_id(PRODUCT_NAME)
PRODUCT_REPOSITORY_ID = "repo_" + "1" * 32
OTHER_PRODUCT_NAME = "Other Product"
OTHER_PRODUCT_ID = product_id(OTHER_PRODUCT_NAME)
OTHER_REPOSITORY_ID = "repo_" + "2" * 32
COMMIT_SHA = "a" * 40


class _RegistryQuery:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable

    def snapshot(self) -> RegistrySnapshot:
        if self.unavailable:
            raise RegistryQueryUnavailable("registry_unavailable")
        return RegistrySnapshot(
            commit_sha=COMMIT_SHA,
            products={
                PRODUCT_ID: ProductMetadata(PRODUCT_ID, PRODUCT_NAME),
                OTHER_PRODUCT_ID: ProductMetadata(
                    OTHER_PRODUCT_ID, OTHER_PRODUCT_NAME
                ),
            },
            registries={
                PRODUCT_ID: ProductRegistry(PRODUCT_ID, {}),
                OTHER_PRODUCT_ID: ProductRegistry(OTHER_PRODUCT_ID, {}),
            },
            decisions={},
        )


class RegistryQueryTest(unittest.TestCase):
    def test_uncommitted_canonical_registry_content_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
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
            atomic_write_json(
                registry / "registry.json",
                {
                    "format": "zdecision-registry/v1",
                    "schema_version": 1,
                    "products": {},
                },
            )
            subprocess.run(
                ("git", "-C", str(repository), "add", "decision-registry"),
                check=True,
            )
            subprocess.run(
                ("git", "-C", str(repository), "commit", "-m", "registry"),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                (
                    "git",
                    "-C",
                    str(repository),
                    "remote",
                    "add",
                    "origin",
                    str(remote.resolve()),
                ),
                check=True,
            )
            subprocess.run(
                ("git", "-C", str(repository), "push", "-u", "origin", "main"),
                check=True,
                capture_output=True,
            )
            product_root = registry / "products" / PRODUCT_ID
            product_root.mkdir(parents=True)
            atomic_write_json(
                product_root / "product.json",
                ProductMetadata(PRODUCT_ID, PRODUCT_NAME).to_dict(),
            )
            atomic_write_json(
                product_root / "registry.json",
                ProductRegistry(PRODUCT_ID, {}).to_dict(),
            )
            atomic_write_json(
                registry / "registry.json",
                {
                    "format": "zdecision-registry/v1",
                    "schema_version": 1,
                    "products": {
                        PRODUCT_ID: {
                            "name": PRODUCT_NAME,
                            "product_path": (
                                f"products/{PRODUCT_ID}/product.json"
                            ),
                            "registry_path": (
                                f"products/{PRODUCT_ID}/registry.json"
                            ),
                        }
                    },
                },
            )
            query = RegistryQuery(
                repository,
                GitRegistryAdapter(
                    repository, expected_origin=str(remote.resolve())
                ),
            )

            with self.assertRaises(RegistryQueryUnavailable):
                query.snapshot()


class CentralWebQueriesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = CentralStore.open(
            Path(self.temporary_directory.name) / "central.sqlite3"
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.addCleanup(self.store.close)
        self.user = Principal("user", "org_demo", "user_demo", None)
        self.store.put_repository_mapping(
            "org_demo",
            RepositoryView(
                PRODUCT_REPOSITORY_ID,
                PRODUCT_ID,
                PRODUCT_NAME,
                True,
            ),
        )
        self.store.put_repository_mapping(
            "org_other",
            RepositoryView(
                OTHER_REPOSITORY_ID,
                OTHER_PRODUCT_ID,
                OTHER_PRODUCT_NAME,
                True,
            ),
        )
        with self.store.connection:
            self.store.connection.executemany(
                """
                INSERT INTO candidate_family_heads(
                    organization_id, repository_id, family_id,
                    revision, revision_id
                ) VALUES (?, ?, ?, 1, ?)
                """,
                (
                    (
                        "org_demo",
                        PRODUCT_REPOSITORY_ID,
                        "cfm_" + "1" * 32,
                        "crv_" + "1" * 32,
                    ),
                    (
                        "org_other",
                        OTHER_REPOSITORY_ID,
                        "cfm_" + "2" * 32,
                        "crv_" + "2" * 32,
                    ),
                ),
            )
        self.queries = CentralWebQueries(
            self.store.connection, _RegistryQuery()
        )

    def test_dashboard_derives_products_and_counts_from_owned_sources(
        self,
    ) -> None:
        dashboard = self.queries.dashboard(self.user)

        self.assertEqual(
            [PRODUCT_ID], [item.product_id for item in dashboard.products]
        )
        self.assertEqual(1, dashboard.metrics.product_count)
        self.assertEqual(1, dashboard.metrics.pending_candidate_count)
        self.assertEqual("available", dashboard.registry.state)
        self.assertEqual(COMMIT_SHA, dashboard.registry.commit_sha)
        self.assertEqual(0, dashboard.metrics.active_decision_count)
        self.assertEqual((), dashboard.recent_publications)

    def test_unknown_repository_has_no_product_route(self) -> None:
        self.assertIsNone(
            self.queries.resolve_repository(
                self.user, "repo_" + "f" * 32
            )
        )

    def test_second_organization_never_contributes_dashboard_rows(self) -> None:
        dashboard = self.queries.dashboard(self.user)

        self.assertEqual((PRODUCT_REPOSITORY_ID,), dashboard.products[0].repository_ids)
        self.assertEqual(1, dashboard.products[0].pending_candidate_count)
        self.assertNotIn(OTHER_PRODUCT_ID, dashboard.to_dict().__repr__())

    def test_registry_unavailable_is_not_reported_as_an_empty_registry(
        self,
    ) -> None:
        dashboard = CentralWebQueries(
            self.store.connection, _RegistryQuery(unavailable=True)
        ).dashboard(self.user)

        self.assertEqual("unavailable", dashboard.registry.state)
        self.assertIsNone(dashboard.registry.commit_sha)
        self.assertIsNone(dashboard.metrics.active_decision_count)
        self.assertIsNone(dashboard.products[0].active_decision_count)
        self.assertEqual(1, dashboard.metrics.pending_candidate_count)

    def test_only_the_latest_matching_terminal_review_resolves_a_head(
        self,
    ) -> None:
        self._insert_review("1", "accept", "2026-08-04T01:00:00Z")
        self.assertEqual(
            0, self.queries.dashboard(self.user).metrics.pending_candidate_count
        )

        self._insert_review("2", "skip", "2026-08-04T02:00:00Z")
        self.assertEqual(
            1, self.queries.dashboard(self.user).metrics.pending_candidate_count
        )

    def _insert_review(self, ordinal: str, action: str, created_at: str) -> None:
        batch_id = "rvb_" + ordinal * 32
        with self.store.connection:
            self.store.connection.execute(
                """
                INSERT INTO web_review_batches(
                    organization_id, product_id, review_batch_id, actor_id,
                    client_action_id, request_digest, record_json,
                    record_digest, created_at
                ) VALUES (?, ?, ?, 'user_demo', ?, ?, '{}', ?, ?)
                """,
                (
                    "org_demo",
                    PRODUCT_ID,
                    batch_id,
                    f"web_action_review-{ordinal}",
                    ordinal * 64,
                    ordinal * 64,
                    created_at,
                ),
            )
            self.store.connection.execute(
                """
                INSERT INTO web_review_items(
                    organization_id, product_id, review_batch_id, item_order,
                    review_id, family_id, publication_candidate_id,
                    repository_id, revision_id, revision, content_digest,
                    action, effective_content_json, effective_content_digest,
                    note
                ) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, 1, ?, ?, NULL, NULL, NULL)
                """,
                (
                    "org_demo",
                    PRODUCT_ID,
                    batch_id,
                    "rvi_" + ordinal * 32,
                    "cfm_" + "1" * 32,
                    "cand_" + "1" * 32 + "_01",
                    PRODUCT_REPOSITORY_ID,
                    "crv_" + "1" * 32,
                    ordinal * 64,
                    action,
                ),
            )


if __name__ == "__main__":
    unittest.main()
