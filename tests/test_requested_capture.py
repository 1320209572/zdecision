from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_inventory import VALID_INVENTORY
from zdecision.agent.session_index import FrozenSessionSource
from zdecision.app_server.jsonl import AppServerTimeout
from zdecision.app_server.models import (
    AppServerTurnReceipt,
    FeasibilityModelProfile,
    SourceBoundary,
)
from zdecision.capture.service import CaptureService
from zdecision.capture.templates import TemplateCatalog
from zdecision.private_store.filesystem import FilePrivateStore


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPOSITORY_ROOT / "decision-templates"
ENVELOPE_ROOT = REPOSITORY_ROOT / "src" / "zdecision" / "capture" / "prompt_contracts"
REQUEST_ID = "crq_11111111111111111111111111111111"
SOURCE_SESSION = "019fb100-0000-7000-8000-000000000001"
SOURCE_TURN = "019fb100-0000-7000-8000-000000000002"
FORK_THREAD = "019fb100-0000-7000-8000-000000000003"
INVENTORY_TURN = "019fb100-0000-7000-8000-000000000004"
EXTRACTION_TURN = "019fb100-0000-7000-8000-000000000005"


class FakeGateway:
    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.interactive_ids = frozenset((SOURCE_SESSION,))
        self.boundary_cwd = cwd
        self.profile = FeasibilityModelProfile.create(
            model_id="model-default",
            reasoning_effort="medium",
            discovery_digest="a" * 64,
            discovered_at="2026-07-30T12:00:00.000000Z",
        )
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
        self.created_fork_count = 0
        self.inventory_turn_count = 0
        self.extraction_turn_count = 0
        self.fail_after_external_fork = False
        self.fail_after_external_turn: str | None = None
        self._threads_by_source: dict[str, str] = {}
        self._turns_by_client_id: dict[str, AppServerTurnReceipt] = {}

    def list_interactive_thread_ids(self, cwd: str) -> frozenset[str]:
        if cwd != self.cwd:
            return frozenset()
        return self.interactive_ids

    def read_completed_boundary(
        self, thread_id: str, turn_id: str
    ) -> SourceBoundary:
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
        return self.profile

    def find_thread_by_source(
        self, thread_source: str, *, cwd: str | None = None
    ) -> str | None:
        return self._threads_by_source.get(thread_source)

    def fork_ephemeral(
        self,
        thread_id: str,
        last_turn_id: str,
        *,
        thread_source: str | None = None,
    ) -> str:
        assert thread_source is not None
        self.created_fork_count += 1
        self._threads_by_source[thread_source] = FORK_THREAD
        if self.fail_after_external_fork:
            raise AppServerTimeout("transport result unknown")
        return FORK_THREAD

    def run_structured_turn(
        self,
        thread_id: str,
        prompt: str,
        output_schema,
        profile: FeasibilityModelProfile,
        cwd: str,
        *,
        client_user_message_id: str | None = None,
    ) -> AppServerTurnReceipt:
        assert client_user_message_id is not None
        if client_user_message_id.endswith("/inventory"):
            stage = "inventory"
            turn_id = INVENTORY_TURN
            output = VALID_INVENTORY
            self.inventory_turn_count += 1
        elif client_user_message_id.endswith("/extraction"):
            stage = "extraction"
            turn_id = EXTRACTION_TURN
            output = self.extraction_output
            self.extraction_turn_count += 1
        else:
            raise AssertionError("unexpected client user message id")
        self.stage_names.append(stage)
        receipt = AppServerTurnReceipt.create(
            thread_id=thread_id,
            turn_id=turn_id,
            structured_output=output,
            model_profile_id=profile.profile_id,
        )
        self._turns_by_client_id[client_user_message_id] = receipt
        if self.fail_after_external_turn == stage:
            raise AppServerTimeout("transport result unknown")
        return receipt

    def read_structured_turn_by_client_id(
        self,
        thread_id: str,
        client_user_message_id: str,
        profile: FeasibilityModelProfile,
    ) -> AppServerTurnReceipt | None:
        return self._turns_by_client_id.get(client_user_message_id)


class RequestedCaptureRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.gateway = FakeGateway(str(self.root))
        self.capture_service = CaptureService(
            FilePrivateStore(self.root / "private"),
            TemplateCatalog(TEMPLATE_ROOT, ENVELOPE_ROOT),
        )
        try:
            from zdecision.agent.request_state import RequestStateStore
            from zdecision.app_server.requested_capture import (
                RequestedCaptureRunner,
            )
        except ModuleNotFoundError as error:
            self.fail(f"Requested Capture API is missing: {error}")
        self.request_state = RequestStateStore.open(self.root / "agent.sqlite3")
        self.addCleanup(self.request_state.close)
        self.runner = RequestedCaptureRunner(
            gateway=self.gateway,
            capture_service=self.capture_service,
            request_state=self.request_state,
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

    def test_request_runs_inventory_then_extraction_without_assessment(
        self,
    ) -> None:
        with patch(
            "zdecision.capture.eligibility.capture_eligible",
            side_effect=AssertionError("eligibility must not run"),
        ):
            result = self.runner.run(
                self.source,
                product_name="ZDecision",
                template_id="business",
            )

        self.assertEqual(("inventory", "extraction"), tuple(self.gateway.stage_names))
        self.assertNotIn("eligibility", self.gateway.stage_names)
        self.assertEqual(
            ("cand_" + result.capture_operation_id[4:] + "_01",),
            tuple(item.candidate_id for item in result.observations),
        )
        self.assertEqual("completed", result.status)
        self.assertEqual(64, len(result.evidence_digest))

    def test_zero_candidates_is_success(self) -> None:
        self.gateway.extraction_output = {"candidates": []}

        result = self.runner.run(
            self.source,
            product_name="ZDecision",
            template_id="business",
        )

        self.assertEqual((), result.observations)
        self.assertEqual("completed", result.status)

    def test_noninteractive_or_wrong_cwd_source_is_excluded(self) -> None:
        from zdecision.app_server.requested_capture import SourceNotInteractive

        self.gateway.interactive_ids = frozenset()
        with self.assertRaises(SourceNotInteractive):
            self.runner.run(
                self.source,
                product_name="ZDecision",
                template_id="business",
            )

        self.gateway.interactive_ids = frozenset((SOURCE_SESSION,))
        self.gateway.boundary_cwd = str(self.root / "other")
        with self.assertRaises(SourceNotInteractive):
            self.runner.run(
                self.source,
                product_name="ZDecision",
                template_id="business",
            )

    def test_retry_adopts_unknown_fork_by_stable_tag(self) -> None:
        from zdecision.agent.request_state import CaptureResultUnknown

        self.gateway.fail_after_external_fork = True
        with self.assertRaises(CaptureResultUnknown):
            self.runner.run(
                self.source,
                product_name="ZDecision",
                template_id="business",
            )
        self.gateway.fail_after_external_fork = False

        result = self.runner.run(
            self.source,
            product_name="ZDecision",
            template_id="business",
        )

        self.assertEqual(1, self.gateway.created_fork_count)
        self.assertEqual(1, self.gateway.inventory_turn_count)
        self.assertEqual("completed", result.status)

    def test_retry_adopts_unknown_stage_turn_by_client_id(self) -> None:
        from zdecision.agent.request_state import CaptureResultUnknown

        self.gateway.fail_after_external_turn = "inventory"
        with self.assertRaises(CaptureResultUnknown):
            self.runner.run(
                self.source,
                product_name="ZDecision",
                template_id="business",
            )
        self.gateway.fail_after_external_turn = None

        result = self.runner.run(
            self.source,
            product_name="ZDecision",
            template_id="business",
        )

        self.assertEqual(1, self.gateway.created_fork_count)
        self.assertEqual(1, self.gateway.inventory_turn_count)
        self.assertEqual(1, self.gateway.extraction_turn_count)
        self.assertEqual("completed", result.status)


if __name__ == "__main__":
    unittest.main()
