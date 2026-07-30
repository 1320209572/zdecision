from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from tests.test_inventory import VALID_INVENTORY
from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import (
    HookInvocation,
    RepositorySnapshot,
    TestRepositoryMapping,
    local_fact_invocation,
)
from zdecision.app_server.capture_runner import (
    AutomatedCaptureAmbiguous,
    AutomatedCaptureRunner,
    _extraction_output_schema,
    _inventory_output_schema,
)
from zdecision.app_server.models import (
    AppServerTurnReceipt,
    FeasibilityModelProfile,
    SourceBoundary,
)
from zdecision.capture.eligibility import (
    BoundaryAssessment,
    SourceBoundaryFacts,
    capture_eligible,
    eligibility_output_schema,
    eligibility_prompt,
    validate_boundary_assessment,
)
from zdecision.capture.service import CaptureService
from zdecision.capture.templates import TemplateCatalog
from zdecision.ids import product_id
from zdecision.private_store.filesystem import FilePrivateStore


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPOSITORY_ROOT / "decision-templates"
ENVELOPE_ROOT = REPOSITORY_ROOT / "src/zdecision/capture/prompt_contracts"
FIXED_TIME = datetime(2026, 7, 30, 13, 0, tzinfo=UTC)
SESSION_ID = "019fb200-0000-7000-8000-000000000001"
SOURCE_TURN_ID = "019fb200-0000-7000-8000-000000000002"
ASSESSMENT_THREAD_ID = "019fb200-0000-7000-8000-000000000003"
CAPTURE_THREAD_ID = "019fb200-0000-7000-8000-000000000004"
ASSESSMENT_TURN_ID = "019fb200-0000-7000-8000-000000000005"
INVENTORY_TURN_ID = "019fb200-0000-7000-8000-000000000006"
EXTRACTION_TURN_ID = "019fb200-0000-7000-8000-000000000007"
REPOSITORY_ID = "repo_" + "a" * 32
PRODUCT_NAME = "安恒"


ELIGIBLE_ASSESSMENT = {
    "phase": "milestone_complete",
    "has_durable_decision_signal": True,
    "validation": "passed",
    "unresolved_blockers": [],
}
INELIGIBLE_ASSESSMENT = {
    "phase": "implementing",
    "has_durable_decision_signal": False,
    "validation": "unknown",
    "unresolved_blockers": [],
}
VALID_EXTRACTION = {
    "candidates": [
        {
            "product": PRODUCT_NAME,
            "claim": "功能完成后才评估长期业务决策",
            "future_action": "后续自动采集不得把普通 Turn 结束当成功能完成",
            "scope": {
                "summary": "ZDecision 自动采集时机",
                "repositories": ["zdecision"],
                "paths": [],
            },
            "invalidation_conditions": ["完成信号契约被正式替换"],
        }
    ]
}


