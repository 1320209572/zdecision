from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import (
    AgentEvent,
    HookInvocation,
    RepositorySnapshot,
    TestRepositoryMapping,
    event_id_for,
)
from zdecision.app_server.models import FeasibilityModelProfile
from zdecision.app_server.requested_capture import SessionCaptureResult
from zdecision.capture.models import (
    Candidate,
    CandidateContent,
    SourceCheckpoint,
)
from zdecision.capture.reconciliation import (
    ReconciliationDecision,
    apply_reconciliation,
)
from zdecision.ids import candidate_family_id
from zdecision.sync.contracts import (
    ClaimedCaptureRequest,
    UploadReceipt,
)


REQUEST_ID = "crq_" + "1" * 32
SECOND_REQUEST_ID = "crq_" + "2" * 32
REPOSITORY_ID = "repo_" + "3" * 32
PRODUCT_ID = "prod_" + "4" * 32
NOW = datetime(2026, 7, 31, 5, 0, tzinfo=UTC)
SESSION_ID = "019fb100-0000-7000-8000-000000000001"
TURN_1 = "019fb100-0000-7000-8000-000000000002"
TURN_2 = "019fb100-0000-7000-8000-000000000003"


def claimed_request() -> ClaimedCaptureRequest:
    return ClaimedCaptureRequest(
        request_id=REQUEST_ID,
        repository_id=REPOSITORY_ID,
        product_id=PRODUCT_ID,
        product_name="ZDecision",
        template_id="business",
        lease_token="lease_0123456789abcdef",
        lease_expires_at="2026-07-31T05:00:30Z",
    )


def stop_event(cwd: str, turn_id: str, observed_at: str) -> AgentEvent:
    invocation = HookInvocation.from_dict(
        {
            "hook_event_name": "Stop",
            "session_id": SESSION_ID,
            "turn_id": turn_id,
            "cwd": cwd,
        },
        occurred_at=observed_at,
        repository=RepositorySnapshot(
            repository_id=REPOSITORY_ID,
            worktree_root=cwd,
            branch="main",
            head_commit="a" * 40,
        ),
    )
    return AgentEvent(
        event_id=event_id_for(invocation),
        invocation=invocation,
        state="recorded",
        failure_code=None,
    )


def observation(turn_id: str) -> Candidate:
    return Candidate(
        candidate_id="cand_" + "5" * 32 + "_01",
        capture_id="cap_" + "5" * 32,
        ordinal=1,
        content=CandidateContent(
            product="ZDecision",
            claim="页面操作是 Candidate 采集授权边界。",
            future_action="只处理页面请求冻结的 Session 边界。",
            scope_summary="按需 Candidate 采集",
            repositories=("zdecision",),
            paths=(),
            invalidation_conditions=("产品改变采集授权方式",),
        ),
        source=SourceCheckpoint(
            thread_id=SESSION_ID,
            turn_id=turn_id,
        ),
    )


class FakeCaptureRunner:
    def __init__(self) -> None:
        self.call_count = 0
        self.sweep_count = 0
        self.error: Exception | None = None
        self.after_freeze = None
        self.profile = FeasibilityModelProfile.create(
            model_id="model-default",
            reasoning_effort="medium",
            discovery_digest="a" * 64,
            discovered_at="2026-07-31T05:00:00Z",
        )

    def run(
        self,
        source,
        *,
        product_name: str,
        template_id: str,
        heartbeat=None,
    ) -> SessionCaptureResult:
        self.call_count += 1
        if self.error is not None:
            raise self.error
        if self.after_freeze is not None:
            callback = self.after_freeze
            self.after_freeze = None
            callback()
        return SessionCaptureResult(
            status="completed",
            source_key=source.source_key,
            capture_operation_id="cap_" + "5" * 32,
            inventory_turn_id="inventory-turn",
            extraction_turn_id="extraction-turn",
            observations=(observation(source.upper_turn_id),),
            evidence_digest="b" * 64,
            model_profile=self.profile,
        )

    def sweep_archives(self) -> None:
        self.sweep_count += 1


