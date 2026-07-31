from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from zdecision.app_server.jsonl import (
    AppServerRequestError,
    AppServerTimeout,
)
from zdecision.app_server.models import AppServerTurnReceipt
from zdecision.capture.models import (
    Candidate,
    CandidateContent,
    SourceCheckpoint,
)
from zdecision.ids import candidate_family_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import (
    CandidateBatchUpload,
    CandidateRevisionUpload,
    UploadReceipt,
)


REQUEST_ID = "crq_11111111111111111111111111111111"
SECOND_REQUEST_ID = "crq_55555555555555555555555555555555"
OPERATION_KEY = "src_22222222222222222222222222222222"
PROFILE_ID = "fmp_33333333333333333333333333333333"
REPOSITORY_ID = "repo_44444444444444444444444444444444"


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


def _observation(seed: str, claim: str) -> Candidate:
    return Candidate(
        candidate_id=f"cand_{seed * 32}_01",
        capture_id=f"cap_{seed * 32}",
        ordinal=1,
        content=_content(claim),
        source=SourceCheckpoint(
            thread_id=f"thread-{seed}",
            turn_id=f"turn-{seed}",
        ),
    )


def _upload_batch(
    revisions: tuple[object, ...],
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
        for revision in revisions
    )
    return CandidateBatchUpload(
        request_id=REQUEST_ID,
        repository_id=REPOSITORY_ID,
        items=items,
        batch_digest=hashlib.sha256(
            canonical_json_bytes(
                {"items": [item.to_dict() for item in items]}
            )
        ).hexdigest(),
    )


class RequestStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "agent.sqlite3"

    def _api(self):
        try:
            from zdecision.agent.request_state import (
                CaptureResultUnknown,
                NativeAttemptConflict,
                NativeCallCoordinator,
                RequestStateStore,
            )
        except ModuleNotFoundError as error:
            self.fail(f"Request state API is missing: {error}")
        return (
            CaptureResultUnknown,
            NativeAttemptConflict,
            NativeCallCoordinator,
            RequestStateStore,
        )

    def _durable_api(self):
        try:
            from zdecision.agent.request_state import (
                BatchConflict,
                ReconciliationConflict,
                RequestStateStore,
            )
            from zdecision.capture.reconciliation import (
                ReconciliationDecision,
                ReconciliationResult,
                apply_reconciliation,
            )
        except (ImportError, ModuleNotFoundError) as error:
            self.fail(f"Durable reconciliation API is missing: {error}")
        return (
            BatchConflict,
            ReconciliationConflict,
            RequestStateStore,
            ReconciliationDecision,
            ReconciliationResult,
            apply_reconciliation,
        )

    def _reconciliation_fixture(self):
        (
            _,
            _,
            _,
            ReconciliationDecision,
            _,
            apply_reconciliation,
        ) = self._durable_api()
        first = _observation(
            "a", "更新候选决策按钮是采集授权边界"
        )
        second = _observation(
            "b", "是否达到稳定决策仍然无法可靠判断"
        )
        family_id = candidate_family_id(
            REPOSITORY_ID, first.candidate_id
        )
        result = apply_reconciliation(
            REPOSITORY_ID,
            (first, second),
            (),
            (
                ReconciliationDecision(
                    first.candidate_id,
                    "unrelated",
                    family_id,
                    None,
                ),
                ReconciliationDecision(
                    second.candidate_id,
                    "ambiguous",
                    None,
                    None,
                ),
            ),
        )
        return result, _upload_batch(result.uploadable_revisions)

    def test_native_attempt_transitions_are_durable_and_idempotent(self) -> None:
        _, _, _, RequestStateStore = self._api()
        store = RequestStateStore.open(self.path)
        self.addCleanup(store.close)

        prepared = store.get_or_create_native_attempt(
            REQUEST_ID,
            OPERATION_KEY,
            "capture_fork",
            "zdecision/capture/cap_1",
        )
        pending = store.mark_native_pending(
            REQUEST_ID, OPERATION_KEY, "capture_fork"
        )
        attached = store.attach_native_result(
            REQUEST_ID,
            OPERATION_KEY,
            "capture_fork",
            "thread-fork",
        )
        completed = store.complete_native_attempt(
            REQUEST_ID,
            OPERATION_KEY,
            "capture_fork",
            "a" * 64,
        )

        self.assertEqual("prepared", prepared.state)
        self.assertEqual("pending", pending.state)
        self.assertEqual("attached", attached.state)
        self.assertEqual("completed", completed.state)
        self.assertEqual("thread-fork", completed.native_id)
        self.assertEqual(
            completed,
            store.complete_native_attempt(
                REQUEST_ID,
                OPERATION_KEY,
                "capture_fork",
                "a" * 64,
            ),
        )
        store.close()
        reopened = RequestStateStore.open(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(
            completed,
            reopened.get_native_attempt(
                REQUEST_ID, OPERATION_KEY, "capture_fork"
            ),
        )

    def test_attempt_identity_or_result_conflicts_are_rejected(self) -> None:
        _, NativeAttemptConflict, _, RequestStateStore = self._api()
        store = RequestStateStore.open(self.path)
        self.addCleanup(store.close)
        store.get_or_create_native_attempt(
            REQUEST_ID,
            OPERATION_KEY,
            "inventory",
            "zdecision/cap_1/inventory",
        )

        with self.assertRaises(NativeAttemptConflict):
            store.get_or_create_native_attempt(
                REQUEST_ID,
                OPERATION_KEY,
                "inventory",
                "zdecision/cap_1/changed",
            )

        store.mark_native_pending(REQUEST_ID, OPERATION_KEY, "inventory")
        store.attach_native_result(
            REQUEST_ID, OPERATION_KEY, "inventory", "turn-1"
        )
        with self.assertRaises(NativeAttemptConflict):
            store.attach_native_result(
                REQUEST_ID, OPERATION_KEY, "inventory", "turn-2"
            )

    def test_unknown_thread_result_is_adopted_without_duplicate_creation(
        self,
    ) -> None:
        (
            CaptureResultUnknown,
            _,
            NativeCallCoordinator,
            RequestStateStore,
        ) = self._api()
        store = RequestStateStore.open(self.path)
        self.addCleanup(store.close)
        coordinator = NativeCallCoordinator(store)
        created = 0
        visible: dict[str, str] = {}

        def create() -> str:
            nonlocal created
            created += 1
            visible["zdecision/capture/cap_1"] = "thread-fork"
            raise AppServerTimeout("transport result unknown")

        with self.assertRaises(CaptureResultUnknown):
            coordinator.resolve_thread(
                request_id=REQUEST_ID,
                operation_key=OPERATION_KEY,
                stage="capture_fork",
                stable_tag="zdecision/capture/cap_1",
                find=visible.get,
                create=create,
            )
        self.assertEqual("pending", store.get_native_attempt(
            REQUEST_ID, OPERATION_KEY, "capture_fork"
        ).state)

        adopted = coordinator.resolve_thread(
            request_id=REQUEST_ID,
            operation_key=OPERATION_KEY,
            stage="capture_fork",
            stable_tag="zdecision/capture/cap_1",
            find=visible.get,
            create=create,
        )

        self.assertEqual("thread-fork", adopted)
        self.assertEqual(1, created)
        self.assertEqual("completed", store.get_native_attempt(
            REQUEST_ID, OPERATION_KEY, "capture_fork"
        ).state)

    def test_pending_without_visible_result_never_starts_replacement(self) -> None:
        (
            CaptureResultUnknown,
            _,
            NativeCallCoordinator,
            RequestStateStore,
        ) = self._api()
        store = RequestStateStore.open(self.path)
        self.addCleanup(store.close)
        store.get_or_create_native_attempt(
            REQUEST_ID,
            OPERATION_KEY,
            "capture_fork",
            "zdecision/capture/cap_1",
        )
        store.mark_native_pending(REQUEST_ID, OPERATION_KEY, "capture_fork")
        create_calls = 0

        def create() -> str:
            nonlocal create_calls
            create_calls += 1
            return "replacement-forbidden"

        with self.assertRaises(CaptureResultUnknown):
            NativeCallCoordinator(store).resolve_thread(
                request_id=REQUEST_ID,
                operation_key=OPERATION_KEY,
                stage="capture_fork",
                stable_tag="zdecision/capture/cap_1",
                find=lambda _: None,
                create=create,
            )

        self.assertEqual(0, create_calls)

    def test_explicit_native_rejection_resets_the_attempt(self) -> None:
        _, _, NativeCallCoordinator, RequestStateStore = self._api()
        store = RequestStateStore.open(self.path)
        self.addCleanup(store.close)

        with self.assertRaises(AppServerRequestError):
            NativeCallCoordinator(store).resolve_thread(
                request_id=REQUEST_ID,
                operation_key=OPERATION_KEY,
                stage="capture_fork",
                stable_tag="zdecision/capture/cap_1",
                find=lambda _: None,
                create=lambda: (_ for _ in ()).throw(
                    AppServerRequestError("thread/fork", -32602)
                ),
            )

        self.assertEqual(
            "prepared",
            store.get_native_attempt(
                REQUEST_ID, OPERATION_KEY, "capture_fork"
            ).state,
        )

    def test_structured_turn_unknown_result_is_read_back_by_client_id(
        self,
    ) -> None:
        (
            CaptureResultUnknown,
            _,
            NativeCallCoordinator,
            RequestStateStore,
        ) = self._api()
        store = RequestStateStore.open(self.path)
        self.addCleanup(store.close)
        coordinator = NativeCallCoordinator(store)
        visible: dict[str, AppServerTurnReceipt] = {}
        create_calls = 0
        receipt = AppServerTurnReceipt.create(
            thread_id="thread-fork",
            turn_id="turn-inventory",
            structured_output={"signals": []},
            model_profile_id=PROFILE_ID,
        )

        def create() -> AppServerTurnReceipt:
            nonlocal create_calls
            create_calls += 1
            visible["zdecision/cap_1/inventory"] = receipt
            raise AppServerTimeout("transport result unknown")

        with self.assertRaises(CaptureResultUnknown):
            coordinator.resolve_structured_turn(
                request_id=REQUEST_ID,
                operation_key=OPERATION_KEY,
                stage="inventory",
                stable_tag="zdecision/cap_1/inventory",
                read=visible.get,
                create=create,
            )

        adopted = coordinator.resolve_structured_turn(
            request_id=REQUEST_ID,
            operation_key=OPERATION_KEY,
            stage="inventory",
            stable_tag="zdecision/cap_1/inventory",
            read=visible.get,
            create=create,
        )

        self.assertEqual(receipt, adopted)
        self.assertEqual(1, create_calls)
        attempt = store.get_native_attempt(
            REQUEST_ID, OPERATION_KEY, "inventory"
        )
        self.assertEqual(receipt.turn_id, attempt.native_id)
        self.assertEqual(receipt.output_sha256, attempt.output_digest)

    def test_reconciliation_and_pending_batch_survive_restart(self) -> None:
        (
            _,
            _,
            RequestStateStore,
            _,
            _,
            _,
        ) = self._durable_api()
        result, batch = self._reconciliation_fixture()
        store = RequestStateStore.open(self.path)
        store.save_reconciliation(REQUEST_ID, result)
        store.stage_batch(
            REQUEST_ID, result.uploadable_revisions, batch
        )
        store.close()

        reopened = RequestStateStore.open(self.path)
        self.addCleanup(reopened.close)

        self.assertEqual(
            result, reopened.get_reconciliation(REQUEST_ID)
        )
        self.assertEqual(
            result.current_revisions,
            reopened.current_families(REPOSITORY_ID),
        )
        self.assertEqual(batch, reopened.pending_batch(REQUEST_ID))
        self.assertEqual(
            (second_id := result.ambiguous_observation_ids[0],),
            reopened.get_reconciliation(
                REQUEST_ID
            ).ambiguous_observation_ids,
        )
        self.assertTrue(second_id.startswith("cand_"))

    def test_same_reconciliation_and_batch_are_idempotent(self) -> None:
        (
            _,
            _,
            RequestStateStore,
            _,
            _,
            _,
        ) = self._durable_api()
        result, batch = self._reconciliation_fixture()
        store = RequestStateStore.open(self.path)
        self.addCleanup(store.close)

        store.save_reconciliation(REQUEST_ID, result)
        store.save_reconciliation(REQUEST_ID, result)
        store.stage_batch(
            REQUEST_ID, result.uploadable_revisions, batch
        )
        store.stage_batch(
            REQUEST_ID, result.uploadable_revisions, batch
        )

        self.assertEqual(batch, store.pending_batch(REQUEST_ID))

    def test_later_request_advances_family_head_across_restart(
        self,
    ) -> None:
        (
            _,
            _,
            RequestStateStore,
            ReconciliationDecision,
            _,
            apply_reconciliation,
        ) = self._durable_api()
        first_result, _ = self._reconciliation_fixture()
        current = first_result.current_revisions
        replacement = _observation(
            "c", "候选决策改为持续自动采集"
        )
        second_result = apply_reconciliation(
            REPOSITORY_ID,
            (replacement,),
            current,
            (
                ReconciliationDecision(
                    replacement.candidate_id,
                    "replace",
                    current[0].family_id,
                    replacement.content,
                ),
            ),
        )
        store = RequestStateStore.open(self.path)
        store.save_reconciliation(REQUEST_ID, first_result)
        store.save_reconciliation(
            SECOND_REQUEST_ID, second_result
        )
        store.close()

        reopened = RequestStateStore.open(self.path)
        self.addCleanup(reopened.close)
        head = reopened.current_families(REPOSITORY_ID)[0]

        self.assertEqual(2, head.revision)
        self.assertEqual(
            current[0].revision_id, head.supersedes_revision_id
        )
        self.assertEqual(
            replacement.content.claim, head.content.claim
        )

    def test_conflicting_reconciliation_or_batch_is_rejected(self) -> None:
        (
            BatchConflict,
            ReconciliationConflict,
            RequestStateStore,
            _,
            ReconciliationResult,
            _,
        ) = self._durable_api()
        result, batch = self._reconciliation_fixture()
        store = RequestStateStore.open(self.path)
        self.addCleanup(store.close)
        store.save_reconciliation(REQUEST_ID, result)
        store.stage_batch(
            REQUEST_ID, result.uploadable_revisions, batch
        )

        with self.assertRaises(ReconciliationConflict):
            store.save_reconciliation(
                REQUEST_ID,
                ReconciliationResult.empty(REPOSITORY_ID),
            )
        empty_batch = _upload_batch(())
        with self.assertRaises(BatchConflict):
            store.stage_batch(
                REQUEST_ID, result.uploadable_revisions, empty_batch
            )

    def test_upload_requires_exact_receipt_and_is_replay_safe(self) -> None:
        (
            BatchConflict,
            _,
            RequestStateStore,
            _,
            _,
            _,
        ) = self._durable_api()
        result, batch = self._reconciliation_fixture()
        store = RequestStateStore.open(self.path)
        self.addCleanup(store.close)
        store.save_reconciliation(REQUEST_ID, result)
        store.stage_batch(
            REQUEST_ID, result.uploadable_revisions, batch
        )

        with self.assertRaises(BatchConflict):
            store.mark_uploaded(
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
        store.mark_uploaded(receipt)
        store.mark_uploaded(receipt)

        self.assertIsNone(store.pending_batch(REQUEST_ID))
        with self.assertRaises(BatchConflict):
            store.mark_uploaded(
                UploadReceipt(
                    request_id=REQUEST_ID,
                    batch_digest=batch.batch_digest,
                    acknowledged_at="2026-07-31T03:00:01Z",
                )
            )


if __name__ == "__main__":
    unittest.main()
