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
from zdecision.central.service import CaptureRequestService
from zdecision.ids import candidate_family_id, candidate_revision_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import CandidateRevisionUpload, RepositoryView


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


if __name__ == "__main__":
    unittest.main()