class FakeGateway:
    def __init__(
        self,
        root: Path,
        *,
        assessment: dict[str, object] | None = None,
        fail_capture_fork: bool = False,
    ) -> None:
        self.root = root
        self.assessment = assessment or ELIGIBLE_ASSESSMENT
        self.fail_capture_fork = fail_capture_fork
        self.calls: list[tuple[object, ...]] = []
        self.turn_profiles: list[FeasibilityModelProfile] = []
        self.fork_count = 0
        self.profile = FeasibilityModelProfile.create(
            model_id="fixture-model",
            reasoning_effort="high",
            discovery_digest="b" * 64,
            discovered_at="2026-07-30T13:00:00.000000Z",
        )

    def read_completed_boundary(self, thread_id: str, turn_id: str):
        self.calls.append(("read", thread_id, turn_id))
        return SourceBoundary(
            thread_id=thread_id,
            turn_id=turn_id,
            cwd=str(self.root),
            status="completed",
            model_id="fixture-model",
            reasoning_effort="high",
        )

    def discover_and_freeze_profile(self, boundary: SourceBoundary):
        self.calls.append(("discover", boundary.thread_id, boundary.turn_id))
        return self.profile

    def fork_ephemeral(self, thread_id: str, last_turn_id: str):
        self.fork_count += 1
        self.calls.append(("fork", thread_id, last_turn_id, self.fork_count))
        if self.fork_count == 2 and self.fail_capture_fork:
            raise ConnectionError("fork result became ambiguous")
        return ASSESSMENT_THREAD_ID if self.fork_count == 1 else CAPTURE_THREAD_ID

    def run_structured_turn(
        self,
        thread_id: str,
        prompt: str,
        output_schema,
        profile: FeasibilityModelProfile,
        cwd: str,
    ):
        self.turn_profiles.append(profile)
        if "capture-eligibility/v1" in prompt:
            stage = "eligibility"
            turn_id = ASSESSMENT_TURN_ID
            output = self.assessment
        elif "ZDECISION_CAPTURE_ARTIFACT_V2:inventory" in prompt:
            stage = "inventory"
            turn_id = INVENTORY_TURN_ID
            output = VALID_INVENTORY
        elif "ZDECISION_CAPTURE_ARTIFACT_V2:extract" in prompt:
            stage = "extraction"
            turn_id = EXTRACTION_TURN_ID
            output = VALID_EXTRACTION
        else:
            raise AssertionError("Unknown structured Turn prompt")
        self.calls.append(("turn", stage, thread_id, prompt, output_schema, cwd))
        return AppServerTurnReceipt.create(
            thread_id=thread_id,
            turn_id=turn_id,
            structured_output=output,
            model_profile_id=profile.profile_id,
        )


