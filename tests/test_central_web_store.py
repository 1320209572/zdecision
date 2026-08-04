from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from zdecision.capture.models import CandidateContent
from zdecision.capture.reviews import ApprovalRef
from zdecision.central.store import CentralStore
from zdecision.ids import candidate_revision_id, product_id

from zdecision.central.web.contracts import (
    CentralPublication,
    CentralReviewBatch,
    CentralReviewItem,
    DraftItem,
)
from zdecision.central.web.store import (
    CentralWebStore,
    DraftConflict,
    WebActionConflict,
    WebRecordConflict,
)
from zdecision.ids import (
    central_publication_id,
    central_review_batch_id,
    decision_id,
    publication_candidate_id,
    publication_preview_id,
    review_item_id,
)
from zdecision.registry.publication import (
    PublicationFile,
    PublicationRecord,
    content_digest_for_files,
)


PRODUCT_ID = product_id("ZDecision")
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
            note=None,
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
            product_id=PRODUCT_ID,
            product_name="ZDecision",
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

    def preview(self, batch: CentralReviewBatch) -> PublicationRecord:
        candidate_id = publication_candidate_id(FAMILY_ID)
        decision = decision_id(candidate_id, PRODUCT_ID)
        document = PublicationFile.from_bytes(
            "decision-registry/products/zdecision/decisions/demo.md", b"# Demo\n"
        )
        preview_id = publication_preview_id({
            "base_commit": "0" * 40,
            "base_registry_digests": {document.path: "missing"},
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
            base_registry_digests={document.path: "missing"},
            display_documents=(document,),
            changed_files=(document,),
            commit_message=(
                f"decision({PRODUCT_ID}): publish 1 decisions\n\n"
                f"ZDecision-Preview: {preview_id}\n"
            ),
        )

    def test_family_maps_deterministically_to_v1_candidate(self) -> None:
        self.assertEqual(
            "cand_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_01",
            publication_candidate_id("cfm_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        )

    def test_draft_compare_and_swap_survives_reopen(self) -> None:
        empty = self.web_store.get_draft("org_demo", "user_demo", PRODUCT_ID)
        saved = self.web_store.replace_draft(empty, (draft_item(),), NOW)
        self.assertEqual(1, saved.version)
        self.store.close()
        self.store = CentralStore.open(self.database_path)
        self.web_store = CentralWebStore(self.store.connection)
        self.assertEqual(
            saved,
            self.web_store.get_draft("org_demo", "user_demo", PRODUCT_ID),
        )

    def test_draft_compare_and_swap_rejects_stale_version(self) -> None:
        empty = self.web_store.get_draft("org_demo", "user_demo", PRODUCT_ID)
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
                "org_demo", PRODUCT_ID, batch.review_batch_id
            ),
        )

    def test_publication_replacement_allows_only_monotonic_state(self) -> None:
        batch = self.review_batch()
        self.web_store.put_review_batch(batch)
        preview = self.preview(batch)
        self.web_store.put_preview("org_demo", PRODUCT_ID, preview)
        confirmed = CentralPublication(
            publication_id=central_publication_id(preview.preview_id),
            organization_id="org_demo",
            actor_id="user_demo",
            product_id=PRODUCT_ID,
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
            product_id=PRODUCT_ID,
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
