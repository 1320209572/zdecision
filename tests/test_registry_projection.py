from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zdecision.capture.models import CandidateContent, SourceCheckpoint
from zdecision.capture.reviews import ApprovalRef
from zdecision.central.registry_projection import (
    RegistryProjectionStore,
    RegistryProjectionSynchronizer,
)
from zdecision.central.store import CentralStore
from zdecision.ids import decision_id, product_id
from zdecision.registry.git import RegistryOutOfSync
from zdecision.registry.models import (
    DecisionHead,
    DecisionRevision,
    DecisionSeed,
    ProductMetadata,
    ProductRegistry,
)
from zdecision.registry.query import RegistryQueryUnavailable, RegistrySnapshot


PRODUCT_NAME = "ZDecision"
PRODUCT_ID = product_id(PRODUCT_NAME)
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
TREE_A = "1" * 40
TREE_B = "2" * 40
VERIFIED_AT = "2026-08-06T10:00:00Z"
DECISION_CANDIDATE_ID = "cand_" + "3" * 32 + "_01"
DECISION_ID = decision_id(DECISION_CANDIDATE_ID, PRODUCT_ID)


def _snapshot(commit_sha: str = COMMIT_A) -> RegistrySnapshot:
    return RegistrySnapshot(
        commit_sha=commit_sha,
        products={PRODUCT_ID: ProductMetadata(PRODUCT_ID, PRODUCT_NAME)},
        registries={PRODUCT_ID: ProductRegistry(PRODUCT_ID, {})},
        decisions={},
    )


def _decision() -> DecisionRevision:
    seed = DecisionSeed(
        candidate_id=DECISION_CANDIDATE_ID,
        decision_id=DECISION_ID,
        product_id=PRODUCT_ID,
        product_name=PRODUCT_NAME,
        content=CandidateContent(
            product=PRODUCT_NAME,
            claim="The projection remains atomic.",
            future_action="Verify complete tree replacement.",
            scope_summary="Registry projection tests",
            repositories=("https://example.invalid/zdecision.git",),
            paths=("src/zdecision/central",),
            invalidation_conditions=("The schema changes.",),
        ),
        source=SourceCheckpoint("thread-source", "turn-source"),
        review_approval=ApprovalRef(
            actor="user",
            thread_id="thread-review",
            turn_id="turn-review",
            recorded_at=VERIFIED_AT,
        ),
    )
    return DecisionRevision.from_seed(
        seed, "pub_" + "4" * 32
    )


def _snapshot_with_decision(
    commit_sha: str = COMMIT_A,
) -> RegistrySnapshot:
    revision = _decision()
    return RegistrySnapshot(
        commit_sha=commit_sha,
        products={PRODUCT_ID: ProductMetadata(PRODUCT_ID, PRODUCT_NAME)},
        registries={
            PRODUCT_ID: ProductRegistry(
                PRODUCT_ID,
                {
                    DECISION_ID: DecisionHead(
                        1, "active", f"decisions/{DECISION_ID}/r0001.json"
                    )
                },
            )
        },
        decisions={(PRODUCT_ID, DECISION_ID): revision},
    )


class _VerifiedGit:
    def __init__(self, tree_oid: str = TREE_A) -> None:
        self.tree_oid = tree_oid
        self.fetch_count = 0

    def require_exact_main(self, expected_commit: str) -> str:
        return expected_commit

    def registry_tree_oid(self, commit_sha: str) -> str:
        return self.tree_oid