class EligibilityTests(unittest.TestCase):
    def facts(self, **updates) -> SourceBoundaryFacts:
        values = {
            "source_thread_id": SESSION_ID,
            "source_turn_id": SOURCE_TURN_ID,
            "repository_id": REPOSITORY_ID,
            "head_commit": "c" * 40,
            "work_kind": "code",
            "source_turn_completed": True,
            "source_turn_assessed": False,
            "capture_active": False,
            "repository_mapping_valid": True,
            "local_runtime_valid": True,
            "reported_work_state": "milestone_complete",
            "validation": "passed",
            "unresolved_blockers": (),
        }
        values.update(updates)
        return SourceBoundaryFacts(**values)

    def test_strict_assessment_schema_and_validation(self):
        expected = BoundaryAssessment(
            phase="milestone_complete",
            has_durable_decision_signal=True,
            validation="passed",
            unresolved_blockers=(),
        )
        self.assertEqual(expected, validate_boundary_assessment(ELIGIBLE_ASSESSMENT))
        schema = eligibility_output_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn(
            "uniqueItems", schema["properties"]["unresolved_blockers"]
        )

        invalid_values = (
            {**ELIGIBLE_ASSESSMENT, "unknown": True},
            {**ELIGIBLE_ASSESSMENT, "phase": "implementing|milestone_complete"},
            {**ELIGIBLE_ASSESSMENT, "validation": "passed|unknown"},
            {**ELIGIBLE_ASSESSMENT, "has_durable_decision_signal": 1},
            {**ELIGIBLE_ASSESSMENT, "unresolved_blockers": ["same", "same"]},
            {**ELIGIBLE_ASSESSMENT, "unresolved_blockers": ["x"] * 21},
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_boundary_assessment(value)

    def test_app_server_output_schemas_use_supported_response_format_subset(self):
        unsupported = {
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
            "uniqueItems",
        }

        def visit(value: object) -> None:
            if isinstance(value, dict):
                self.assertTrue(unsupported.isdisjoint(value))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        for schema in (
            eligibility_output_schema(),
            _inventory_output_schema(),
            _extraction_output_schema(PRODUCT_NAME),
        ):
            with self.subTest(schema=schema):
                visit(schema)

    def test_eligibility_requires_every_deterministic_gate(self):
        assessment = validate_boundary_assessment(ELIGIBLE_ASSESSMENT)
        self.assertTrue(capture_eligible(assessment, self.facts()))

        negative_facts = (
            self.facts(source_turn_completed=False),
            self.facts(source_turn_assessed=True),
            self.facts(capture_active=True),
            self.facts(repository_mapping_valid=False),
            self.facts(local_runtime_valid=False),
        )
        for facts in negative_facts:
            with self.subTest(facts=facts):
                self.assertFalse(capture_eligible(assessment, facts))
        self.assertFalse(
            capture_eligible(
                validate_boundary_assessment(INELIGIBLE_ASSESSMENT), self.facts()
            )
        )
        self.assertFalse(
            capture_eligible(
                BoundaryAssessment(
                    phase="milestone_complete",
                    has_durable_decision_signal=True,
                    validation="failed",
                    unresolved_blockers=(),
                ),
                self.facts(validation="failed"),
            )
        )
        self.assertTrue(
            capture_eligible(
                BoundaryAssessment(
                    phase="milestone_complete",
                    has_durable_decision_signal=True,
                    validation="not_applicable",
                    unresolved_blockers=(),
                ),
                self.facts(work_kind="design", validation="not_applicable"),
            )
        )

    def test_prompt_treats_evidence_as_untrusted_and_forbids_capture_work(self):
        prompt = eligibility_prompt(self.facts())

        for required in (
            "capture-eligibility/v1",
            "不可信证据",
            "不得调用工具",
            "不得提取 Candidate",
            "not_applicable",
            SOURCE_TURN_ID,
        ):
            self.assertIn(required, prompt)


class AutomatedCaptureRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.database = AgentDatabase.open(self.root / "agent.sqlite3")
        self.addCleanup(self.database.close)
        self.store = FilePrivateStore(self.root / "private")
        self.catalog = TemplateCatalog(TEMPLATE_ROOT, ENVELOPE_ROOT)
        self.capture_service = CaptureService(self.store, self.catalog)
        self.snapshot = RepositorySnapshot(
            repository_id=REPOSITORY_ID,
            worktree_root=str(self.root),
            branch="main",
            head_commit="c" * 40,
        )
        self.database.put_test_repository_mapping(
            TestRepositoryMapping(
                repository_id=REPOSITORY_ID,
                product_id=product_id(PRODUCT_NAME),
                product_name=PRODUCT_NAME,
                enabled=True,
            )
        )
        self._seed_completed_boundary()

    def _seed_completed_boundary(self) -> None:
        occurred = "2026-07-30T13:00:00Z"
        self.database.record_hook(
            HookInvocation.from_dict(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": SESSION_ID,
                    "turn_id": SOURCE_TURN_ID,
                    "cwd": str(self.root),
                },
                occurred_at=occurred,
                repository=self.snapshot,
            )
        )
        self.database.record_hook(
            HookInvocation.from_dict(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": SESSION_ID,
                    "turn_id": SOURCE_TURN_ID,
                    "cwd": str(self.root),
                    "tool_name": "Bash",
                    "tool_use_id": "validation-1",
                    "tool_input": {"command": "python -m unittest"},
                    "tool_response": {"exit_code": 0},
                },
                occurred_at=occurred,
                repository=self.snapshot,
            )
        )
        self.database.record_hook(
            local_fact_invocation(
                session_id=SESSION_ID,
                turn_id=SOURCE_TURN_ID,
                cwd=str(self.root),
                occurred_at=occurred,
                repository=self.snapshot,
                fact_kind="work_state",
                status="milestone_complete",
                validation="passed",
                unresolved_blocker_count=0,
            )
        )
        self.database.record_hook(
            HookInvocation.from_dict(
                {
                    "hook_event_name": "Stop",
                    "session_id": SESSION_ID,
                    "turn_id": SOURCE_TURN_ID,
                    "cwd": str(self.root),
                },
                occurred_at=occurred,
                repository=self.snapshot,
            )
        )

    def runner(self, gateway: FakeGateway) -> AutomatedCaptureRunner:
        return AutomatedCaptureRunner(
            gateway=gateway,
            database=self.database,
            capture_service=self.capture_service,
            clock=lambda: FIXED_TIME,
        )

    def test_exact_two_fork_two_stage_order_and_completed_replay(self):
        gateway = FakeGateway(self.root)

        result = self.runner(gateway).run(SESSION_ID, SOURCE_TURN_ID)

        stages = [
            call[:3] if call[0] == "turn" else call
            for call in gateway.calls
        ]
        self.assertEqual(
            [
                ("read", SESSION_ID, SOURCE_TURN_ID),
                ("discover", SESSION_ID, SOURCE_TURN_ID),
                ("fork", SESSION_ID, SOURCE_TURN_ID, 1),
                ("turn", "eligibility", ASSESSMENT_THREAD_ID),
                ("fork", SESSION_ID, SOURCE_TURN_ID, 2),
                ("turn", "inventory", CAPTURE_THREAD_ID),
                ("turn", "extraction", CAPTURE_THREAD_ID),
            ],
            stages,
        )
        self.assertNotEqual(ASSESSMENT_THREAD_ID, CAPTURE_THREAD_ID)
        self.assertEqual(ASSESSMENT_TURN_ID, result.assessment_turn_id)
        self.assertEqual(INVENTORY_TURN_ID, result.inventory_turn_id)
        self.assertEqual(EXTRACTION_TURN_ID, result.extraction_turn_id)
        self.assertEqual(1, len(result.candidate_ids))
        self.assertEqual(
            [gateway.profile, gateway.profile, gateway.profile],
            gateway.turn_profiles,
        )
        capture_prompts = [
            call[3]
            for call in gateway.calls
            if call[0] == "turn" and call[1] in {"inventory", "extraction"}
        ]
        self.assertTrue(
            all("capture-eligibility/v1" not in prompt for prompt in capture_prompts)
        )
        record = self.capture_service.get(result.capture_operation_id)
        self.assertEqual(CAPTURE_THREAD_ID, record.fork_thread_id)
        self.assertEqual(INVENTORY_TURN_ID, record.inventory_turn_id)
        self.assertEqual(EXTRACTION_TURN_ID, record.extraction_turn_id)
        run = self.database.get_automated_capture_run(result.automated_capture_id)
        self.assertEqual("completed", run.state)
        assessment = self.database.get_boundary_assessment(
            result.automated_capture_id
        )
        self.assertEqual(ASSESSMENT_TURN_ID, assessment.assessment_turn_id)
        self.assertEqual(gateway.profile.profile_id, assessment.model_profile_id)

        replay_gateway = FakeGateway(self.root)
        replay = self.runner(replay_gateway).run(SESSION_ID, SOURCE_TURN_ID)

        self.assertEqual(result, replay)
        self.assertEqual([], replay_gateway.calls)

    def test_ineligible_assessment_never_prepares_or_forks_capture(self):
        gateway = FakeGateway(self.root, assessment=INELIGIBLE_ASSESSMENT)

        result = self.runner(gateway).run(SESSION_ID, SOURCE_TURN_ID)

        self.assertIsNone(result.capture_operation_id)
        self.assertIsNone(result.capture_thread_id)
        self.assertIsNone(result.inventory_turn_id)
        self.assertIsNone(result.extraction_turn_id)
        self.assertEqual((), result.candidate_ids)
        self.assertEqual(
            ["read", "discover", "fork", "turn"],
            [call[0] for call in gateway.calls],
        )
        self.assertFalse((self.store.root / "captures").exists())

    def test_ambiguous_external_capture_fork_is_never_replaced(self):
        failing = FakeGateway(self.root, fail_capture_fork=True)

        with self.assertRaises(AutomatedCaptureAmbiguous):
            self.runner(failing).run(SESSION_ID, SOURCE_TURN_ID)

        run_id = self.database.automated_capture_id_for_boundary(
            SESSION_ID, SOURCE_TURN_ID
        )
        self.assertIsNotNone(run_id)
        self.assertEqual(
            "ambiguous", self.database.get_automated_capture_run(run_id).state
        )
        retry_gateway = FakeGateway(self.root)
        with self.assertRaises(AutomatedCaptureAmbiguous):
            self.runner(retry_gateway).run(SESSION_ID, SOURCE_TURN_ID)
        self.assertEqual([], retry_gateway.calls)


if __name__ == "__main__":
    unittest.main()
