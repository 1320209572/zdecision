from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from zdecision.capture.models import CandidateContent
from zdecision.capture.reviews import ApprovalRef
from zdecision.central.store import CentralStore
from zdecision.ids import candidate_revision_id, decision_space_id, product_id

from zdecision.central.web.contracts import (
    CentralPublication,
    CentralReviewBatch,
    CentralReviewItem,
    DraftItem,
    ReviewDraft,
    ReviewSubmissionSnapshot,
)
from zdecision.central.web.store import (
    CentralWebStore,
    DraftConflict,
    WebActionConflict,
    WebRecordConflict,
)
from zdecision.central.web.schema import _migrate_leaf_owned_web_tables
from zdecision.ids import (
    central_publication_id,
    central_review_batch_id,
    decision_id,
    publication_candidate_id,
    publication_preview_id,
    review_item_id,
)
from zdecision.jsonio import canonical_json_bytes
from zdecision.registry.publication import (
    PublicationFile,
    PublicationRecord,
    content_digest_for_files,
)


PRODUCT_ID = product_id("ZDecision")
DECISION_SPACE_ID = decision_space_id("product", PRODUCT_ID)
NOW = "2026-08-04T01:02:03Z"
FAMILY_ID = "cfm_" + "a" * 32
REPOSITORY_ID = "repo_" + "b" * 32
DIGEST = "c" * 64
REVISION_ID = candidate_revision_id(FAMILY_ID, 1, DIGEST)


def content() -> CandidateContent:
    return CandidateContent(
        product="ZDecision",
        claim="Central drafts survive a service restart.",
        future_action="Restore the reviewed candidate before submission.",
        scope_summary="Central web persistence",
        repositories=(REPOSITORY_ID,),
        paths=("src/zdecision/central/web",),
        invalidation_conditions=("The central storage contract changes.",),
    )


def draft_item() -> DraftItem:
    return DraftItem(
        family_id=FAMILY_ID,
        repository_id=REPOSITORY_ID,
        revision_id=REVISION_ID,
        revision=1,
        content_digest=DIGEST,
        action="edit_accept",
        effective_content=content(),
        note="Keep this decision durable.",
    )


class CentralWebStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "central.sqlite3"
        self.store = CentralStore.open(self.database_path)
        self.web_store = CentralWebStore(self.store.connection)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def review_batch(self, action_id: str = "web_action_review-1") -> CentralReviewBatch:
        source = draft_item()
        item = CentralReviewItem(
            review_id="rvi_" + "d" * 32,
            family_id=FAMILY_ID,
            publication_candidate_id=publication_candidate_id(FAMILY_ID),
            repository_id=REPOSITORY_ID,
            revision_id=REVISION_ID,
            revision=1,
            content_digest=DIGEST,
            action="edit_accept",
            effective_content=content(),
            note=source.note,
        )
        batch_id = central_review_batch_id(
            "org_demo", "user_demo", PRODUCT_ID, action_id, (source.to_dict(),)
        )
        item = replace(
            item,
            review_id=review_item_id(batch_id, item.publication_candidate_id),
        )
        return CentralReviewBatch(
            review_batch_id=batch_id,
            organization_id="org_demo",
            actor_id="user_demo",
            decision_space_id=DECISION_SPACE_ID,
            compatibility_product_id=PRODUCT_ID,
            compatibility_product_name="ZDecision",
            client_action_id=action_id,
            request_digest=hashlib.sha256(b"review").hexdigest(),
            approval=ApprovalRef("user", "web_review", action_id, NOW),
            items=(item,),
            created_at=NOW,
        )

    def test_review_item_id_is_bound_to_its_batch_and_candidate(self) -> None:
        batch = self.review_batch()
        with self.assertRaises(ValueError):
            replace(batch, items=(replace(batch.items[0], review_id="rvi_" + "e" * 32),))

    def test_review_batch_id_is_bound_to_its_ordered_item_projection(self) -> None:
        batch = self.review_batch()
        wrong_id = "rvb_" + "f" * 32
        wrong_item = replace(
            batch.items[0],
            review_id=review_item_id(wrong_id, batch.items[0].publication_candidate_id),
        )
        with self.assertRaises(ValueError):
            replace(batch, review_batch_id=wrong_id, items=(wrong_item,))

    def preview(
        self,
        batch: CentralReviewBatch,
        *,
        document_path: str | None = None,
        base_registry_path: str | None = None,
    ) -> PublicationRecord:
        candidate_id = publication_candidate_id(FAMILY_ID)
        decision = decision_id(candidate_id, PRODUCT_ID)
        document = PublicationFile.from_bytes(
            document_path
            or f"decision-registry/products/{PRODUCT_ID}/decisions/demo.md",
            b"# Demo\n",
        )
        registry_path = base_registry_path or document.path
        preview_id = publication_preview_id({
            "base_commit": "0" * 40,
            "base_registry_digests": {registry_path: "missing"},
            "decision_ids": (decision,),
            "publisher_format": "zdecision-publisher/v1",
            "review_ids": (batch.items[0].review_id,),
            "target_paths": (document.path,),
        })
        return PublicationRecord(
            record_version=1,
            preview_id=preview_id,
            content_digest=content_digest_for_files((document,)),
            state="previewed",
            created_at=NOW,
            review_batch_id=batch.review_batch_id,
            review_ids=(batch.items[0].review_id,),
            candidate_ids=(candidate_id,),
            decision_ids=(decision,),
            product_id=PRODUCT_ID,
            product_name="ZDecision",
            base_commit="0" * 40,
            base_registry_digests={registry_path: "missing"},
            display_documents=(document,),
            changed_files=(document,),
            commit_message=(
                f"decision({PRODUCT_ID}): publish 1 decisions\n\n"
                f"ZDecision-Preview: {preview_id}\n"
            ),
        )

    def test_preview_rejects_cross_product_registry_paths(self) -> None:
        batch = self.review_batch()
        self.web_store.put_review_batch(batch)
        other_product = "prod_" + "2" * 32
        invalid_document = self.preview(
            batch,
            document_path=(
                f"decision-registry/products/{other_product}/decisions/demo.md"
            ),
        )
        invalid_digest = self.preview(
            batch,
            base_registry_path=(
                f"decision-registry/products/{other_product}/registry.json"
            ),
        )

        with self.assertRaises(ValueError):
            self.web_store.put_preview("org_demo", PRODUCT_ID, invalid_document)
        with self.assertRaises(ValueError):
            self.web_store.put_preview("org_demo", PRODUCT_ID, invalid_digest)

    def test_family_maps_deterministically_to_v1_candidate(self) -> None:
        self.assertEqual(
            "cand_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_01",
            publication_candidate_id("cfm_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        )

    def test_candidate_web_association_can_reference_a_capture_group(self) -> None:
        columns = {
            row["name"]
            for row in self.store.connection.execute(
                "PRAGMA table_info(web_candidate_revision_batches)"
            ).fetchall()
        }
        self.assertIn("decision_space_id", columns)
        self.assertIn("ownership_json", columns)

    def test_registry_projection_schema_is_initialized_with_web_schema(self) -> None:
        tables = {
            row["name"]
            for row in self.store.connection.execute(
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

    def test_private_web_records_are_owned_by_decision_space(self) -> None:
        for table in (
            "web_review_drafts",
            "web_review_batches",
            "web_review_items",
            "web_review_submission_results",
            "web_publication_previews",
            "web_publications",
            "web_publication_families",
            "web_candidate_receipts",
        ):
            columns = {
                row["name"]
                for row in self.store.connection.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            self.assertIn("decision_space_id", columns, table)
            self.assertNotIn("product_id", columns, table)

    def test_legacy_publication_history_migrates_with_foreign_keys_enabled(
        self,
    ) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE decision_spaces (
              organization_id TEXT NOT NULL,
              decision_space_id TEXT NOT NULL,
              compatibility_product_id TEXT NOT NULL,
              PRIMARY KEY(organization_id, decision_space_id)
            );
            CREATE TABLE web_review_drafts (
              organization_id TEXT NOT NULL,
              actor_id TEXT NOT NULL,
              product_id TEXT NOT NULL,
              version INTEGER NOT NULL,
              record_json TEXT NOT NULL,
              record_digest TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(organization_id, actor_id, product_id)
            );
            CREATE TABLE web_review_batches (
              organization_id TEXT NOT NULL,
              product_id TEXT NOT NULL,
              review_batch_id TEXT NOT NULL,
              actor_id TEXT NOT NULL,
              client_action_id TEXT NOT NULL,
              request_digest TEXT NOT NULL,
              record_json TEXT NOT NULL,
              record_digest TEXT NOT NULL,
              created_at TEXT NOT NULL,
              submission_order INTEGER NOT NULL,
              PRIMARY KEY(organization_id, product_id, review_batch_id)
            );
            CREATE TABLE web_review_items (
              organization_id TEXT NOT NULL,
              product_id TEXT NOT NULL,
              review_batch_id TEXT NOT NULL,
              item_order INTEGER NOT NULL,
              review_id TEXT NOT NULL,
              family_id TEXT NOT NULL,
              publication_candidate_id TEXT NOT NULL,
              repository_id TEXT NOT NULL,
              revision_id TEXT NOT NULL,
              revision INTEGER NOT NULL,
              content_digest TEXT NOT NULL,
              action TEXT NOT NULL,
              effective_content_json TEXT,
              effective_content_digest TEXT,
              note TEXT,
              PRIMARY KEY(
                organization_id, product_id, review_batch_id, item_order
              ),
              FOREIGN KEY(organization_id, product_id, review_batch_id)
                REFERENCES web_review_batches(
                  organization_id, product_id, review_batch_id
                )
            );
            CREATE TABLE web_review_submission_results (
              organization_id TEXT NOT NULL,
              actor_id TEXT NOT NULL,
              product_id TEXT NOT NULL,
              review_batch_id TEXT NOT NULL,
              record_json TEXT NOT NULL,
              record_digest TEXT NOT NULL,
              PRIMARY KEY(organization_id, actor_id, review_batch_id),
              FOREIGN KEY(organization_id, product_id, review_batch_id)
                REFERENCES web_review_batches(
                  organization_id, product_id, review_batch_id
                )
            );
            CREATE TABLE web_publication_previews (
              organization_id TEXT NOT NULL,
              product_id TEXT NOT NULL,
              preview_id TEXT NOT NULL,
              review_batch_id TEXT NOT NULL,
              actor_id TEXT NOT NULL,
              record_json TEXT NOT NULL,
              record_digest TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY(organization_id, product_id, preview_id),
              FOREIGN KEY(organization_id, product_id, review_batch_id)
                REFERENCES web_review_batches(
                  organization_id, product_id, review_batch_id
                )
            );
            CREATE TABLE web_publications (
              organization_id TEXT NOT NULL,
              product_id TEXT NOT NULL,
              publication_id TEXT NOT NULL,
              preview_id TEXT NOT NULL,
              actor_id TEXT NOT NULL,
              state TEXT NOT NULL,
              recovery_code TEXT,
              commit_sha TEXT,
              record_json TEXT NOT NULL,
              record_digest TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(organization_id, product_id, publication_id),
              FOREIGN KEY(organization_id, product_id, preview_id)
                REFERENCES web_publication_previews(
                  organization_id, product_id, preview_id
                )
            );
            CREATE TABLE web_publication_families (
              organization_id TEXT NOT NULL,
              product_id TEXT NOT NULL,
              family_id TEXT NOT NULL,
              publication_id TEXT NOT NULL,
              PRIMARY KEY(organization_id, product_id, family_id),
              FOREIGN KEY(organization_id, product_id, publication_id)
                REFERENCES web_publications(
                  organization_id, product_id, publication_id
                )
            );
            CREATE TABLE web_candidate_receipts (
              organization_id TEXT NOT NULL,
              product_id TEXT NOT NULL,
              family_id TEXT NOT NULL,
              publication_candidate_id TEXT NOT NULL,
              decision_id TEXT NOT NULL,
              preview_id TEXT NOT NULL,
              commit_sha TEXT NOT NULL,
              recorded_at TEXT NOT NULL,
              PRIMARY KEY(organization_id, product_id, family_id)
            );
            """
        )
        connection.execute(
            "INSERT INTO decision_spaces VALUES (?, ?, ?)",
            ("org_demo", DECISION_SPACE_ID, PRODUCT_ID),
        )
        batch = self.review_batch()
        preview = self.preview(batch)
        publication = CentralPublication(
            publication_id=central_publication_id(preview.preview_id),
            organization_id="org_demo",
            actor_id="user_demo",
            decision_space_id=DECISION_SPACE_ID,
            compatibility_product_id=PRODUCT_ID,
            preview_id=preview.preview_id,
            confirm_action_id="web_action_publish-migration",
            confirm_request_digest="f" * 64,
            state="completed",
            approval=ApprovalRef(
                "user", "web_publication",
                "web_action_publish-migration", NOW,
            ),
            commit_sha="1" * 40,
            recovery_code=None,
            created_at=NOW,
            updated_at=NOW,
        )
        draft = ReviewDraft(
            "org_demo", "user_demo", DECISION_SPACE_ID, 1,
            (draft_item(),), NOW,
        )
        snapshot = ReviewSubmissionSnapshot(
            "org_demo", "user_demo", DECISION_SPACE_ID,
            batch.review_batch_id, True, (), 2,
        )

        def legacy_record(value: object, kind: str) -> tuple[str, str]:
            raw = value.to_dict()
            if kind == "batch":
                raw = {
                    **{
                        key: member
                        for key, member in raw.items()
                        if key not in (
                            "decision_space_id",
                            "compatibility_product_id",
                            "compatibility_product_name",
                        )
                    },
                    "product_id": PRODUCT_ID,
                    "product_name": "ZDecision",
                }
            elif kind == "publication":
                raw = {
                    **{
                        key: member
                        for key, member in raw.items()
                        if key not in (
                            "decision_space_id",
                            "compatibility_product_id",
                        )
                    },
                    "product_id": PRODUCT_ID,
                }
            else:
                raw = {
                    **{
                        key: member
                        for key, member in raw.items()
                        if key != "decision_space_id"
                    },
                    "product_id": PRODUCT_ID,
                }
            encoded = canonical_json_bytes(raw)
            return (
                encoded.decode("utf-8"),
                hashlib.sha256(encoded).hexdigest(),
            )

        draft_json, draft_digest = legacy_record(draft, "draft")
        connection.execute(
            "INSERT INTO web_review_drafts VALUES (?, ?, ?, 1, ?, ?, ?)",
            (
                "org_demo",
                "user_demo",
                PRODUCT_ID,
                draft_json,
                draft_digest,
                NOW,
            ),
        )
        batch_json, batch_digest = legacy_record(batch, "batch")
        connection.execute(
            """INSERT INTO web_review_batches
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                "org_demo", PRODUCT_ID, batch.review_batch_id,
                "user_demo", batch.client_action_id, batch.request_digest,
                batch_json, batch_digest, NOW,
            ),
        )
        item = batch.items[0]
        content_json = canonical_json_bytes(
            item.effective_content.to_dict()
        )
        connection.execute(
            """INSERT INTO web_review_items
               VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "org_demo", PRODUCT_ID, batch.review_batch_id,
                item.review_id, item.family_id,
                item.publication_candidate_id, item.repository_id,
                item.revision_id, item.revision, item.content_digest,
                item.action, content_json.decode("utf-8"),
                hashlib.sha256(content_json).hexdigest(), item.note,
            ),
        )
        snapshot_json, snapshot_digest = legacy_record(snapshot, "snapshot")
        connection.execute(
            """INSERT INTO web_review_submission_results
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "org_demo", "user_demo", PRODUCT_ID,
                batch.review_batch_id, snapshot_json, snapshot_digest,
            ),
        )
        preview_json = canonical_json_bytes(preview.to_dict())
        connection.execute(
            """INSERT INTO web_publication_previews
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "org_demo", PRODUCT_ID, preview.preview_id,
                batch.review_batch_id, "user_demo",
                preview_json.decode("utf-8"),
                hashlib.sha256(preview_json).hexdigest(), NOW,
            ),
        )
        publication_json, publication_digest = legacy_record(
            publication, "publication"
        )
        connection.execute(
            """INSERT INTO web_publications
               VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)""",
            (
                "org_demo", PRODUCT_ID, publication.publication_id,
                preview.preview_id, "user_demo", "completed",
                publication.commit_sha, publication_json,
                publication_digest, NOW, NOW,
            ),
        )
        connection.execute(
            "INSERT INTO web_publication_families VALUES (?, ?, ?, ?)",
            (
                "org_demo", PRODUCT_ID, item.family_id,
                publication.publication_id,
            ),
        )
        connection.execute(
            """INSERT INTO web_candidate_receipts
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "org_demo", PRODUCT_ID, item.family_id,
                item.publication_candidate_id, preview.decision_ids[0],
                preview.preview_id, publication.commit_sha, NOW,
            ),
        )
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")

        try:
            _migrate_leaf_owned_web_tables(connection)
        except ValueError as error:
            self.fail(f"valid publication history did not migrate: {error}")
        self.assertEqual(
            0,
            connection.execute("PRAGMA defer_foreign_keys").fetchone()[0],
        )
        connection.commit()

        self.assertEqual(1, connection.execute("PRAGMA foreign_keys").fetchone()[0])
        self.assertEqual(
            0,
            connection.execute("PRAGMA defer_foreign_keys").fetchone()[0],
        )
        self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
        for table in (
            "web_review_drafts",
            "web_review_batches",
            "web_review_items",
            "web_review_submission_results",
            "web_publication_previews",
            "web_publications",
            "web_publication_families",
            "web_candidate_receipts",
        ):
            self.assertEqual(
                {DECISION_SPACE_ID},
                {
                    row["decision_space_id"]
                    for row in connection.execute(
                        f"SELECT decision_space_id FROM {table}"
                    ).fetchall()
                },
                table,
            )
        web_store = CentralWebStore(connection)
        self.assertEqual(
            batch,
            web_store.get_review_batch(
                "org_demo", DECISION_SPACE_ID, batch.review_batch_id
            ),
        )
        self.assertEqual(
            snapshot,
            web_store.get_review_submission_result(
                "org_demo", "user_demo", DECISION_SPACE_ID,
                batch.review_batch_id,
            ),
        )
        self.assertEqual(
            publication,
            web_store.get_publication(
                "org_demo", publication.publication_id
            ),
        )

    def test_draft_compare_and_swap_survives_reopen(self) -> None:
        empty = self.web_store.get_draft(
            "org_demo", "user_demo", DECISION_SPACE_ID
        )
        saved = self.web_store.replace_draft(empty, (draft_item(),), NOW)
        self.assertEqual(1, saved.version)
        self.store.close()
        self.store = CentralStore.open(self.database_path)
        self.web_store = CentralWebStore(self.store.connection)
        self.assertEqual(
            saved,
            self.web_store.get_draft(
                "org_demo", "user_demo", DECISION_SPACE_ID
            ),
        )

    def test_draft_compare_and_swap_rejects_stale_version(self) -> None:
        empty = self.web_store.get_draft(
            "org_demo", "user_demo", DECISION_SPACE_ID
        )
        self.web_store.replace_draft(empty, (draft_item(),), NOW)
        with self.assertRaises(DraftConflict):
            self.web_store.replace_draft(empty, (), NOW)

    def test_action_replay_rejects_different_request_digest(self) -> None:
        self.web_store.record_action(
            "org_demo", "user_demo", "review", "web_action_1", "a" * 64,
            "rvb_" + "1" * 32, NOW,
        )
        with self.assertRaises(WebActionConflict):
            self.web_store.record_action(
                "org_demo", "user_demo", "review", "web_action_1", "b" * 64,
                "rvb_" + "2" * 32, NOW,
            )

    def test_review_batch_replays_only_exact_canonical_record(self) -> None:
        batch = self.review_batch()

        self.assertEqual(batch, self.web_store.put_review_batch(batch))
        self.assertEqual(batch, self.web_store.put_review_batch(batch))
        self.assertEqual(
            batch,
            self.web_store.get_review_batch(
                "org_demo", DECISION_SPACE_ID, batch.review_batch_id
            ),
        )

    def test_publication_replacement_allows_only_monotonic_state(self) -> None:
        batch = self.review_batch()
        self.web_store.put_review_batch(batch)
        preview = self.preview(batch)
        self.web_store.put_preview("org_demo", DECISION_SPACE_ID, preview)
        confirmed = CentralPublication(
            publication_id=central_publication_id(preview.preview_id),
            organization_id="org_demo",
            actor_id="user_demo",
            decision_space_id=DECISION_SPACE_ID,
            compatibility_product_id=PRODUCT_ID,
            preview_id=preview.preview_id,
            confirm_action_id="web_action_publish-1",
            confirm_request_digest="f" * 64,
            state="confirmed",
            approval=ApprovalRef("user", "web_publication", "web_action_publish-1", NOW),
            commit_sha=None,
            recovery_code=None,
            created_at=NOW,
            updated_at=NOW,
        )
        self.web_store.put_publication(confirmed)
        pending = replace(
            confirmed,
            state="committed_pending_push",
            commit_sha="1" * 40,
        )
        self.assertEqual(pending, self.web_store.replace_publication(confirmed, pending))

    def test_receipts_require_persisted_publication_and_preview(self) -> None:
        batch = self.review_batch()
        self.web_store.put_review_batch(batch)
        preview = self.preview(batch)
        publication = CentralPublication(
            publication_id=central_publication_id(preview.preview_id),
            organization_id="org_demo",
            actor_id="user_demo",
            decision_space_id=DECISION_SPACE_ID,
            compatibility_product_id=PRODUCT_ID,
            preview_id=preview.preview_id,
            confirm_action_id="web_action_publish-2",
            confirm_request_digest="f" * 64,
            state="confirmed",
            approval=ApprovalRef("user", "web_publication", "web_action_publish-2", NOW),
            commit_sha=None,
            recovery_code=None,
            created_at=NOW,
            updated_at=NOW,
        )

        with self.assertRaises(WebRecordConflict):
            self.web_store.put_family_receipts(publication, preview, "1" * 40)
        self.assertEqual(
            0,
            self.store.connection.execute(
                "SELECT COUNT(*) FROM web_candidate_receipts"
            ).fetchone()[0],
        )


if __name__ == "__main__":
    unittest.main()
