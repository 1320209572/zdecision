from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_inventory import VALID_INVENTORY
from zdecision.agent.capture_operation_store import CaptureOperationStore
from zdecision.agent.session_index import FrozenSessionSource
from zdecision.app_server.gateway import (
    FrozenModelProfileUnavailable,
    UnknownSourceTurn,
)
from zdecision.app_server.jsonl import AppServerTimeout
from zdecision.app_server.models import (
    AppServerTurnReceipt,
    FeasibilityModelProfile,
    SourceBoundary,
)
from zdecision.capture.templates import TemplateCatalog


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPOSITORY_ROOT / "decision-templates"
ENVELOPE_ROOT = (
    REPOSITORY_ROOT / "src" / "zdecision" / "capture" / "prompt_contracts"
)
REQUEST_ID = "crq_11111111111111111111111111111111"
SOURCE_SESSION = "019fb100-0000-7000-8000-000000000001"
SOURCE_TURN = "019fb100-0000-7000-8000-000000000002"


class FakeGateway:
    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.interactive_ids = frozenset((SOURCE_SESSION,))
        self.boundary_cwd = cwd
        self.boundary_available = True
        self.boundary_calls: list[tuple[str, str]] = []
        self.profile = FeasibilityModelProfile.create(
            model_id="model-default",
            reasoning_effort="medium",
            discovery_digest="a" * 64,
            discovered_at="2026-07-30T12:00:00.000000Z",
        )
        self.discover_count = 0
        self.support_check_count = 0
        self.profile_available = True
        self.stage_names: list[str] = []
        self.extraction_output: dict[str, object] = {
            "candidates": [
                {
                    "product": "ZDecision",
                    "claim": "页面上的 Update Candidates 是采集授权边界",
                    "future_action": "只在用户点击后冻结并处理变化的会话",
                    "scope": {
                        "summary": "ZDecision 按需采集",
                        "repositories": ["zdecision"],
                        "paths": [],
                    },
                    "invalidation_conditions": [
                        "产品重新采用零接触自动采集"
                    ],
                }
            ]
        }
        self.fork_count = 0
        self.inventory_count = 0
        self.extraction_count = 0
        self.drop_first_fork_response = False
        self.drop_first_inventory_result = False
        self.drop_first_extraction_result = False
        self.archive_failures_remaining = 0
        self.archived_threads: list[str] = []

    def list_interactive_thread_ids(self, cwd: str) -> frozenset[str]:
        if cwd != self.cwd:
            return frozenset()
        return self.interactive_ids

    def read_completed_boundary(
        self, thread_id: str, turn_id: str
    ) -> SourceBoundary:
        self.boundary_calls.append((thread_id, turn_id))
        if not self.boundary_available:
            raise UnknownSourceTurn("missing exact source boundary")
        return SourceBoundary(
            thread_id=thread_id,
            turn_id=turn_id,
            cwd=self.boundary_cwd,
            status="completed",
            model_id="model-default",
            reasoning_effort="medium",
        )

    def discover_and_freeze_profile(
        self, boundary: SourceBoundary
    ) -> FeasibilityModelProfile:
        self.discover_count += 1
        return self.profile

    def resolve_active_profile(self) -> FeasibilityModelProfile:
        self.discover_count += 1
        return self.profile

    def require_supported_profile(
        self, profile: FeasibilityModelProfile
    ) -> FeasibilityModelProfile:
        self.support_check_count += 1
        if not self.profile_available:
            raise FrozenModelProfileUnavailable("model removed")
        return profile

    def fork_disposable_thread(
        self, thread_id: str, last_turn_id: str
    ) -> str:
        self.fork_count += 1
        created = f"fork-{self.fork_count}"
        if self.drop_first_fork_response and self.fork_count == 1:
            raise AppServerTimeout("transport result unknown")
        return created

    def run_structured_turn(
        self,
        thread_id: str,
        prompt: str,
        output_schema,
        profile: FeasibilityModelProfile,
        cwd: str,
    ) -> AppServerTurnReceipt:
        properties = output_schema.get("properties", {})
        if "signals" in properties:
            stage = "inventory"
            self.inventory_count += 1
            count = self.inventory_count
            output = VALID_INVENTORY
            should_drop = self.drop_first_inventory_result and count == 1
        elif "candidates" in properties:
            stage = "extraction"
            self.extraction_count += 1
            count = self.extraction_count
            output = self.extraction_output
            should_drop = self.drop_first_extraction_result and count == 1
        else:
            raise AssertionError("unexpected structured-output schema")
        self.stage_names.append(stage)
        receipt = AppServerTurnReceipt.create(
            thread_id=thread_id,
            turn_id=f"{stage}-turn-{count}",
            structured_output=output,
            model_profile_id=profile.profile_id,
        )
        if should_drop:
            raise AppServerTimeout("transport result unknown")
        return receipt

    def archive_thread(self, thread_id: str) -> None:
        if self.archive_failures_remaining:
            self.archive_failures_remaining -= 1
            raise AppServerTimeout("archive transport unavailable")
        self.archived_threads.append(thread_id)


class RequestedCaptureRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.template_root = self.root / "decision-templates"
        shutil.copytree(TEMPLATE_ROOT, self.template_root)
        self.gateway = FakeGateway(str(self.root))
        self.request_profile = self.gateway.profile
        self.operation_store = CaptureOperationStore.open(
            self.root / "capture-operations.sqlite3"
        )
        self.addCleanup(self.operation_store.close)
        self.catalog = TemplateCatalog(
            self.template_root, ENVELOPE_ROOT
        )
        try:
            from zdecision.app_server.requested_capture import (
                RequestedCaptureRunner,
            )
        except ModuleNotFoundError as error:
            self.fail(f"Requested Capture API is missing: {error}")
        self.runner = RequestedCaptureRunner(
            gateway=self.gateway,
            operation_store=self.operation_store,
            template_catalog=self.catalog,
        )
        self.source = FrozenSessionSource(
            request_id=REQUEST_ID,
            source_key="src_22222222222222222222222222222222",
            repository_id="repo_33333333333333333333333333333333",
            session_id=SOURCE_SESSION,
            cwd=str(self.root),
            lineage="lin_44444444444444444444444444444444",
            previous_handled_turn_id=None,
            upper_turn_id=SOURCE_TURN,
            source_fingerprint="5" * 64,
        )

    def _run(self):
        return self.runner.run(
            self.source,
            product_name="ZDecision",
            template_id="business",
            model_profile=self.request_profile,
        )

    def test_new_operation_uses_supplied_request_profile(self) -> None:
        supplied = self.request_profile

        self._run()

        operation = self.operation_store.operation_for_source(
            self.source.request_id, self.source.source_key
        )
        self.assertEqual(
            supplied.profile_id,
            operation.frozen.model_profile_id,
        )
        self.assertEqual(0, self.gateway.discover_count)

    def test_operation_profile_reads_frozen_replay_profile(self) -> None:
        self._run()

        self.assertEqual(
            self.request_profile,
            self.runner.operation_profile(self.source),
        )

    def test_request_profile_resolution_discovers_only_when_missing(self) -> None:
        active = self.runner.resolve_request_profile(None)
        frozen = self.runner.resolve_request_profile(self.request_profile)

        self.assertEqual(self.gateway.profile, active)
        self.assertIs(self.request_profile, frozen)
        self.assertEqual(1, self.gateway.discover_count)
        self.assertEqual(1, self.gateway.support_check_count)

    def test_unavailable_frozen_profile_is_explicit(self) -> None:
        from zdecision.app_server.requested_capture import FrozenModelUnavailable

        self.gateway.profile_available = False

        with self.assertRaises(FrozenModelUnavailable):
            self.runner.resolve_request_profile(self.request_profile)

    def test_request_runs_inventory_then_extraction_without_assessment(
        self,
    ) -> None:
        result = self._run()

        self.assertEqual(
            ("inventory", "extraction"),
            tuple(self.gateway.stage_names),
        )
        self.assertEqual(
            ("cand_" + result.capture_operation_id[4:] + "_01",),
            tuple(item.candidate_id for item in result.observations),
        )
        self.assertEqual("completed", result.status)
        self.assertEqual(64, len(result.evidence_digest))
        self.assertEqual(["fork-1"], self.gateway.archived_threads)

    def test_zero_candidates_is_success(self) -> None:
        self.gateway.extraction_output = {"candidates": []}

        result = self._run()

        self.assertEqual((), result.observations)
        self.assertEqual("completed", result.status)

    def test_heartbeat_wraps_each_structured_turn(self) -> None:
        heartbeats: list[str] = []

        self.runner.run(
            self.source,
            product_name="ZDecision",
            template_id="business",
            model_profile=self.request_profile,
            heartbeat=lambda: heartbeats.append("renewed"),
        )

        self.assertEqual(
            ["renewed", "renewed", "renewed", "renewed"],
            heartbeats,
        )

    def test_noninteractive_source_is_excluded(self) -> None:
        from zdecision.app_server.requested_capture import SourceNotInteractive

        self.gateway.interactive_ids = frozenset()

        with self.assertRaises(SourceNotInteractive):
            self._run()

    def test_unknown_fork_starts_a_new_generation(self) -> None:
        from zdecision.app_server.requested_capture import (
            CaptureAttemptRetryable,
        )

        self.gateway.drop_first_fork_response = True
        with self.assertRaises(CaptureAttemptRetryable):
            self._run()

        result = self._run()

        self.assertEqual(2, self.gateway.fork_count)
        self.assertEqual(1, self.gateway.inventory_count)
        self.assertEqual(1, self.gateway.extraction_count)
        self.assertEqual("completed", result.status)

    def test_unknown_inventory_abandons_the_whole_attempt(self) -> None:
        from zdecision.app_server.requested_capture import (
            CaptureAttemptRetryable,
        )

        self.gateway.drop_first_inventory_result = True
        with self.assertRaises(CaptureAttemptRetryable):
            self._run()

        result = self._run()

        self.assertEqual(2, self.gateway.fork_count)
        self.assertEqual(2, self.gateway.inventory_count)
        self.assertEqual(1, self.gateway.extraction_count)
        operation = self.operation_store.operation_for_source(
            REQUEST_ID, self.source.source_key
        )
        self.assertEqual("committed", operation.status)
        self.assertEqual("completed", result.status)

    def test_unknown_extraction_reruns_both_stages(self) -> None:
        from zdecision.app_server.requested_capture import (
            CaptureAttemptRetryable,
        )

        self.gateway.drop_first_extraction_result = True
        with self.assertRaises(CaptureAttemptRetryable):
            self._run()

        self._run()

        self.assertEqual(2, self.gateway.fork_count)
        self.assertEqual(2, self.gateway.inventory_count)
        self.assertEqual(2, self.gateway.extraction_count)

    def test_retry_uses_the_frozen_profile_and_template(self) -> None:
        from zdecision.app_server.requested_capture import (
            CaptureAttemptRetryable,
        )

        self.gateway.drop_first_fork_response = True
        with self.assertRaises(CaptureAttemptRetryable):
            self._run()
        operation = self.operation_store.operation_for_source(
            REQUEST_ID, self.source.source_key
        )
        frozen_prompt = operation.frozen.template.inventory_prompt
        first_profile = operation.frozen.model_profile_id
        policy = self.template_root / "business" / "inventory.md"
        policy.write_text(
            policy.read_text("utf-8") + "\nA changed live policy.\n",
            "utf-8",
        )
        self.gateway.profile = FeasibilityModelProfile.create(
            model_id="changed-model",
            reasoning_effort="high",
            discovery_digest="b" * 64,
            discovered_at="2026-07-31T12:00:00.000000Z",
        )

        result = self._run()

        replayed = self.operation_store.operation_for_source(
            REQUEST_ID, self.source.source_key
        )
        self.assertEqual(0, self.gateway.discover_count)
        self.assertEqual(first_profile, result.model_profile.profile_id)
        self.assertEqual(
            frozen_prompt, replayed.frozen.template.inventory_prompt
        )
        self.assertNotIn(
            "A changed live policy.",
            replayed.frozen.template.inventory_prompt,
        )

    def test_replay_rejects_a_different_request_profile(self) -> None:
        from zdecision.app_server.requested_capture import RequestedCaptureFailed

        self._run()
        changed = FeasibilityModelProfile.create(
            model_id="changed-model",
            reasoning_effort="high",
            discovery_digest="b" * 64,
            discovered_at="2026-07-31T12:00:00.000000Z",
        )

        with self.assertRaises(RequestedCaptureFailed):
            self.runner.run(
                self.source,
                product_name="ZDecision",
                template_id="business",
                model_profile=changed,
            )

    def test_completed_operation_replay_starts_no_native_work(self) -> None:
        first = self._run()
        counts = (
            self.gateway.fork_count,
            self.gateway.inventory_count,
            self.gateway.extraction_count,
        )

        replay = self._run()

        self.assertEqual(first, replay)
        self.assertEqual(
            counts,
            (
                self.gateway.fork_count,
                self.gateway.inventory_count,
                self.gateway.extraction_count,
            ),
        )

    def test_restart_commits_active_validated_attempt_without_model_work(
        self,
    ) -> None:
        with patch.object(
            self.operation_store,
            "commit_attempt",
            side_effect=RuntimeError("crash before operation CAS"),
        ):
            with self.assertRaises(RuntimeError):
                self._run()
        counts = (
            self.gateway.fork_count,
            self.gateway.inventory_count,
            self.gateway.extraction_count,
        )

        result = self._run()

        self.assertEqual("completed", result.status)
        self.assertEqual(
            counts,
            (
                self.gateway.fork_count,
                self.gateway.inventory_count,
                self.gateway.extraction_count,
            ),
        )

    def test_archive_failure_never_reopens_committed_model_work(self) -> None:
        self.gateway.archive_failures_remaining = 1
        first = self._run()
        self.assertEqual([], self.gateway.archived_threads)

        replay = self._run()

        self.assertEqual(first, replay)
        self.assertEqual(1, self.gateway.fork_count)
        self.assertEqual(["fork-1"], self.gateway.archived_threads)

    def test_missing_exact_boundary_fails_the_existing_operation(self) -> None:
        from zdecision.app_server.requested_capture import (
            CaptureAttemptRetryable,
            SourceBoundaryUnavailable,
        )

        self.gateway.drop_first_fork_response = True
        with self.assertRaises(CaptureAttemptRetryable):
            self._run()
        self.gateway.boundary_available = False

        with self.assertRaises(SourceBoundaryUnavailable):
            self._run()

        operation = self.operation_store.operation_for_source(
            REQUEST_ID, self.source.source_key
        )
        self.assertEqual("failed_terminal", operation.status)
        self.assertEqual(
            "source_boundary_unavailable", operation.failure_code
        )

    def test_later_source_activity_cannot_move_the_frozen_boundary(self) -> None:
        later_turn = "019fb100-0000-7000-8000-000000000099"
        self.gateway.latest_turn_id = later_turn

        self._run()

        self.assertEqual(
            [(SOURCE_SESSION, SOURCE_TURN)],
            self.gateway.boundary_calls,
        )


if __name__ == "__main__":
    unittest.main()