class FakeReconciliationRunner:
    def __init__(self, request_state) -> None:
        self.request_state = request_state
        self.call_count = 0

    def run(
        self,
        *,
        request_id,
        repository_id,
        cwd,
        observations,
        current,
        profile,
        heartbeat=None,
    ):
        self.call_count += 1
        ordered = tuple(sorted(
            observations, key=lambda item: item.candidate_id
        ))
        decisions = tuple(
            ReconciliationDecision(
                item.candidate_id,
                "unrelated",
                candidate_family_id(
                    repository_id, item.candidate_id
                ),
                None,
            )
            for item in ordered
        )
        result = apply_reconciliation(
            repository_id, ordered, current, decisions
        )
        self.request_state.save_reconciliation(
            request_id, result
        )
        return result


class FakeCentralClient:
    def __init__(self) -> None:
        self.upload_error: Exception | None = None
        self.complete_error: Exception | None = None
        self.uploads = []
        self.completed: list[str] = []
        self.calls: list[str] = []

    def start(self, request_id: str, lease_token: str) -> None:
        self.calls.append("start")

    def heartbeat(
        self, request_id: str, lease_token: str
    ) -> None:
        self.calls.append("heartbeat")

    def progress(
        self, request_id: str, lease_token: str, code: str
    ) -> None:
        self.calls.append(code)

    def upload_candidates(self, lease_token: str, batch):
        self.calls.append("upload")
        self.uploads.append(batch)
        if self.upload_error is not None:
            raise self.upload_error
        return UploadReceipt(
            request_id=batch.request_id,
            batch_digest=batch.batch_digest,
            acknowledged_at="2026-07-31T05:00:10Z",
        )

    def complete(
        self,
        request_id: str,
        lease_token: str,
        batch_digest: str,
    ) -> None:
        self.calls.append("complete")
        if self.complete_error is not None:
            raise self.complete_error
        self.completed.append(batch_digest)


class CaptureRequestProcessorTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.state_path = self.root / "state.sqlite3"
        self.database = AgentDatabase.open(self.state_path)
        self.addCleanup(self.database.close)
        self.database.put_test_repository_mapping(
            TestRepositoryMapping(
                repository_id=REPOSITORY_ID,
                product_id=PRODUCT_ID,
                product_name="ZDecision",
                enabled=True,
            )
        )
        from zdecision.agent.request_state import RequestStateStore
        from zdecision.agent.session_index import SessionIndex

        self.session_index = SessionIndex.open(
            self.root / "sessions.sqlite3"
        )
        self.addCleanup(self.session_index.close)
        self.request_state = RequestStateStore.open(
            self.root / "requests.sqlite3"
        )
        self.addCleanup(self.request_state.close)
        self.capture_runner = FakeCaptureRunner()
        self.reconciliation_runner = FakeReconciliationRunner(
            self.request_state
        )
        try:
            from zdecision.agent.capture_processor import (
                OnDemandCaptureProcessor,
            )
        except ModuleNotFoundError as error:
            self.fail(f"Capture Request processor is missing: {error}")
        self.processor = OnDemandCaptureProcessor(
            database=self.database,
            session_index=self.session_index,
            capture_runner=self.capture_runner,
            reconciliation_runner=self.reconciliation_runner,
            request_state=self.request_state,
            clock=lambda: NOW,
        )
        self.client = FakeCentralClient()

    def observe_turn_1(self) -> None:
        self.session_index.observe(
            stop_event(
                str(self.root),
                TURN_1,
                "2026-07-31T05:00:00Z",
            )
        )

    def test_checkpoint_advances_only_after_exact_upload_receipt(
        self,
    ) -> None:
        from zdecision.agent.service import (
            RetryableCaptureRequestError,
        )

        self.observe_turn_1()
        source = self.session_index.freeze_sources(
            REQUEST_ID, REPOSITORY_ID, NOW
        )[0]
        self.client.upload_error = ConnectionError("offline")

        with self.assertRaises(RetryableCaptureRequestError):
            self.processor.process(claimed_request(), self.client)
        self.assertIsNone(
            self.session_index.handled_turn(source.source_key)
        )

        self.client.upload_error = None
        self.processor.process(claimed_request(), self.client)

        self.assertEqual(
            TURN_1,
            self.session_index.handled_turn(source.source_key),
        )
        self.assertEqual(1, self.capture_runner.call_count)
        self.assertEqual(
            1, self.reconciliation_runner.call_count
        )
        self.assertEqual(2, len(self.client.uploads))

    def test_uploaded_receipt_resumes_completion_without_reupload(
        self,
    ) -> None:
        from zdecision.agent.service import (
            RetryableCaptureRequestError,
        )

        self.observe_turn_1()
        self.client.complete_error = ConnectionError("offline")
        with self.assertRaises(RetryableCaptureRequestError):
            self.processor.process(claimed_request(), self.client)
        self.client.complete_error = None

        self.processor.process(claimed_request(), self.client)

        self.assertEqual(1, self.capture_runner.call_count)
        self.assertEqual(1, len(self.client.uploads))
        self.assertEqual(2, self.client.calls.count("complete"))

    def test_activity_after_freeze_waits_for_the_next_click(
        self,
    ) -> None:
        self.observe_turn_1()
        self.capture_runner.after_freeze = lambda: (
            self.session_index.observe(
                stop_event(
                    str(self.root),
                    TURN_2,
                    "2026-07-31T05:01:00Z",
                )
            )
        )

        self.processor.process(claimed_request(), self.client)
        next_sources = self.session_index.freeze_sources(
            SECOND_REQUEST_ID, REPOSITORY_ID, NOW
        )

        self.assertEqual(1, len(next_sources))
        self.assertEqual(TURN_2, next_sources[0].upper_turn_id)
        self.assertEqual(
            TURN_1, next_sources[0].previous_handled_turn_id
        )

    def test_mapping_mismatch_fails_before_model_work(self) -> None:
        from zdecision.agent.service import (
            TerminalCaptureRequestError,
        )

        self.observe_turn_1()
        self.database.put_test_repository_mapping(
            TestRepositoryMapping(
                repository_id=REPOSITORY_ID,
                product_id="prod_" + "f" * 32,
                product_name="Wrong Product",
                enabled=True,
            )
        )

        with self.assertRaises(TerminalCaptureRequestError):
            self.processor.process(claimed_request(), self.client)

        self.assertEqual(0, self.capture_runner.call_count)
        self.assertEqual(
            0, self.reconciliation_runner.call_count
        )

    def test_disposable_attempt_failure_is_explicitly_retryable(self) -> None:
        from zdecision.agent.service import (
            RetryableCaptureRequestError,
        )
        from zdecision.app_server.requested_capture import (
            CaptureAttemptRetryable,
        )

        self.observe_turn_1()
        self.capture_runner.error = CaptureAttemptRetryable(
            "retry whole attempt"
        )

        with self.assertRaises(
            RetryableCaptureRequestError
        ) as raised:
            self.processor.process(claimed_request(), self.client)

        self.assertEqual(
            "capture_attempt_retryable", raised.exception.code
        )

    def test_missing_frozen_boundary_is_explicitly_terminal(self) -> None:
        from zdecision.agent.service import (
            TerminalCaptureRequestError,
        )
        from zdecision.app_server.requested_capture import (
            SourceBoundaryUnavailable,
        )

        self.observe_turn_1()
        self.capture_runner.error = SourceBoundaryUnavailable(
            "missing"
        )

        with self.assertRaises(
            TerminalCaptureRequestError
        ) as raised:
            self.processor.process(claimed_request(), self.client)

        self.assertEqual(
            "source_boundary_unavailable", raised.exception.code
        )

    def test_archive_sweep_runs_before_request_processing(self) -> None:
        self.processor.process(claimed_request(), self.client)

        self.assertEqual(1, self.capture_runner.sweep_count)

    def test_no_changed_source_uploads_canonical_empty_batch(
        self,
    ) -> None:
        self.processor.process(claimed_request(), self.client)

        self.assertEqual(1, len(self.client.uploads))
        self.assertEqual((), self.client.uploads[0].items)
        self.assertEqual(0, self.capture_runner.call_count)
        self.assertEqual(
            self.client.uploads[0].batch_digest,
            self.client.completed[0],
        )


if __name__ == "__main__":
    unittest.main()
