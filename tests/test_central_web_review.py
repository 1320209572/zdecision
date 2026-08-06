from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from zdecision.capture.models import CandidateContent
from zdecision.central.auth import Principal
from zdecision.central.decision_spaces import LeafDecisionSpace
from zdecision.central.registry_projection import RegistryProjectionStore
from zdecision.central.store import CentralStore
from zdecision.central.web.contracts import DraftItem
from zdecision.central.web.queries import CentralWebQueries
from zdecision.central.web.reviews import (
    CentralReviewError,
    CentralReviewService,
    ProductNotFound,
    ProductOwnershipConflict,
)
from zdecision.central.web import reviews as review_module
from zdecision.central.web.store import (
    CentralWebStore,
    DraftConflict,
    WebActionConflict,
    WebRecordCorrupt,
)
from zdecision.ids import candidate_revision_id, decision_space_id, product_id, repository_route_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.registry.models import ProductMetadata, ProductRegistry
from zdecision.registry.query import RegistrySnapshot
from zdecision.sync.contracts import (
    CandidateOwnershipSnapshot,
    CandidateRevisionUpload,
    RepositoryView,
)


NOW = "2026-08-04T08:00:00Z"
PRODUCT_NAME = "ZDecision"
PRODUCT_ID = product_id(PRODUCT_NAME)
REPOSITORY_ID = "repo_" + "1" * 32
FAMILY_ID = "cfm_" + "a" * 32
CAPTURE_REQUEST_ID = "crq_" + "3" * 32
OTHER_PRODUCT_NAME = "Other Product"
OTHER_PRODUCT_ID = product_id(OTHER_PRODUCT_NAME)
OTHER_REPOSITORY_ID = "repo_" + "2" * 32
OTHER_FAMILY_ID = "cfm_" + "b" * 32
FAMILY_B = "cfm_" + "c" * 32
FAMILY_C = "cfm_" + "d" * 32
PRODUCT_SPACE_ID = decision_space_id("product", PRODUCT_ID)
OTHER_SPACE_ID = decision_space_id("product", OTHER_PRODUCT_ID)


def _projection(connection: sqlite3.Connection) -> RegistryProjectionStore:
    store = RegistryProjectionStore(connection)
    snapshot = RegistrySnapshot(
        "c" * 40,
        {PRODUCT_ID: ProductMetadata(PRODUCT_ID, PRODUCT_NAME)},
        {PRODUCT_ID: ProductRegistry(PRODUCT_ID, {})},
        {},
    )
    store.mark_syncing(
        "org_demo", "c" * 40, "1" * 40,
        "2026-08-06T10:00:00Z", "2026-08-06T10:00:00Z",
    )
    store.install(
        "org_demo", "1" * 40, snapshot,
        "2026-08-06T10:00:00Z", "2026-08-06T10:00:00Z",
    )
    return store


def candidate_content(
    *, claim: str = "Keep product decisions explicit."
) -> CandidateContent:
    return CandidateContent(
        product=PRODUCT_NAME,
        claim=claim,
        future_action="Read the formal decision before changing this area.",
        scope_summary="Central product review",
        repositories=("zdecision",),
        paths=("src/zdecision/central/",),
        invalidation_conditions=("The product workflow changes.",),
    )


def revision(
    family_id: str,
    number: int,
    content: CandidateContent,
) -> CandidateRevisionUpload:
    digest = hashlib.sha256(canonical_json_bytes(content.to_dict())).hexdigest()
    return CandidateRevisionUpload(
        family_id=family_id,
        revision_id=candidate_revision_id(family_id, number, digest),
        revision=number,
        content=content,
        content_digest=digest,
        evidence_digest="e" * 64,
    )


class CentralWebReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = CentralStore.open(Path(directory.name) / "central.sqlite3")
        self.addCleanup(self.store.close)
        self.user = Principal("user", "org_demo", "user_demo", None)
        self.store.put_repository_mapping(
            "org_demo",
            RepositoryView(REPOSITORY_ID, PRODUCT_ID, PRODUCT_NAME, True),
        )
        self._put_space(PRODUCT_SPACE_ID, PRODUCT_ID, PRODUCT_NAME)
        self._put_space(OTHER_SPACE_ID, OTHER_PRODUCT_ID, OTHER_PRODUCT_NAME)
        self.store.put_repository_mapping(
            "org_demo",
            RepositoryView(
                OTHER_REPOSITORY_ID,
                OTHER_PRODUCT_ID,
                OTHER_PRODUCT_NAME,
                True,
            ),
        )
        self.current = revision(FAMILY_ID, 2, candidate_content(claim="current"))
        self.older = revision(FAMILY_ID, 1, candidate_content(claim="older"))
        self.other = revision(
            OTHER_FAMILY_ID,
            1,
            replace(candidate_content(), product=OTHER_PRODUCT_NAME),
        )
        with self.store.connection:
            self._insert_request(CAPTURE_REQUEST_ID, REPOSITORY_ID, PRODUCT_ID)
            self._insert_revision(
                "org_demo", REPOSITORY_ID, self.older, False,
                decision_space=PRODUCT_SPACE_ID,
                compatibility_product=PRODUCT_ID,
                compatibility_name=PRODUCT_NAME,
            )
            self._insert_revision(
                "org_demo", REPOSITORY_ID, self.current, True,
                decision_space=PRODUCT_SPACE_ID,
                compatibility_product=PRODUCT_ID,
                compatibility_name=PRODUCT_NAME,
            )
            self._insert_revision(
                "org_demo", OTHER_REPOSITORY_ID, self.other, True,
                decision_space=OTHER_SPACE_ID,
                compatibility_product=OTHER_PRODUCT_ID,
                compatibility_name=OTHER_PRODUCT_NAME,
            )
            self.store.connection.execute(
                """
                INSERT INTO web_candidate_revision_batches(
                    organization_id, repository_id, family_id, revision_id,
                    request_id, decision_space_id, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "org_demo",
                    REPOSITORY_ID,
                    FAMILY_ID,
                    self.current.revision_id,
                    CAPTURE_REQUEST_ID,
                    PRODUCT_SPACE_ID,
                    NOW,
                ),
            )
        queries = CentralWebQueries(
            self.store.connection, _projection(self.store.connection)
        )
        self.service = CentralReviewService(
            store=CentralWebStore(self.store.connection), queries=queries
        )

    def _put_space(
        self, space_id: str, compatibility_id: str, compatibility_name: str
    ) -> None:
        self.store.put_decision_space(
            "org_demo",
            LeafDecisionSpace(
                decision_space_id=space_id,
                kind="product",
                display_name=compatibility_name,
                compatibility_product_id=compatibility_id,
                compatibility_product_name=compatibility_name,
                catalog_group_id=None,
                catalog_breadcrumb=(),
                source_root=".",
                package_name=None,
                asset_type=None,
                enabled=True,
            ),
        )

    def _insert_request(
        self, request_id: str, repository_id: str, routed_product_id: str
    ) -> None:
        product_name = (
            PRODUCT_NAME if routed_product_id == PRODUCT_ID else OTHER_PRODUCT_NAME
        )
        self.store.connection.execute(
            """
            INSERT INTO capture_requests(
                request_id, organization_id, actor_id, repository_id,
                product_id, product_name, template_id, capture_scope,
                client_action_id, state, attempt_count, last_sequence,
                created_at, updated_at
            ) VALUES (?, 'org_demo', 'user_demo', ?, ?, ?, 'business',
                      'all_valid_sessions', 'web_action_fixture', 'succeeded',
                      1, 1, ?, ?)
            """,
            (request_id, repository_id, routed_product_id, product_name, NOW, NOW),
        )

    def _insert_revision(
        self,
        organization_id: str,
        repository_id: str,
        item: CandidateRevisionUpload,
        is_head: bool,
        *,
        decision_space: str = PRODUCT_SPACE_ID,
        compatibility_product: str = PRODUCT_ID,
        compatibility_name: str = PRODUCT_NAME,
    ) -> None:
        payload = canonical_json_bytes(item.to_dict())
        self.store.connection.execute(
            """
            INSERT INTO candidate_revisions(
                organization_id, repository_id, family_id, revision,
                revision_id, record_json, record_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                organization_id,
                repository_id,
                item.family_id,
                item.revision,
                item.revision_id,
                payload.decode("utf-8"),
                hashlib.sha256(payload).hexdigest(),
            ),
        )
        ownership = CandidateOwnershipSnapshot(
            repository_id=repository_id,
            route_id=repository_route_id(repository_id, decision_space),
            route_configuration_version=1,
            decision_space_id=decision_space,
            decision_space_kind="product",
            display_name=compatibility_name,
            catalog_breadcrumb=(),
            source_root=".",
            compatibility_product_id=compatibility_product,
            compatibility_product_name=compatibility_name,
            source_boundary_digest="9" * 64,
        )
        ownership_bytes = canonical_json_bytes(ownership.to_dict())
        self.store.connection.execute(
            """
            INSERT INTO candidate_revision_ownership(
                organization_id, repository_id, family_id, revision,
                decision_space_id, route_id, route_configuration_version,
                ownership_json, ownership_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                organization_id,
                repository_id,
                item.family_id,
                item.revision,
                ownership.decision_space_id,
                ownership.route_id,
                ownership.route_configuration_version,
                ownership_bytes.decode("utf-8"),
                hashlib.sha256(ownership_bytes).hexdigest(),
            ),
        )
        if is_head:
            self.store.connection.execute(
                """
                INSERT INTO candidate_family_heads(
                    organization_id, repository_id, family_id, revision,
                    revision_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    organization_id,
                    repository_id,
                    item.family_id,
                    item.revision,
                    item.revision_id,
                ),
            )

    def add_current(
        self, family_id: str, claim: str, *, revision_number: int = 1
    ) -> CandidateRevisionUpload:
        item = revision(
            family_id,
            revision_number,
            candidate_content(claim=claim),
        )
        with self.store.connection:
            self._insert_revision("org_demo", REPOSITORY_ID, item, True)
        return item

    def count_rows(self, table: str) -> int:
        allowed = {"web_review_batches", "web_publication_previews"}
        if table not in allowed:
            raise ValueError("unexpected table")
        return self.store.connection.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]

    @staticmethod
    def draft_item(
        item: CandidateRevisionUpload,
        repository_id: str,
        action: str = "accept",
        *,
        effective_content: CandidateContent | None = None,
        note: str | None = None,
    ) -> DraftItem:
        return DraftItem(
            family_id=item.family_id,
            repository_id=repository_id,
            revision_id=item.revision_id,
            revision=item.revision,
            content_digest=item.content_digest,
            action=action,
            effective_content=effective_content,
            note=note,
        )

    def test_inbox_contains_only_current_heads_for_route_product(self) -> None:
        view = self.service.list_candidates(self.user, PRODUCT_ID)

        self.assertEqual((FAMILY_ID,), tuple(item.family_id for item in view.items))
        self.assertEqual(self.current.revision_id, view.items[0].revision_id)
        self.assertNotIn("session", json.dumps(view.to_dict()).lower())

    def test_save_draft_rejects_wrong_version_without_losing_existing_actions(
        self,
    ) -> None:
        accepted = self.draft_item(self.current, REPOSITORY_ID)
        rejected = self.draft_item(
            self.current, REPOSITORY_ID, action="reject"
        )

        first = self.service.save_draft(
            self.user, PRODUCT_ID, 0, (accepted,), NOW
        )
        with self.assertRaises(DraftConflict):
            self.service.save_draft(
                self.user, PRODUCT_ID, 0, (rejected,), NOW
            )

        self.assertEqual(first, self.service.get_draft(self.user, PRODUCT_ID))

    def test_draft_cannot_reference_candidate_from_another_product(self) -> None:
        with self.assertRaises(ProductOwnershipConflict):
            self.service.save_draft(
                self.user,
                PRODUCT_ID,
                0,
                (self.draft_item(self.other, OTHER_REPOSITORY_ID),),
                NOW,
            )

    def test_review_rejects_items_from_another_leaf_even_when_mapping_matches(
        self,
    ) -> None:
        family = self.add_current(FAMILY_B, "belongs to another leaf")
        other_ownership = CandidateOwnershipSnapshot(
            repository_id=REPOSITORY_ID,
            route_id=repository_route_id(REPOSITORY_ID, OTHER_SPACE_ID),
            route_configuration_version=1,
            decision_space_id=OTHER_SPACE_ID,
            decision_space_kind="product",
            display_name=OTHER_PRODUCT_NAME,
            catalog_breadcrumb=(),
            source_root=".",
            compatibility_product_id=OTHER_PRODUCT_ID,
            compatibility_product_name=OTHER_PRODUCT_NAME,
            source_boundary_digest="8" * 64,
        )
        payload = canonical_json_bytes(other_ownership.to_dict())
        with self.store.connection:
            self.store.connection.execute(
                """
                UPDATE candidate_revision_ownership
                SET decision_space_id = ?, route_id = ?, ownership_json = ?,
                    ownership_digest = ?
                WHERE organization_id = 'org_demo' AND repository_id = ?
                  AND family_id = ? AND revision = ?
                """,
                (
                    OTHER_SPACE_ID,
                    other_ownership.route_id,
                    payload.decode("utf-8"),
                    hashlib.sha256(payload).hexdigest(),
                    REPOSITORY_ID,
                    family.family_id,
                    family.revision,
                ),
            )

        with self.assertRaises(CentralReviewError) as raised:
            self.service.save_draft(
                self.user,
                PRODUCT_ID,
                0,
                (self.draft_item(family, REPOSITORY_ID),),
                NOW,
            )

        self.assertEqual(
            "decision_space_ownership_conflict", raised.exception.code
        )

    def test_capture_batch_filter_uses_safe_request_association(self) -> None:
        view = self.service.list_candidates(
            self.user,
            PRODUCT_ID,
            capture_request_id=CAPTURE_REQUEST_ID,
        )

        self.assertEqual((FAMILY_ID,), tuple(item.family_id for item in view.items))
        self.assertEqual((CAPTURE_REQUEST_ID,), view.items[0].capture_request_ids)

    def test_stale_saved_draft_is_returned_against_the_new_head(self) -> None:
        stale = self.draft_item(self.older, REPOSITORY_ID)
        self.service.save_draft(self.user, PRODUCT_ID, 0, (stale,), NOW)

        item = self.service.list_candidates(self.user, PRODUCT_ID).items[0]

        self.assertEqual(self.current.revision_id, item.revision_id)
        self.assertEqual("accept", item.draft_action)
        self.assertTrue(item.stale_draft)

    def test_edit_accept_locks_product_and_repository_content(self) -> None:
        edited = replace(self.current.content, product="Untrusted Product")

        with self.assertRaises(ValueError):
            self.service.save_draft(
                self.user,
                PRODUCT_ID,
                0,
                (
                    self.draft_item(
                        self.current,
                        REPOSITORY_ID,
                        action="edit_accept",
                        effective_content=edited,
                    ),
                ),
                NOW,
            )

    def test_search_and_repository_filters_are_bounded_and_owned(self) -> None:
        self.assertEqual(
            (FAMILY_ID,),
            tuple(
                item.family_id
                for item in self.service.list_candidates(
                    self.user, PRODUCT_ID, search="current"
                ).items
            ),
        )
        self.assertEqual(
            (),
            self.service.list_candidates(
                self.user, PRODUCT_ID, search="not present"
            ).items,
        )
        with self.assertRaises(ProductOwnershipConflict):
            self.service.list_candidates(
                self.user, PRODUCT_ID, repository_id=OTHER_REPOSITORY_ID
            )
        with self.assertRaises(ValueError):
            self.service.list_candidates(self.user, PRODUCT_ID, search="界" * 67)

    def test_unknown_product_is_not_treated_as_an_empty_inbox(self) -> None:
        with self.assertRaises(ProductNotFound):
            self.service.get_draft(self.user, "prod_" + "f" * 32)

    def test_draft_rejects_canonical_payload_with_mismatched_storage_identity(
        self,
    ) -> None:
        mismatched = revision(
            "cfm_" + "c" * 32,
            self.current.revision,
            self.current.content,
        )
        payload = canonical_json_bytes(mismatched.to_dict())
        with self.store.connection:
            self.store.connection.execute(
                """
                UPDATE candidate_revisions
                SET record_json = ?, record_digest = ?
                WHERE organization_id = 'org_demo'
                  AND repository_id = ? AND family_id = ?
                  AND revision_id = ?
                """,
                (
                    payload.decode("utf-8"),
                    hashlib.sha256(payload).hexdigest(),
                    REPOSITORY_ID,
                    FAMILY_ID,
                    self.current.revision_id,
                ),
            )

        with self.assertRaises(WebRecordCorrupt):
            self.service.save_draft(
                self.user,
                PRODUCT_ID,
                0,
                (self.draft_item(self.current, REPOSITORY_ID),),
                NOW,
            )

    def test_partial_review_records_only_classified_items(self) -> None:
        family_b = self.add_current(FAMILY_B, "reject this revision")
        self.add_current(FAMILY_C, "leave this revision pending")
        accepted = self.draft_item(self.current, REPOSITORY_ID)
        rejected = self.draft_item(
            family_b, REPOSITORY_ID, action="reject", note="private note"
        )
        self.service.save_draft(
            self.user, PRODUCT_ID, 0, (accepted, rejected), NOW
        )

        result = self.service.submit(
            self.user,
            PRODUCT_ID,
            "web_action_review-partial",
            1,
            (accepted, rejected),
            NOW,
        )

        self.assertEqual(
            (FAMILY_ID, FAMILY_B),
            tuple(item.family_id for item in result.batch.items),
        )
        self.assertEqual((FAMILY_C,), result.remaining_pending)
        self.assertTrue(result.preview_eligible)
        self.assertEqual(2, result.draft_version)
        self.assertEqual(
            self.current.content, result.batch.items[0].effective_content
        )
        self.assertEqual("private note", result.batch.items[1].note)
        self.assertEqual(0, self.count_rows("web_publication_previews"))

    def test_one_changed_revision_writes_no_batch_and_keeps_draft(self) -> None:
        family_b = self.add_current(FAMILY_B, "original revision")
        accepted_a = self.draft_item(self.current, REPOSITORY_ID)
        accepted_b = self.draft_item(family_b, REPOSITORY_ID)
        saved = self.service.save_draft(
            self.user, PRODUCT_ID, 0, (accepted_a, accepted_b), NOW
        )
        replacement = revision(
            FAMILY_B, 2, candidate_content(claim="new current revision")
        )
        with self.store.connection:
            self._insert_revision("org_demo", REPOSITORY_ID, replacement, False)
            self.store.connection.execute(
                """
                UPDATE candidate_family_heads
                SET revision = ?, revision_id = ?
                WHERE organization_id = 'org_demo' AND repository_id = ?
                  AND family_id = ?
                """,
                (
                    replacement.revision,
                    replacement.revision_id,
                    REPOSITORY_ID,
                    FAMILY_B,
                ),
            )

        with self.assertRaises(review_module.ReviewStale) as raised:
            self.service.submit(
                self.user,
                PRODUCT_ID,
                "web_action_review-stale",
                1,
                (accepted_a, accepted_b),
                NOW,
            )

        self.assertEqual((FAMILY_B,), raised.exception.family_ids)
        self.assertEqual(0, self.count_rows("web_review_batches"))
        self.assertEqual(saved, self.service.get_draft(self.user, PRODUCT_ID))

    def test_identical_action_replays_and_changed_bytes_conflict(self) -> None:
        accepted = self.draft_item(self.current, REPOSITORY_ID)
        self.service.save_draft(self.user, PRODUCT_ID, 0, (accepted,), NOW)

        first = self.service.submit(
            self.user,
            PRODUCT_ID,
            "web_action_review-replay",
            1,
            (accepted,),
            NOW,
        )
        replay = self.service.submit(
            self.user,
            PRODUCT_ID,
            "web_action_review-replay",
            1,
            (accepted,),
            NOW,
        )
        changed = self.draft_item(
            self.current, REPOSITORY_ID, action="reject"
        )
        with self.assertRaises(WebActionConflict):
            self.service.submit(
                self.user,
                PRODUCT_ID,
                "web_action_review-replay",
                1,
                (changed,),
                NOW,
            )

        self.assertEqual(first, replay)
        self.assertEqual(1, self.count_rows("web_review_batches"))

    def test_reject_and_skip_are_processed_without_preview_eligibility(
        self,
    ) -> None:
        family_b = self.add_current(FAMILY_B, "skip this revision")
        rejected = self.draft_item(
            self.current, REPOSITORY_ID, action="reject"
        )
        skipped = self.draft_item(family_b, REPOSITORY_ID, action="skip")
        self.service.save_draft(
            self.user, PRODUCT_ID, 0, (rejected, skipped), NOW
        )

        result = self.service.submit(
            self.user,
            PRODUCT_ID,
            "web_action_review-no-preview",
            1,
            (rejected, skipped),
            NOW,
        )

        self.assertFalse(result.preview_eligible)
        self.assertEqual((FAMILY_B,), result.remaining_pending)
        self.assertEqual(
            (), self.service.list_candidates(self.user, PRODUCT_ID, state="accepted").items
        )
        self.assertEqual(
            (FAMILY_ID,),
            tuple(
                item.family_id
                for item in self.service.list_candidates(
                    self.user, PRODUCT_ID, state="rejected"
                ).items
            ),
        )
        self.assertEqual(0, self.count_rows("web_publication_previews"))

    def test_later_same_timestamp_reject_is_the_latest_review_state(
        self,
    ) -> None:
        accepted = self.draft_item(self.current, REPOSITORY_ID)
        self.service.save_draft(self.user, PRODUCT_ID, 0, (accepted,), NOW)
        self.service.submit(
            self.user,
            PRODUCT_ID,
            "web_action_same-time-accept",
            1,
            (accepted,),
            NOW,
        )
        rejected = self.draft_item(
            self.current, REPOSITORY_ID, action="reject"
        )
        self.service.save_draft(self.user, PRODUCT_ID, 2, (rejected,), NOW)
        self.service.submit(
            self.user,
            PRODUCT_ID,
            "web_action_same-time-reject",
            3,
            (rejected,),
            NOW,
        )

        self.assertEqual(
            (FAMILY_ID,),
            tuple(
                item.family_id
                for item in self.service.list_candidates(
                    self.user, PRODUCT_ID, state="rejected"
                ).items
            ),
        )

    def test_replay_returns_original_result_after_later_review_state(
        self,
    ) -> None:
        family_b = self.add_current(FAMILY_B, "later review")
        accepted = self.draft_item(self.current, REPOSITORY_ID)
        self.service.save_draft(self.user, PRODUCT_ID, 0, (accepted,), NOW)
        first = self.service.submit(
            self.user,
            PRODUCT_ID,
            "web_action_snapshot-first",
            1,
            (accepted,),
            NOW,
        )
        rejected = self.draft_item(
            family_b, REPOSITORY_ID, action="reject"
        )
        self.service.save_draft(self.user, PRODUCT_ID, 2, (rejected,), NOW)
        self.service.submit(
            self.user,
            PRODUCT_ID,
            "web_action_snapshot-later",
            3,
            (rejected,),
            NOW,
        )
        database_path = self.store.path
        self.store.close()
        self.store = CentralStore.open(database_path)
        self.addCleanup(self.store.close)
        self.service = CentralReviewService(
            store=CentralWebStore(self.store.connection),
            queries=CentralWebQueries(
                self.store.connection, _projection(self.store.connection)
            ),
        )

        replay = self.service.submit(
            self.user,
            PRODUCT_ID,
            "web_action_snapshot-first",
            1,
            (accepted,),
            NOW,
        )

        self.assertEqual((FAMILY_B,), first.remaining_pending)
        self.assertEqual(first, replay)


if __name__ == "__main__":
    unittest.main()
