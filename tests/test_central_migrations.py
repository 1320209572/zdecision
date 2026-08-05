from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from zdecision.capture.models import CandidateContent
from zdecision.central.migrations import migrate_legacy_repository_candidates
from zdecision.central.store import CentralStore
from zdecision.central.auth import Principal
from zdecision.central.decision_spaces import (
    EnabledRepository,
    LeafDecisionSpace,
    RepositoryDecisionRoute,
)
from zdecision.central.service import CaptureRequestService
from zdecision.ids import (
    candidate_family_id,
    candidate_revision_id,
    decision_space_id,
    product_id,
    repository_route_id,
)
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import (
    CandidateOwnershipSnapshot,
    CandidateRevisionUpload,
    RepositoryView,
)


NOW = datetime(2026, 8, 5, 1, 0, tzinfo=UTC)
REPOSITORY_ID = "repo_" + "1" * 32


class CentralMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = CentralStore.open(
            Path(self.temporary_directory.name) / "central.sqlite3"
        )
        self.store.put_repository_mapping(
            "org_demo",
            RepositoryView(
                repository_id=REPOSITORY_ID,
                product_id="prod_" + "9" * 32,
                product_name="Generic repository",
                enabled=True,
            ),
        )
        self.family_ids: list[str] = []
        for index in (1, 2):
            self.insert_legacy_family(index)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def insert_legacy_family(self, index: int) -> None:
        content = CandidateContent(
            product="Generic repository",
            claim=f"Legacy candidate {index}",
            future_action="Recapture it with trusted routing.",
            scope_summary="Ambiguous monorepo ownership",
            repositories=(REPOSITORY_ID,),
            paths=(f"packages/unknown-{index}",),
            invalidation_conditions=("Routing becomes explicit.",),
        )
        digest = hashlib.sha256(canonical_json_bytes(content.to_dict())).hexdigest()
        family_id = candidate_family_id(
            REPOSITORY_ID, f"cand_{index:032x}_01"
        )
        self.family_ids.append(family_id)
        item = CandidateRevisionUpload(
            family_id=family_id,
            revision_id=candidate_revision_id(family_id, 1, digest),
            revision=1,
            content=content,
            content_digest=digest,
            evidence_digest=str(index) * 64,
        )
        record = canonical_json_bytes(item.to_dict()).decode("utf-8")
        record_digest = hashlib.sha256(record.encode("utf-8")).hexdigest()
        with self.store.connection:
            self.store.connection.execute(
                """INSERT INTO candidate_revisions(
                organization_id, repository_id, family_id, revision,
                revision_id, record_json, record_digest
                ) VALUES ('org_demo', ?, ?, 1, ?, ?, ?)""",
                (REPOSITORY_ID, family_id, item.revision_id, record, record_digest),
            )
            self.store.connection.execute(
                """INSERT INTO candidate_family_heads(
                organization_id, repository_id, family_id, revision, revision_id
                ) VALUES ('org_demo', ?, ?, 1, ?)""",
                (REPOSITORY_ID, family_id, item.revision_id),
            )

    def configure_trusted_root(self) -> RepositoryDecisionRoute:
        compatibility_id = product_id("Standalone legacy")
        space = LeafDecisionSpace(
            decision_space_id=decision_space_id("product", compatibility_id),
            kind="product",
            display_name="Standalone legacy",
            compatibility_product_id=compatibility_id,
            compatibility_product_name="Standalone legacy",
            catalog_group_id=None,
            catalog_breadcrumb=(),
            source_root=".",
            package_name=None,
            asset_type=None,
            enabled=True,
        )
        self.store.put_repository(
            "org_demo", EnabledRepository(REPOSITORY_ID, True)
        )
        self.store.put_decision_space("org_demo", space)
        route = RepositoryDecisionRoute(
            route_id=repository_route_id(
                REPOSITORY_ID, space.decision_space_id
            ),
            repository_id=REPOSITORY_ID,
            decision_space_id=space.decision_space_id,
            path_prefixes=(".",),
            excluded_prefixes=(),
            enabled=True,
            configuration_version=1,
        )
        self.store.replace_trusted_route_heads(
            "org_demo", REPOSITORY_ID, (route,)
        )
        return route

    def test_generic_monorepo_candidates_are_archived_not_guessed(self) -> None:
        report = migrate_legacy_repository_candidates(
            self.store.connection,
            "org_demo",
            REPOSITORY_ID,
            policy="archive_and_recapture",
            root_route=None,
            archived_at=NOW,
        )

        self.assertEqual(2, report.archived_family_count)
        self.assertEqual(0, report.backfilled_family_count)
        self.assertEqual(
            2,
            self.store.connection.execute(
                "SELECT COUNT(*) FROM candidate_family_archives"
            ).fetchone()[0],
        )
        replay = migrate_legacy_repository_candidates(
            self.store.connection,
            "org_demo",
            REPOSITORY_ID,
            policy="archive_and_recapture",
            root_route=None,
            archived_at=NOW,
        )
        self.assertEqual(report, replay)
        self.assertEqual(
            (),
            CaptureRequestService(self.store).list_current_candidates(
                Principal("user", "org_demo", "user_demo", None),
                REPOSITORY_ID,
            ),
        )
        self.assertEqual(
            2,
            self.store.connection.execute(
                "SELECT COUNT(*) FROM candidate_revisions"
            ).fetchone()[0],
        )

    def test_trusted_root_backfill_is_replay_stable(self) -> None:
        route = self.configure_trusted_root()

        first = migrate_legacy_repository_candidates(
            self.store.connection,
            "org_demo",
            REPOSITORY_ID,
            policy="trusted_root_backfill",
            root_route=route,
            archived_at=NOW,
        )
        replay = migrate_legacy_repository_candidates(
            self.store.connection,
            "org_demo",
            REPOSITORY_ID,
            policy="trusted_root_backfill",
            root_route=route,
            archived_at=NOW,
        )

        self.assertEqual(first, replay)
        self.assertEqual(2, first.backfilled_family_count)
        self.assertEqual(
            {route.decision_space_id},
            {
                row["decision_space_id"]
                for row in self.store.connection.execute(
                    "SELECT decision_space_id FROM candidate_family_heads"
                ).fetchall()
            },
        )
        self.assertEqual(
            2,
            self.store.connection.execute(
                "SELECT COUNT(*) FROM candidate_revision_ownership"
            ).fetchone()[0],
        )

    def test_trusted_root_backfill_rejects_conflicting_ownership_atomically(
        self,
    ) -> None:
        route = self.configure_trusted_root()
        conflicting = CandidateOwnershipSnapshot(
            repository_id=REPOSITORY_ID,
            route_id="drr_" + "f" * 32,
            route_configuration_version=1,
            decision_space_id="dsp_" + "f" * 32,
            decision_space_kind="product",
            display_name="Conflicting leaf",
            catalog_breadcrumb=(),
            source_root=".",
            compatibility_product_id="prod_" + "f" * 32,
            compatibility_product_name="Conflicting leaf",
            source_boundary_digest="f" * 64,
        )
        ownership_json = canonical_json_bytes(
            conflicting.to_dict()
        ).decode("utf-8")
        with self.store.connection:
            self.store.connection.execute(
                """INSERT INTO candidate_revision_ownership(
                organization_id, repository_id, family_id, revision,
                decision_space_id, route_id, route_configuration_version,
                ownership_json, ownership_digest
                ) VALUES ('org_demo', ?, ?, 1, ?, ?, 1, ?, ?)""",
                (
                    REPOSITORY_ID,
                    self.family_ids[0],
                    conflicting.decision_space_id,
                    conflicting.route_id,
                    ownership_json,
                    hashlib.sha256(ownership_json.encode("utf-8")).hexdigest(),
                ),
            )

        with self.assertRaisesRegex(
            ValueError, "legacy_candidate_ownership_conflict"
        ):
            migrate_legacy_repository_candidates(
                self.store.connection,
                "org_demo",
                REPOSITORY_ID,
                policy="trusted_root_backfill",
                root_route=route,
                archived_at=NOW,
            )

        self.assertEqual(
            {"legacy_unassigned"},
            {
                row["decision_space_id"]
                for row in self.store.connection.execute(
                    "SELECT decision_space_id FROM candidate_family_heads"
                ).fetchall()
            },
        )


if __name__ == "__main__":
    unittest.main()
