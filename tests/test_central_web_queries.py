from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from zdecision.central.auth import Principal
from zdecision.central.decision_spaces import LeafDecisionSpace
from zdecision.central.store import CentralStore
from zdecision.central.web.contracts import CentralPublication
from zdecision.central.web.queries import CentralWebQueries, DecisionNotFound
from zdecision.capture.models import CandidateContent, SourceCheckpoint
from zdecision.capture.reviews import ApprovalRef
from zdecision.ids import (
    central_publication_id,
    decision_id,
    decision_space_id,
    product_id,
)
from zdecision.registry.models import (
    DecisionHead,
    DecisionRevision,
    DecisionSeed,
    ProductMetadata,
    ProductRegistry,
    RootProductEntry,
    RootRegistry,
)
from zdecision.registry.git import GitRegistryAdapter
from zdecision.jsonio import atomic_write_json, canonical_json_bytes
from zdecision.registry.query import (
    RegistryQuery,
    RegistryQueryUnavailable,
    RegistrySnapshot,
)
from zdecision.sync.contracts import RepositoryView


PRODUCT_NAME = "ZDecision"
PRODUCT_ID = product_id(PRODUCT_NAME)
PRODUCT_SPACE_ID = decision_space_id("product", PRODUCT_ID)
PRODUCT_REPOSITORY_ID = "repo_" + "1" * 32
OTHER_PRODUCT_NAME = "Other Product"
OTHER_PRODUCT_ID = product_id(OTHER_PRODUCT_NAME)
OTHER_REPOSITORY_ID = "repo_" + "2" * 32
COMMIT_SHA = "a" * 40
PUBLICATION_COMMIT = "b" * 40


def _decision(
    *,
    product_name: str = PRODUCT_NAME,
    candidate_ordinal: str = "3",
    claim: str = "隔离正式决策读取",
    repository: str = "zdecision",
) -> DecisionRevision:
    owned_product_id = product_id(product_name)
    candidate_id = "cand_" + candidate_ordinal * 32 + "_01"
    seed = DecisionSeed(
        candidate_id=candidate_id,
        decision_id=decision_id(candidate_id, owned_product_id),
        product_id=owned_product_id,
        product_name=product_name,
        content=CandidateContent(
            product=product_name,
            claim=claim,
            future_action="Keep the catalog commit-bound.",
            scope_summary="Registry isolation",
            repositories=(repository,),
            paths=("decision-registry/",),
            invalidation_conditions=("Registry ownership changes.",),
        ),
        source=SourceCheckpoint("opaque-thread", "opaque-turn"),
        review_approval=ApprovalRef(
            "user",
            "approval-thread",
            "approval-turn",
            "2026-08-03T10:00:00Z",
        ),
    )
    return DecisionRevision.from_seed(seed, "pub_" + candidate_ordinal * 32)


class _RegistryQuery:
    def __init__(
        self,
        *,
        unavailable: bool = False,
        decisions: tuple[DecisionRevision, ...] = (),
    ) -> None:
        self.unavailable = unavailable
        self.decisions = decisions
        self.snapshot_count = 0

    def snapshot(self) -> RegistrySnapshot:
        self.snapshot_count += 1
        if self.unavailable:
            raise RegistryQueryUnavailable("registry_unavailable")
        product_decisions: dict[str, dict[str, DecisionHead]] = {
            PRODUCT_ID: {},
            OTHER_PRODUCT_ID: {},
        }
        indexed: dict[tuple[str, str], DecisionRevision] = {}
        for revision in self.decisions:
            indexed[(revision.product_id, revision.decision_id)] = revision
            product_decisions[revision.product_id][revision.decision_id] = (
                DecisionHead(
                    revision.revision,
                    revision.lifecycle,
                    f"decisions/{revision.decision_id}/r0001.json",
                )
            )
        return RegistrySnapshot(
            commit_sha=COMMIT_SHA,
            products={
                PRODUCT_ID: ProductMetadata(PRODUCT_ID, PRODUCT_NAME),
                OTHER_PRODUCT_ID: ProductMetadata(
                    OTHER_PRODUCT_ID, OTHER_PRODUCT_NAME
                ),
            },
            registries={
                PRODUCT_ID: ProductRegistry(
                    PRODUCT_ID, product_decisions[PRODUCT_ID]
                ),
                OTHER_PRODUCT_ID: ProductRegistry(
                    OTHER_PRODUCT_ID, product_decisions[OTHER_PRODUCT_ID]
                ),
            },
            decisions=indexed,
        )


class RegistryQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.remote = root / "remote.git"
        self.repository = root / "repository"
        self._git("init", "--bare", str(self.remote), repository=root)
        self._git("init", "-b", "main", str(self.repository), repository=root)
        self._git("config", "user.email", "tests@example.com")
        self._git("config", "user.name", "ZDecision Tests")
        registry = self.repository / "decision-registry"
        registry.mkdir()
        candidate_id = "cand_" + "1" * 32 + "_01"
        self.formal_decision_id = decision_id(candidate_id, PRODUCT_ID)
        seed = DecisionSeed(
            candidate_id=candidate_id,
            decision_id=self.formal_decision_id,
            product_id=PRODUCT_ID,
            product_name=PRODUCT_NAME,
            content=CandidateContent(
                product=PRODUCT_NAME,
                claim="committed formal decision",
                future_action="Read only the committed Registry bytes.",
                scope_summary="Commit-bound Registry query",
                repositories=("zdecision",),
                paths=("decision-registry/",),
                invalidation_conditions=("The formal Decision changes",),
            ),
            source=SourceCheckpoint("thread-source", "turn-source"),
            review_approval=ApprovalRef(
                "user",
                "thread-review",
                "turn-review",
                "2026-08-04T00:00:00Z",
            ),
        )
        self.revision = DecisionRevision.from_seed(
            seed, "pub_" + "2" * 32
        )
        product_root = registry / "products" / PRODUCT_ID
        self.decision_path = (
            product_root
            / "decisions"
            / self.formal_decision_id
            / "r0001.json"
        )
        self.decision_path.parent.mkdir(parents=True)
        atomic_write_json(
            registry / "registry.json",
            RootRegistry(
                {
                    PRODUCT_ID: RootProductEntry(
                        PRODUCT_NAME,
                        f"products/{PRODUCT_ID}/product.json",
                        f"products/{PRODUCT_ID}/registry.json",
                    )
                }
            ).to_dict(),
        )
        atomic_write_json(
            product_root / "product.json",
            ProductMetadata(PRODUCT_ID, PRODUCT_NAME).to_dict(),
        )
        atomic_write_json(
            product_root / "registry.json",
            ProductRegistry(
                PRODUCT_ID,
                {
                    self.formal_decision_id: DecisionHead(
                        1,
                        "active",
                        (
                            f"decisions/{self.formal_decision_id}/"
                            "r0001.json"
                        ),
                    )
                },
            ).to_dict(),
        )
        atomic_write_json(self.decision_path, self.revision.to_dict())
        self._git("add", "decision-registry")
        self._git("commit", "-m", "registry")
        self._git(
            "remote", "add", "origin", str(self.remote.resolve())
        )
        self._git("push", "-u", "origin", "main")
        self.query = RegistryQuery(
            self.repository,
            GitRegistryAdapter(
                self.repository, expected_origin=str(self.remote.resolve())
            ),
        )

    def _git(
        self,
        *arguments: str,
        repository: Path | None = None,
        input_bytes: bytes | None = None,
    ) -> bytes:
        result = subprocess.run(
            ("git", "-C", str(repository or self.repository), *arguments),
            input=input_bytes,
            check=True,
            capture_output=True,
        )
        return result.stdout

    def _assert_committed_claim(self) -> None:
        snapshot = self.query.snapshot()
        self.assertEqual(
            "committed formal decision",
            snapshot.decisions[
                (PRODUCT_ID, self.formal_decision_id)
            ].claim,
        )

    def test_snapshot_reads_commit_when_index_assumes_worktree_is_unchanged(
        self,
    ) -> None:
        self._git(
            "update-index",
            "--assume-unchanged",
            str(self.decision_path.relative_to(self.repository)),
        )
        changed = self.revision.to_dict()
        changed["claim"] = "uncommitted canonical decision"
        atomic_write_json(self.decision_path, changed)

        self._assert_committed_claim()

    def test_snapshot_ignores_local_replacement_for_committed_blob(self) -> None:
        relative_path = str(self.decision_path.relative_to(self.repository))
        original_blob = self._git(
            "rev-parse", f"HEAD:{relative_path}"
        ).decode("ascii").strip()
        changed = self.revision.to_dict()
        changed["claim"] = "replacement canonical decision"
        replacement_blob = self._git(
            "hash-object",
            "-w",
            "--stdin",
            input_bytes=canonical_json_bytes(changed),
        ).decode("ascii").strip()
        self._git("replace", original_blob, replacement_blob)

        self._assert_committed_claim()

    def test_snapshot_selects_active_revisions_by_product_ownership(self) -> None:
        snapshot = self.query.snapshot()

        self.assertEqual(
            (self.revision,), snapshot.active_decisions(PRODUCT_ID)
        )
        self.assertEqual((), snapshot.active_decisions(OTHER_PRODUCT_ID))


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

    def test_global_and_product_catalog_return_same_owned_revision(self) -> None:
        revision = _decision()
        registry = _RegistryQuery(decisions=(revision,))
        queries = CentralWebQueries(self.store.connection, registry)

        global_items = queries.list_decisions(
            self.user, product_id=None, search="隔离"
        )
        product_items = queries.list_decisions(
            self.user, product_id=PRODUCT_ID, search="隔离"
        )

        self.assertEqual(global_items.items, product_items.items)
        self.assertEqual(PRODUCT_ID, global_items.items[0].product_id)
        self.assertEqual(2, registry.snapshot_count)

    def test_product_detail_rejects_decision_owned_by_another_product(self) -> None:
        revision = _decision()
        self.store.put_repository_mapping(
            "org_demo",
            RepositoryView(
                OTHER_REPOSITORY_ID,
                OTHER_PRODUCT_ID,
                OTHER_PRODUCT_NAME,
                True,
            ),
        )
        queries = CentralWebQueries(
            self.store.connection, _RegistryQuery(decisions=(revision,))
        )

        with self.assertRaises(DecisionNotFound) as raised:
            queries.get_decision(
                self.user, OTHER_PRODUCT_ID, revision.decision_id
            )
        self.assertEqual("not_found", getattr(raised.exception, "code", None))

    def test_invalid_registry_is_unavailable_not_empty(self) -> None:
        result = CentralWebQueries(
            self.store.connection, _RegistryQuery(unavailable=True)
        ).list_decisions(self.user)

        self.assertEqual("unavailable", result.registry_state)
        self.assertIsNone(result.items)
        self.assertIsNone(result.total)

    def test_catalog_filters_are_bounded_and_join_completed_publication(self) -> None:
        revision = _decision(repository="ZDecision")
        publication_id = central_publication_id(
            revision.publication_preview_id
        )
        publication = CentralPublication(
            publication_id=publication_id,
            organization_id="org_demo",
            actor_id="user_demo",
            decision_space_id=PRODUCT_SPACE_ID,
            compatibility_product_id=PRODUCT_ID,
            preview_id=revision.publication_preview_id,
            confirm_action_id="web_action_catalog-fixture",
            confirm_request_digest="d" * 64,
            state="completed",
            approval=ApprovalRef(
                "user",
                "publish-thread",
                "publish-turn",
                "2026-08-03T11:00:00Z",
            ),
            commit_sha=PUBLICATION_COMMIT,
            recovery_code=None,
            created_at="2026-08-03T11:00:00Z",
            updated_at="2026-08-04T12:30:00Z",
        )
        encoded = canonical_json_bytes(publication.to_dict())
        self.store.connection.execute("PRAGMA foreign_keys = OFF")
        with self.store.connection:
            self.store.connection.execute(
                """
                INSERT INTO web_publications(
                    organization_id, decision_space_id,
                    compatibility_product_id, publication_id, preview_id,
                    actor_id, state, recovery_code, commit_sha, record_json,
                    record_digest, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    publication.organization_id,
                    publication.decision_space_id,
                    publication.compatibility_product_id,
                    publication.publication_id,
                    publication.preview_id,
                    publication.actor_id,
                    publication.state,
                    publication.recovery_code,
                    publication.commit_sha,
                    encoded.decode("utf-8"),
                    hashlib.sha256(encoded).hexdigest(),
                    publication.created_at,
                    publication.updated_at,
                ),
            )
            self.store.connection.execute(
                """
                INSERT INTO web_candidate_receipts(
                    organization_id, decision_space_id,
                    compatibility_product_id, family_id,
                    publication_candidate_id, decision_id, preview_id,
                    commit_sha, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "org_demo",
                    PRODUCT_SPACE_ID,
                    PRODUCT_ID,
                    "cfm_" + "3" * 32,
                    "cand_" + "3" * 32 + "_01",
                    revision.decision_id,
                    revision.publication_preview_id,
                    PUBLICATION_COMMIT,
                    publication.created_at,
                ),
            )
        self.store.connection.execute("PRAGMA foreign_keys = ON")
        registry = _RegistryQuery(decisions=(revision,))
        queries = CentralWebQueries(self.store.connection, registry)

        result = queries.list_decisions(
            self.user,
            repository="ZDecision",
            published_after="2026-08-04T00:00:00Z",
        )
        detail = queries.get_decision(
            self.user, PRODUCT_ID, revision.decision_id
        )

        self.assertEqual(1, result.total)
        self.assertEqual(publication_id, result.items[0].publication_id)
        self.assertEqual("2026-08-04T12:30:00Z", result.items[0].published_at)
        self.assertEqual(PUBLICATION_COMMIT, result.items[0].commit_sha)
        self.assertEqual(publication_id, detail.publication_id)
        self.assertEqual(
            canonical_json_bytes(revision.to_dict()).decode("utf-8"),
            detail.to_dict()["canonical_json"],
        )
        self.assertEqual(2, registry.snapshot_count)

        invalid_arguments = (
            {"search": "界" * 67},
            {"repository": "r" * 201},
            {"published_after": "yesterday"},
            {"limit": 0},
            {"limit": 101},
            {"offset": -1},
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                queries.list_decisions(self.user, **arguments)

    def test_publication_metadata_requires_the_revision_preview_id(self) -> None:
        revision = _decision()
        mismatched_preview = "pub_" + "8" * 32
        self._insert_completed_publication_metadata(
            revision.decision_id, mismatched_preview, "8"
        )
        queries = CentralWebQueries(
            self.store.connection, _RegistryQuery(decisions=(revision,))
        )

        catalog = queries.list_decisions(self.user)
        filtered = queries.list_decisions(
            self.user, published_after="2026-08-04T00:00:00Z"
        )
        detail = queries.get_decision(
            self.user, PRODUCT_ID, revision.decision_id
        )

        self.assertEqual(1, catalog.total)
        self.assertIsNone(catalog.items[0].publication_id)
        self.assertIsNone(catalog.items[0].published_at)
        self.assertIsNone(catalog.items[0].commit_sha)
        self.assertEqual(0, filtered.total)
        self.assertIsNone(detail.publication_id)
        self.assertIsNone(detail.published_at)
        self.assertIsNone(detail.commit_sha)

    def test_dashboard_excludes_registry_name_mismatch_from_active_counts(
        self,
    ) -> None:
        revision = _decision()
        self.store.put_repository_mapping(
            "org_demo",
            RepositoryView(
                PRODUCT_REPOSITORY_ID,
                PRODUCT_ID,
                "Mismatched Product Name",
                True,
            ),
        )
        self.store.put_decision_space(
            "org_demo",
            LeafDecisionSpace(
                PRODUCT_SPACE_ID,
                "product",
                "Mismatched Product Name",
                PRODUCT_ID,
                "Mismatched Product Name",
                None,
                (),
                ".",
                None,
                None,
                True,
            ),
        )
        queries = CentralWebQueries(
            self.store.connection, _RegistryQuery(decisions=(revision,))
        )

        dashboard = queries.dashboard(self.user)

        self.assertEqual(0, dashboard.products[0].active_decision_count)
        self.assertEqual(0, dashboard.metrics.active_decision_count)

    def test_dashboard_derives_products_and_counts_from_owned_sources(
        self,
    ) -> None:
        dashboard = self.queries.dashboard(self.user)

        self.assertEqual(
            [PRODUCT_SPACE_ID],
            [item.decision_space_id for item in dashboard.products],
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

    def test_same_timestamp_dashboard_uses_database_submission_order(
        self,
    ) -> None:
        self._insert_review(
            "1", "accept", "2026-08-04T01:00:00Z", submission_order=1
        )
        self._insert_review(
            "f", "skip", "2026-08-04T01:00:00Z", submission_order=2
        )

        self.assertEqual(
            1, self.queries.dashboard(self.user).metrics.pending_candidate_count
        )

    def _insert_review(
        self,
        ordinal: str,
        action: str,
        created_at: str,
        *,
        submission_order: int | None = None,
    ) -> None:
        batch_id = "rvb_" + ordinal * 32
        with self.store.connection:
            if submission_order is None:
                submission_order = self.store.connection.execute(
                    """
                    SELECT COALESCE(MAX(submission_order), 0) + 1
                    FROM web_review_batches
                    WHERE organization_id = ? AND decision_space_id = ?
                    """,
                    ("org_demo", PRODUCT_SPACE_ID),
                ).fetchone()[0]
            self.store.connection.execute(
                """
                INSERT INTO web_review_batches(
                    organization_id, decision_space_id,
                    compatibility_product_id, compatibility_product_name,
                    review_batch_id, actor_id,
                    client_action_id, request_digest, record_json,
                    record_digest, created_at, submission_order
                ) VALUES (?, ?, ?, ?, ?, 'user_demo', ?, ?, '{}', ?, ?, ?)
                """,
                (
                    "org_demo",
                    PRODUCT_SPACE_ID,
                    PRODUCT_ID,
                    PRODUCT_NAME,
                    batch_id,
                    f"web_action_review-{ordinal}",
                    ordinal * 64,
                    ordinal * 64,
                    created_at,
                    submission_order,
                ),
            )
            self.store.connection.execute(
                """
                INSERT INTO web_review_items(
                    organization_id, decision_space_id, review_batch_id, item_order,
                    review_id, family_id, publication_candidate_id,
                    repository_id, revision_id, revision, content_digest,
                    action, effective_content_json, effective_content_digest,
                    note
                ) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, 1, ?, ?, NULL, NULL, NULL)
                """,
                (
                    "org_demo",
                    PRODUCT_SPACE_ID,
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

    def _insert_completed_publication_metadata(
        self, decision: str, preview: str, ordinal: str
    ) -> None:
        publication = CentralPublication(
            publication_id=central_publication_id(preview),
            organization_id="org_demo",
            actor_id="user_demo",
            decision_space_id=PRODUCT_SPACE_ID,
            compatibility_product_id=PRODUCT_ID,
            preview_id=preview,
            confirm_action_id=f"web_action_catalog-{ordinal}",
            confirm_request_digest=ordinal * 64,
            state="completed",
            approval=ApprovalRef(
                "user",
                "publish-thread",
                f"publish-turn-{ordinal}",
                "2026-08-03T11:00:00Z",
            ),
            commit_sha=ordinal * 40,
            recovery_code=None,
            created_at="2026-08-03T11:00:00Z",
            updated_at="2026-08-04T12:30:00Z",
        )
        encoded = canonical_json_bytes(publication.to_dict())
        self.store.connection.execute("PRAGMA foreign_keys = OFF")
        with self.store.connection:
            self.store.connection.execute(
                """
                INSERT INTO web_publications(
                    organization_id, decision_space_id,
                    compatibility_product_id, publication_id, preview_id,
                    actor_id, state, recovery_code, commit_sha, record_json,
                    record_digest, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    publication.organization_id,
                    publication.decision_space_id,
                    publication.compatibility_product_id,
                    publication.publication_id,
                    publication.preview_id,
                    publication.actor_id,
                    publication.state,
                    publication.recovery_code,
                    publication.commit_sha,
                    encoded.decode("utf-8"),
                    hashlib.sha256(encoded).hexdigest(),
                    publication.created_at,
                    publication.updated_at,
                ),
            )
            self.store.connection.execute(
                """
                INSERT INTO web_candidate_receipts(
                    organization_id, decision_space_id,
                    compatibility_product_id, family_id,
                    publication_candidate_id, decision_id, preview_id,
                    commit_sha, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "org_demo",
                    PRODUCT_SPACE_ID,
                    PRODUCT_ID,
                    "cfm_" + ordinal * 32,
                    "cand_" + ordinal * 32 + "_01",
                    decision,
                    preview,
                    publication.commit_sha,
                    publication.created_at,
                ),
            )
        self.store.connection.execute("PRAGMA foreign_keys = ON")


if __name__ == "__main__":
    unittest.main()
