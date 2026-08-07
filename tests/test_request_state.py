from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zdecision.capture.models import CandidateContent
from zdecision.capture.reconciliation import (
    CandidateFamilyRevision,
    ReconciliationResult,
)
from zdecision.capture.provenance import CandidateProvenanceSummary
from zdecision.ids import candidate_revision_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import (
    CandidateBatchUpload,
    CandidateRevisionUpload,
    CandidateSliceBatchUpload,
    UploadReceipt,
)


REQUEST_ID = "crq_11111111111111111111111111111111"
SECOND_REQUEST_ID = "crq_55555555555555555555555555555555"
REPOSITORY_ID = "repo_44444444444444444444444444444444"
DECISION_SPACE_ID = "dsp_66666666666666666666666666666666"
FAMILY_ID = "cfm_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
INPUT_DIGEST = "1" * 64
NOW = "2026-07-31T08:00:00Z"
SLICE_ID = "csl_" + "7" * 32
ROUTE_ID = "drr_" + "8" * 32


def _content(claim: str) -> CandidateContent:
    return CandidateContent(
        product="ZDecision",
        claim=claim,
        future_action="只在用户点击更新候选决策后采集",
        scope_summary="页面授权采集边界",
        repositories=("zdecision",),
        paths=(),
        invalidation_conditions=("用户重新定义采集边界",),
    )


def _revision(
    claim: str,
    *,
    revision: int = 1,
    supersedes: str | None = None,
    provenance: CandidateProvenanceSummary | None = None,
) -> CandidateFamilyRevision:
    content = _content(claim)
    content_digest = hashlib.sha256(
        canonical_json_bytes(content.to_dict())
    ).hexdigest()
    return CandidateFamilyRevision(
        family_id=FAMILY_ID,
        revision_id=candidate_revision_id(
            FAMILY_ID, revision, content_digest
        ),
        revision=revision,
        content=content,
        content_digest=content_digest,
        evidence_digest="e" * 64,
        supersedes_revision_id=supersedes,
        provenance=provenance,
    )


def result_for_claim(claim: str) -> ReconciliationResult:
    revision = _revision(claim)
    return ReconciliationResult(
        repository_id=REPOSITORY_ID,
        decision_space_id=DECISION_SPACE_ID,
        current_revisions=(revision,),
        new_revisions=(revision,),
        uploadable_revisions=(revision,),
        same_observation_ids=(),
        ambiguous_observation_ids=(),
    )


def replacement_result(
    previous: CandidateFamilyRevision, claim: str
) -> ReconciliationResult:
    revision = _revision(
        claim,
        revision=previous.revision + 1,
        supersedes=previous.revision_id,
    )
    return ReconciliationResult(
        repository_id=REPOSITORY_ID,
        decision_space_id=DECISION_SPACE_ID,
        current_revisions=(revision,),
        new_revisions=(revision,),
        uploadable_revisions=(revision,),
        same_observation_ids=(),
        ambiguous_observation_ids=(),
    )


def candidate_batch(
    result: ReconciliationResult,
    *,
    request_id: str = REQUEST_ID,
) -> CandidateBatchUpload:
    items = tuple(
        CandidateRevisionUpload(
            family_id=revision.family_id,
            revision_id=revision.revision_id,
            revision=revision.revision,
            content=revision.content,
            content_digest=revision.content_digest,
            evidence_digest=revision.evidence_digest,
        )
        for revision in result.uploadable_revisions
    )
    return CandidateBatchUpload(
        request_id=request_id,
        repository_id=result.repository_id,
        items=items,
        batch_digest=hashlib.sha256(
            canonical_json_bytes(
                {"items": [item.to_dict() for item in items]}
            )
        ).hexdigest(),
    )


def candidate_slice_batch(
    result: ReconciliationResult,
    *,
    request_id: str = REQUEST_ID,
) -> CandidateSliceBatchUpload:
    items = tuple(
        CandidateRevisionUpload(
            family_id=revision.family_id,
            revision_id=revision.revision_id,
            revision=revision.revision,
            content=revision.content,
            content_digest=revision.content_digest,
            evidence_digest=revision.evidence_digest,
            provenance=revision.provenance,
        )
        for revision in result.uploadable_revisions
    )
    return CandidateSliceBatchUpload(
        request_id=request_id,
        slice_id=SLICE_ID,
        route_id=ROUTE_ID,
        route_configuration_version=1,
        decision_space_id=result.decision_space_id,
        items=items,
        batch_digest=hashlib.sha256(
            canonical_json_bytes({"items": [item.to_dict() for item in items]})
        ).hexdigest(),
        item_protocol="candidate-provenance-v1",
    )


class RequestStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "agent.sqlite3"
        try:
            from zdecision.agent.request_state import RequestStateStore
        except ModuleNotFoundError as error:
            self.fail(f"Request state API is missing: {error}")
        self.store = RequestStateStore.open(self.path)
        self.addCleanup(self.store.close)

    def test_reconciliation_input_replay_and_generations_are_fenced(
        self,
    ) -> None:
        from zdecision.agent.request_state import ReconciliationConflict

        first = self.store.begin_reconciliation_attempt(
            REQUEST_ID, INPUT_DIGEST, NOW
        )
        self.store.abandon_reconciliation_attempt(
            first.attempt_id, "turn_result_unknown", NOW
        )
        second = self.store.begin_reconciliation_attempt(
            REQUEST_ID, INPUT_DIGEST, NOW
        )

        self.assertEqual(1, first.generation)
        self.assertEqual(2, second.generation)
        with self.assertRaises(ReconciliationConflict):
            self.store.begin_reconciliation_attempt(
                REQUEST_ID, "2" * 64, NOW
            )

    def test_validated_attempt_survives_restart_before_winner_cas(
        self,
    ) -> None:
        attempt = self.store.begin_reconciliation_attempt(
            REQUEST_ID, INPUT_DIGEST, NOW
        )
        self.store.attach_reconciliation_thread(
            attempt.attempt_id, "thread-1"
        )
        self.store.attach_reconciliation_turn(
            attempt.attempt_id, "turn-1"
        )
        expected = result_for_claim("winner")
        self.store.store_validated_reconciliation(
            attempt.attempt_id, expected, NOW
        )
        self.store.close()
        self._cleanups.pop()

        from zdecision.agent.request_state import RequestStateStore

        self.store = RequestStateStore.open(self.path)
        self.addCleanup(self.store.close)
        resumed = self.store.begin_reconciliation_attempt(
            REQUEST_ID, INPUT_DIGEST, NOW
        )
        committed = self.store.commit_reconciliation_attempt(
            resumed.attempt_id
        )

        self.assertEqual(attempt.attempt_id, resumed.attempt_id)
        self.assertEqual(expected, committed)

    def test_late_reconciliation_generation_cannot_change_family_heads(
        self,
    ) -> None:
        first = self.store.begin_reconciliation_attempt(
            REQUEST_ID, INPUT_DIGEST, NOW
        )
        self.store.abandon_reconciliation_attempt(
            first.attempt_id, "turn_result_unknown", NOW
        )
        second = self.store.begin_reconciliation_attempt(
            REQUEST_ID, INPUT_DIGEST, NOW
        )

        winner_result = result_for_claim("winner")
        self.store.store_validated_reconciliation(
            second.attempt_id, winner_result, NOW
        )
        winner = self.store.commit_reconciliation_attempt(
            second.attempt_id
        )
        self.store.commit_candidate_result(
            REQUEST_ID, winner, candidate_batch(winner)
        )

        self.store.store_validated_reconciliation(
            first.attempt_id, result_for_claim("late"), NOW
        )
        replay = self.store.commit_reconciliation_attempt(
            first.attempt_id
        )

        self.assertEqual(winner, replay)
        self.assertEqual(
            "winner",
            self.store.current_families(REPOSITORY_ID)[0].content.claim,
        )

    def test_known_terminal_reconciliation_thread_is_archived_once(
        self,
    ) -> None:
        attempt = self.store.begin_reconciliation_attempt(
            REQUEST_ID, INPUT_DIGEST, NOW
        )
        self.store.attach_reconciliation_thread(
            attempt.attempt_id, "thread-1"
        )
        self.store.abandon_reconciliation_attempt(
            attempt.attempt_id, "turn_result_unknown", NOW
        )

        pending = self.store.pending_reconciliation_archives()
        self.assertEqual((attempt.attempt_id,), tuple(
            item.attempt_id for item in pending
        ))
        self.store.mark_reconciliation_archived(attempt.attempt_id)
        self.assertEqual((), self.store.pending_reconciliation_archives())

    def test_candidate_result_and_outbox_rollback_as_one_transaction(
        self,
    ) -> None:
        result = result_for_claim("winner")
        batch = candidate_batch(result)

        with patch.object(
            self.store,
            "_insert_candidate_outbox",
            side_effect=RuntimeError("fault before commit"),
        ):
            with self.assertRaises(RuntimeError):
                self.store.commit_candidate_result(
                    REQUEST_ID, result, batch
                )

        self.assertIsNone(self.store.get_reconciliation(REQUEST_ID))
        self.assertEqual((), self.store.current_families(REPOSITORY_ID))
        self.assertIsNone(self.store.staged_batch(REQUEST_ID))

        committed = self.store.commit_candidate_result(
            REQUEST_ID, result, batch
        )
        self.assertEqual(batch, committed)
        self.assertEqual(result, self.store.get_reconciliation(REQUEST_ID))
        self.assertEqual(
            result.current_revisions,
            self.store.current_families(REPOSITORY_ID),
        )
        self.assertEqual(batch, self.store.pending_batch(REQUEST_ID))

    def test_candidate_commit_is_exactly_replay_safe(self) -> None:
        from zdecision.agent.request_state import BatchConflict

        result = result_for_claim("winner")
        batch = candidate_batch(result)
        self.store.commit_candidate_result(REQUEST_ID, result, batch)

        self.assertEqual(
            batch,
            self.store.commit_candidate_result(
                REQUEST_ID, result, batch
            ),
        )
        empty = ReconciliationResult.empty(
            REPOSITORY_ID, DECISION_SPACE_ID
        )
        with self.assertRaises(BatchConflict):
            self.store.commit_candidate_result(
                REQUEST_ID, empty, candidate_batch(empty)
            )

    def test_slice_outbox_replays_exact_provenance_and_rejects_digest_only_conflict(self) -> None:
        from zdecision.agent.request_state import BatchConflict, RequestStateStore

        summary = CandidateProvenanceSummary(
            "candidate-provenance-v1",
            "host_observed_user_prompt_anchor",
            "a" * 64,
        )
        revision = _revision("winner", provenance=summary)
        result = ReconciliationResult(
            REPOSITORY_ID,
            DECISION_SPACE_ID,
            (revision,),
            (revision,),
            (revision,),
            (),
            (),
        )
        batch = candidate_slice_batch(result)
        self.store.store_slice_reconciliation(REQUEST_ID, SLICE_ID, result)
        self.store.commit_slice_result(REQUEST_ID, SLICE_ID, result, batch)
        self.store.close()
        self._cleanups.pop()
        self.store = RequestStateStore.open(self.path)
        self.addCleanup(self.store.close)

        self.assertEqual(batch, self.store.staged_slice_batch(REQUEST_ID, SLICE_ID))
        self.assertEqual(summary, self.store.staged_slice_batch(REQUEST_ID, SLICE_ID).items[0].provenance)

        changed_summary = CandidateProvenanceSummary(
            "candidate-provenance-v1",
            "host_observed_user_prompt_anchor",
            "b" * 64,
        )
        changed_revision = _revision("winner", provenance=changed_summary)
        changed_result = ReconciliationResult(
            REPOSITORY_ID,
            DECISION_SPACE_ID,
            (changed_revision,),
            (changed_revision,),
            (changed_revision,),
            (),
            (),
        )
        with self.assertRaises(BatchConflict):
            self.store.commit_slice_result(
                REQUEST_ID,
                SLICE_ID,
                changed_result,
                candidate_slice_batch(changed_result),
            )

    def test_later_request_moves_family_head_only_forward(self) -> None:
        first = result_for_claim("first")
        self.store.commit_candidate_result(
            REQUEST_ID, first, candidate_batch(first)
        )
        second = replacement_result(
            first.current_revisions[0], "second"
        )
        self.store.commit_candidate_result(
            SECOND_REQUEST_ID,
            second,
            candidate_batch(second, request_id=SECOND_REQUEST_ID),
        )
        self.store.close()
        self._cleanups.pop()

        from zdecision.agent.request_state import RequestStateStore

        self.store = RequestStateStore.open(self.path)
        self.addCleanup(self.store.close)
        head = self.store.current_families(REPOSITORY_ID)[0]

        self.assertEqual(2, head.revision)
        self.assertEqual("second", head.content.claim)

    def test_upload_requires_exact_receipt_and_is_replay_safe(self) -> None:
        from zdecision.agent.request_state import BatchConflict

        result = result_for_claim("winner")
        batch = candidate_batch(result)
        self.store.commit_candidate_result(REQUEST_ID, result, batch)

        with self.assertRaises(BatchConflict):
            self.store.mark_uploaded(
                UploadReceipt(
                    request_id=REQUEST_ID,
                    batch_digest="0" * 64,
                    acknowledged_at="2026-07-31T03:00:00Z",
                )
            )
        receipt = UploadReceipt(
            request_id=REQUEST_ID,
            batch_digest=batch.batch_digest,
            acknowledged_at="2026-07-31T03:00:00Z",
        )
        self.store.mark_uploaded(receipt)
        self.store.mark_uploaded(receipt)

        self.assertIsNone(self.store.pending_batch(REQUEST_ID))
        self.assertEqual(receipt, self.store.upload_receipt(REQUEST_ID))

    def test_legacy_native_table_drops_only_with_all_replacements(self) -> None:
        self.store.close()
        self._cleanups.pop()
        connection = sqlite3.connect(self.path)
        with connection:
            connection.executescript(
                """
                DROP TABLE IF EXISTS native_attempts;
                CREATE TABLE native_attempts (request_id TEXT);
                CREATE TABLE IF NOT EXISTS capture_operations (
                    operation_id TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS capture_execution_attempts (
                    attempt_id TEXT PRIMARY KEY
                );
                """
            )
        connection.close()

        from zdecision.agent.request_state import RequestStateStore

        self.store = RequestStateStore.open(self.path)
        self.addCleanup(self.store.close)
        names = {
            row[0]
            for row in self.store._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

        self.assertNotIn("native_attempts", names)


if __name__ == "__main__":
    unittest.main()