class _CommitQuery:
    def __init__(self, snapshot: RegistrySnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[str] = []

    def snapshot_at_commit(self, commit_sha: str) -> RegistrySnapshot:
        self.calls.append(commit_sha)
        return self.snapshot


class RegistryProjectionStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.central = CentralStore.open(
            Path(self.temporary_directory.name) / "central.sqlite3"
        )
        self.addCleanup(self.central.close)
        self.projection = RegistryProjectionStore(self.central.connection)

    def test_schema_and_install_round_trip_are_commit_and_tree_bound(self) -> None:
        tables = {
            row["name"]
            for row in self.central.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue(
            {
                "registry_projection_state",
                "registry_product_projection",
                "registry_decision_projection",
            }.issubset(tables)
        )
        product_columns = self.central.connection.execute(
            "PRAGMA table_info(registry_product_projection)"
        ).fetchall()
        self.assertEqual(
            ["organization_id", "registry_tree_oid", "product_id"],
            [row["name"] for row in product_columns if row["pk"]],
        )
        decision_columns = self.central.connection.execute(
            "PRAGMA table_info(registry_decision_projection)"
        ).fetchall()
        self.assertEqual(
            [
                "organization_id",
                "registry_tree_oid",
                "product_id",
                "decision_id",
                "revision",
            ],
            [row["name"] for row in decision_columns if row["pk"]],
        )
        state_columns = self.central.connection.execute(
            "PRAGMA table_info(registry_projection_state)"
        ).fetchall()
        self.assertEqual(
            ["organization_id"],
            [row["name"] for row in state_columns if row["pk"]],
        )
        foreign_keys = self.central.connection.execute(
            "PRAGMA foreign_key_list(registry_decision_projection)"
        ).fetchall()
        self.assertEqual(
            "registry_product_projection", foreign_keys[0]["table"]
        )
        state_sql = self.central.connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'registry_projection_state'"
        ).fetchone()["sql"]
        self.assertIn(
            "CHECK(state IN ('available','syncing','unavailable'))", state_sql
        )
        indexes = {
            row["name"]
            for row in self.central.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        self.assertTrue(
            {
                "registry_product_projection_name",
                "registry_decision_projection_lifecycle",
                "registry_decision_projection_identity",
            }.issubset(indexes)
        )

        self.projection.mark_syncing(
            "org_demo", COMMIT_A, TREE_A, VERIFIED_AT, VERIFIED_AT
        )
        state = self.projection.install(
            "org_demo", TREE_A, _snapshot(), VERIFIED_AT, VERIFIED_AT
        )
        active = self.projection.load_active("org_demo")

        self.assertEqual("available", state.state)
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(COMMIT_A, active.commit_sha)
        self.assertEqual(TREE_A, active.tree_oid)
        self.assertEqual(VERIFIED_AT, active.verified_at)
        self.assertEqual(_snapshot(), active.snapshot)

    def test_derived_product_corruption_fails_closed(self) -> None:
        self.projection.mark_syncing(
            "org_demo", COMMIT_A, TREE_A, VERIFIED_AT, VERIFIED_AT
        )
        self.projection.install(
            "org_demo", TREE_A, _snapshot(), VERIFIED_AT, VERIFIED_AT
        )
        self.central.connection.execute(
            """UPDATE registry_product_projection SET product_json = ?
               WHERE organization_id = ?""",
            ('{"name":"ZDecision"}\n', "org_demo"),
        )

        self.assertIsNone(self.projection.load_active("org_demo"))

    def test_derived_decision_digest_corruption_fails_closed(self) -> None:
        snapshot = _snapshot_with_decision()
        self.projection.mark_syncing(
            "org_demo", COMMIT_A, TREE_A, VERIFIED_AT, VERIFIED_AT
        )
        self.projection.install(
            "org_demo", TREE_A, snapshot, VERIFIED_AT, VERIFIED_AT
        )
        self.central.connection.execute(
            """UPDATE registry_decision_projection SET decision_digest = ?
               WHERE organization_id = ?""",
            ("0" * 64, "org_demo"),
        )

        self.assertIsNone(self.projection.load_active("org_demo"))

    def test_same_tree_updates_only_commit_provenance(self) -> None:
        self.projection.mark_syncing(
            "org_demo", COMMIT_A, TREE_A, VERIFIED_AT, VERIFIED_AT
        )
        self.projection.install(
            "org_demo", TREE_A, _snapshot(), VERIFIED_AT, VERIFIED_AT
        )
        before = self.central.connection.execute(
            "SELECT rowid, * FROM registry_product_projection"
        ).fetchall()

        self.projection.mark_syncing(
            "org_demo", COMMIT_B, TREE_A,
            "2026-08-06T11:00:00Z", "2026-08-06T11:00:00Z",
        )
        state = self.projection.update_provenance(
            "org_demo", COMMIT_B, TREE_A,
            "2026-08-06T11:00:00Z", "2026-08-06T11:00:00Z",
        )
        after = self.central.connection.execute(
            "SELECT rowid, * FROM registry_product_projection"
        ).fetchall()

        self.assertEqual(COMMIT_B, state.active_commit)
        self.assertEqual(before, after)

    def test_failed_install_never_switches_or_partially_replaces_active_tree(
        self,
    ) -> None:
        self.projection.mark_syncing(
            "org_demo", COMMIT_A, TREE_A, VERIFIED_AT, VERIFIED_AT
        )
        self.projection.install(
            "org_demo", TREE_A, _snapshot(), VERIFIED_AT, VERIFIED_AT
        )
        self.projection.mark_syncing(
            "org_demo", COMMIT_B, TREE_B, VERIFIED_AT, VERIFIED_AT
        )
        self.central.connection.execute(
            f"""CREATE TRIGGER reject_tree_b BEFORE INSERT
                ON registry_product_projection
                WHEN NEW.registry_tree_oid = '{TREE_B}'
                BEGIN SELECT RAISE(ABORT, 'fixture rejection'); END"""
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.projection.install(
                "org_demo", TREE_B, _snapshot(COMMIT_B),
                VERIFIED_AT, VERIFIED_AT,
            )

        rows = self.central.connection.execute(
            "SELECT DISTINCT registry_tree_oid FROM registry_product_projection"
        ).fetchall()
        self.assertEqual([TREE_A], [row[0] for row in rows])
        self.assertIsNone(self.projection.load_active("org_demo"))

    def test_exact_replay_has_no_duplicate_rows_and_missing_rows_do_not_match(
        self,
    ) -> None:
        snapshot = _snapshot_with_decision()
        self.projection.mark_syncing(
            "org_demo", COMMIT_A, TREE_A, VERIFIED_AT, VERIFIED_AT
        )
        self.projection.install(
            "org_demo", TREE_A, snapshot, VERIFIED_AT, VERIFIED_AT
        )
        self.projection.mark_syncing(
            "org_demo", COMMIT_A, TREE_A, VERIFIED_AT, VERIFIED_AT
        )
        self.projection.install(
            "org_demo", TREE_A, snapshot, VERIFIED_AT, VERIFIED_AT
        )
        self.assertEqual(
            1,
            self.central.connection.execute(
                "SELECT COUNT(*) FROM registry_product_projection"
            ).fetchone()[0],
        )
        self.assertEqual(
            1,
            self.central.connection.execute(
                "SELECT COUNT(*) FROM registry_decision_projection"
            ).fetchone()[0],
        )
        self.assertTrue(self.projection.matches("org_demo", TREE_A, snapshot))
        self.central.connection.execute(
            "DELETE FROM registry_decision_projection WHERE organization_id = ?",
            ("org_demo",),
        )
        self.assertFalse(self.projection.matches("org_demo", TREE_A, snapshot))
        self.assertIsNone(self.projection.load_active("org_demo"))

    def test_available_empty_snapshot_is_not_treated_as_missing_rows(self) -> None:
        empty = RegistrySnapshot(COMMIT_A, {}, {}, {})
        self.projection.mark_syncing(
            "org_demo", COMMIT_A, TREE_A, VERIFIED_AT, VERIFIED_AT
        )
        self.projection.install(
            "org_demo", TREE_A, empty, VERIFIED_AT, VERIFIED_AT
        )

        active = self.projection.load_active("org_demo")

        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(empty, active.snapshot)
        state = self.projection.get_state("org_demo")
        assert state is not None
        self.assertEqual(0, state.product_count)
        self.assertEqual(0, state.decision_count)

    def test_synchronize_installs_then_reuses_same_tree_without_rewriting_rows(
        self,
    ) -> None:
        git = _VerifiedGit()
        query = _CommitQuery(_snapshot())
        synchronizer = RegistryProjectionSynchronizer(
            git=git, query=query, store=self.projection,
            clock=lambda: "2026-08-06T10:00:01Z",
        )

        first = synchronizer.synchronize("org_demo", COMMIT_A, VERIFIED_AT)
        before = self.central.connection.execute(
            "SELECT rowid, * FROM registry_product_projection"
        ).fetchall()
        query.snapshot = _snapshot(COMMIT_B)
        second = synchronizer.synchronize(
            "org_demo", COMMIT_B, "2026-08-06T11:00:00Z"
        )
        after = self.central.connection.execute(
            "SELECT rowid, * FROM registry_product_projection"
        ).fetchall()

        self.assertEqual("available", first.state)
        self.assertEqual(COMMIT_B, second.active_commit)
        self.assertEqual(before, after)
        self.assertEqual([COMMIT_A, COMMIT_B], query.calls)

    def test_synchronize_rebuilds_same_tree_when_product_row_is_corrupt(
        self,
    ) -> None:
        git = _VerifiedGit()
        query = _CommitQuery(_snapshot())
        synchronizer = RegistryProjectionSynchronizer(
            git=git, query=query, store=self.projection,
            clock=lambda: VERIFIED_AT,
        )
        synchronizer.synchronize("org_demo", COMMIT_A, VERIFIED_AT)
        self.central.connection.execute(
            """UPDATE registry_product_projection SET product_name = ?
               WHERE organization_id = ? AND registry_tree_oid = ?""",
            ("corrupt product name", "org_demo", TREE_A),
        )
        query.snapshot = _snapshot(COMMIT_B)

        state = synchronizer.synchronize("org_demo", COMMIT_B, VERIFIED_AT)
        active = self.projection.load_active("org_demo")

        self.assertEqual("available", state.state)
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(COMMIT_B, active.commit_sha)
        self.assertEqual(PRODUCT_NAME, active.snapshot.products[PRODUCT_ID].name)

    def test_synchronize_rebuilds_same_tree_when_state_manifest_is_corrupt(
        self,
    ) -> None:
        query = _CommitQuery(_snapshot())
        synchronizer = RegistryProjectionSynchronizer(
            git=_VerifiedGit(), query=query, store=self.projection,
            clock=lambda: VERIFIED_AT,
        )

        for field, corrupted_value in (
            ("product_count", 2),
            ("decision_count", 1),
            ("projection_digest", "0" * 64),
        ):
            with self.subTest(field=field):
                query.snapshot = _snapshot(COMMIT_A)
                expected = synchronizer.synchronize(
                    "org_demo", COMMIT_A, VERIFIED_AT
                )
                self.central.connection.execute(
                    f"""UPDATE registry_projection_state SET {field} = ?
                       WHERE organization_id = ?""",
                    (corrupted_value, "org_demo"),
                )

                self.assertFalse(
                    self.projection.matches("org_demo", TREE_A, query.snapshot)
                )
                query.snapshot = _snapshot(COMMIT_B)
                state = synchronizer.synchronize(
                    "org_demo", COMMIT_B, VERIFIED_AT
                )
                active = self.projection.load_active("org_demo")

                self.assertEqual(expected.product_count, state.product_count)
                self.assertEqual(expected.decision_count, state.decision_count)
                self.assertEqual(expected.projection_digest, state.projection_digest)
                self.assertIsNotNone(active)
                assert active is not None
                self.assertEqual(COMMIT_B, active.commit_sha)
                self.assertEqual(query.snapshot, active.snapshot)

    def test_parse_failure_marks_projection_unavailable_without_serving_old_rows(
        self,
    ) -> None:
        synchronizer = RegistryProjectionSynchronizer(
            git=_VerifiedGit(), query=_CommitQuery(_snapshot()),
            store=self.projection, clock=lambda: VERIFIED_AT,
        )
        synchronizer.synchronize("org_demo", COMMIT_A, VERIFIED_AT)
        synchronizer.query.snapshot_at_commit = mock.Mock(
            side_effect=RegistryQueryUnavailable("registry_unavailable")
        )

        state = synchronizer.synchronize("org_demo", COMMIT_A, VERIFIED_AT)

        self.assertEqual("unavailable", state.state)
        self.assertEqual("registry_invalid", state.error_code)
        self.assertIsNone(self.projection.load_active("org_demo"))

    def test_install_failure_keeps_old_tree_rows_but_serves_no_active_snapshot(
        self,
    ) -> None:
        git = _VerifiedGit()
        query = _CommitQuery(_snapshot())
        synchronizer = RegistryProjectionSynchronizer(
            git=git, query=query, store=self.projection,
            clock=lambda: VERIFIED_AT,
        )
        synchronizer.synchronize("org_demo", COMMIT_A, VERIFIED_AT)
        git.tree_oid = TREE_B
        query.snapshot = _snapshot(COMMIT_B)
        self.central.connection.execute(
            f"""CREATE TRIGGER reject_tree_b BEFORE INSERT
                ON registry_product_projection
                WHEN NEW.registry_tree_oid = '{TREE_B}'
                BEGIN SELECT RAISE(ABORT, 'fixture rejection'); END"""
        )

        state = synchronizer.synchronize("org_demo", COMMIT_B, VERIFIED_AT)

        rows = self.central.connection.execute(
            "SELECT DISTINCT registry_tree_oid FROM registry_product_projection"
        ).fetchall()
        self.assertEqual("unavailable", state.state)
        self.assertEqual("projection_install_failed", state.error_code)
        self.assertEqual([TREE_A], [row[0] for row in rows])
        self.assertIsNone(self.projection.load_active("org_demo"))

    def test_mismatched_exact_main_marks_git_proof_failed(self) -> None:
        git = _VerifiedGit()
        git.require_exact_main = mock.Mock(
            side_effect=RegistryOutOfSync("mismatched exact main")
        )
        query = _CommitQuery(_snapshot())
        synchronizer = RegistryProjectionSynchronizer(
            git=git, query=query, store=self.projection,
            clock=lambda: VERIFIED_AT,
        )

        state = synchronizer.synchronize("org_demo", COMMIT_A, VERIFIED_AT)

        self.assertEqual("unavailable", state.state)
        self.assertEqual("git_proof_failed", state.error_code)
        self.assertEqual([], query.calls)
        self.assertIsNone(self.projection.load_active("org_demo"))
