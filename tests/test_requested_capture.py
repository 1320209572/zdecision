from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from tests.test_inventory import VALID_INVENTORY
from zdecision.agent.capture_operation_store import CaptureOperationStore
from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import HookInvocation, RepositorySnapshot
from zdecision.agent.recall_host_state import RecallGateConflict, RecallHostStore
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
from zdecision.capture.on_demand import FrozenCaptureInput, FrozenCaptureRouteContext
from zdecision.capture.templates import TemplateCatalog
from zdecision.ids import on_demand_capture_operation_id
from zdecision.jsonio import canonical_json_bytes


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPOSITORY_ROOT / "decision-templates"
ENVELOPE_ROOT = (
    REPOSITORY_ROOT / "src" / "zdecision" / "capture" / "prompt_contracts"
)
REQUEST_ID = "crq_11111111111111111111111111111111"
SOURCE_SESSION = "019fb100-0000-7000-8000-000000000001"
SOURCE_TURN = "019fb100-0000-7000-8000-000000000002"
MATCHED_PATHS = ("packages/shared/theme/src/index.ts",)
REPOSITORY_ID = "repo_33333333333333333333333333333333"
NOW = datetime(2026, 8, 7, 4, 0, tzinfo=UTC)


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
        self.prompts: list[str] = []
        self.schemas: list[dict[str, object]] = []
        self.binding_store: RecallHostStore | None = None
        self.invalid_inventory_receipt = False
        self.invalid_extraction_ordinal = False

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
        self.prompts.append(prompt)
        self.schemas.append(output_schema)
        if self.binding_store is not None and not self.binding_store.is_internal_thread(
            thread_id
        ):
            raise AssertionError("structured Turn started before internal binding")
        properties = output_schema.get("properties", {})
        if "signals" in properties:
            stage = "inventory"
            self.inventory_count += 1
            count = self.inventory_count
            output = deepcopy(VALID_INVENTORY)
            signal_properties = properties["signals"]["items"]["properties"]
            if "evidence_receipt_ids" in signal_properties:
                receipts = signal_properties["evidence_receipt_ids"]["items"][
                    "enum"
                ]
                output["signals"][0]["signal_ordinal"] = 1
                output["signals"][0]["evidence_receipt_ids"] = [receipts[0]]
                if self.invalid_inventory_receipt:
                    output["signals"][0]["evidence_receipt_ids"] = [
                        "rcpt_" + "f" * 64
                    ]
            should_drop = self.drop_first_inventory_result and count == 1
        elif "candidates" in properties:
            stage = "extraction"
            self.extraction_count += 1
            count = self.extraction_count
            output = deepcopy(self.extraction_output)
            candidate_properties = properties["candidates"]["items"][
                "properties"
            ]
            if "source_signal_ordinal" in candidate_properties:
                ordinal = candidate_properties["source_signal_ordinal"]["enum"][0]
                for candidate in output["candidates"]:
                    candidate["source_signal_ordinal"] = ordinal
                    if self.invalid_extraction_ordinal:
                        candidate["source_signal_ordinal"] = ordinal + 99
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
        self.state_path = self.root / "agent.sqlite3"
        self.evidence_ledger = AgentDatabase.open(self.state_path)
        self.addCleanup(self.evidence_ledger.close)
        self.recall_host_store = RecallHostStore.open(self.state_path)
        self.addCleanup(self.recall_host_store.close)
        self.gateway.binding_store = self.recall_host_store
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
            evidence_ledger=self.evidence_ledger,
            recall_host_store=self.recall_host_store,
        )
        repository = RepositorySnapshot(
            REPOSITORY_ID, str(self.root), "main", "d" * 40
        )
        prompt = self._record_event(
            "UserPromptSubmit", SOURCE_TURN, repository, "2026-08-07T04:00:00Z"
        )
        stop = self._record_event(
            "Stop", SOURCE_TURN, repository, "2026-08-07T04:00:01Z"
        )
        self.prompt_event_id = prompt.event_id
        self.stop_event_id = stop.event_id
        self.source = FrozenSessionSource(
            request_id=REQUEST_ID,
            source_key="src_22222222222222222222222222222222",
            repository_id=REPOSITORY_ID,
            session_id=SOURCE_SESSION,
            cwd=str(self.root),
            lineage="lin_44444444444444444444444444444444",
            previous_handled_turn_id=None,
            upper_turn_id=SOURCE_TURN,
            source_fingerprint="5" * 64,
            previous_handled_event_id=None,
            upper_stop_event_id=self.stop_event_id,
        )
        self.route_context = FrozenCaptureRouteContext(
            decision_space_id="dsp_" + "6" * 32,
            decision_space_kind="shared_unit",
            decision_space_name="Shared / packages/shared/theme",
            route_id="drr_" + "7" * 32,
            route_configuration_version=1,
            compatibility_product_id="prod_" + "8" * 32,
            matched_path_digest=(
                "55608e3199e65049bb726efb3ae14f1a"
                "08a9040d0a988ab5320c6ad390cf32d0"
            ),
        )
        self.gateway.extraction_output["candidates"][0]["product"] = (
            self.route_context.decision_space_name
        )

    def _record_event(
        self,
        event_name: str,
        turn_id: str,
        repository: RepositorySnapshot,
        occurred_at: str,
    ):
        invocation = HookInvocation.from_dict(
            {
                "hook_event_name": event_name,
                "session_id": SOURCE_SESSION,
                "turn_id": turn_id,
                "cwd": str(self.root),
                **({"prompt": "never persisted"} if event_name == "UserPromptSubmit" else {}),
            },
            occurred_at=occurred_at,
            repository=repository,
        )
        return self.evidence_ledger.record_hook(invocation)

    def _legacy_operation(self, *, record_version: int = 4, route_context=None):
        template = self.catalog.render("business", self.route_context.decision_space_name)
        context = self.route_context if route_context is None else route_context
        identity = {
            "protocol": f"extractor-v{record_version}",
            "request_id": self.source.request_id,
            "repository_id": self.source.repository_id,
            "source_key": self.source.source_key,
            "session_id": self.source.session_id,
            "cwd": self.source.cwd,
            "lineage": self.source.lineage,
            "previous_handled_turn_id": self.source.previous_handled_turn_id,
            "upper_turn_id": self.source.upper_turn_id,
            "source_fingerprint": self.source.source_fingerprint,
            "product": context.decision_space_name,
            "template": template.to_dict(),
            "model_profile_id": self.request_profile.profile_id,
            "model_id": self.request_profile.model_id,
            "reasoning_effort": self.request_profile.reasoning_effort,
            "model_discovery_digest": self.request_profile.discovery_digest,
            "model_discovered_at": self.request_profile.discovered_at,
        }
        if record_version == 4:
            identity["route_context"] = context.to_dict()
        frozen = FrozenCaptureInput(
            record_version=record_version,
            protocol_revision=f"extractor-v{record_version}",
            operation_id=on_demand_capture_operation_id(identity),
            request_id=self.source.request_id,
            repository_id=self.source.repository_id,
            source_key=self.source.source_key,
            session_id=self.source.session_id,
            cwd=self.source.cwd,
            lineage=self.source.lineage,
            previous_handled_turn_id=self.source.previous_handled_turn_id,
            upper_turn_id=self.source.upper_turn_id,
            source_fingerprint=self.source.source_fingerprint,
            product=context.decision_space_name,
            template=template,
            model_profile_id=self.request_profile.profile_id,
            model_id=self.request_profile.model_id,
            reasoning_effort=self.request_profile.reasoning_effort,
            model_discovery_digest=self.request_profile.discovery_digest,
            model_discovered_at=self.request_profile.discovered_at,
            route_context=None if record_version == 3 else context,
        )
        return self.operation_store.ensure_operation(frozen)

    def _run(self):
        return self.runner.run(
            self.source,
            route_context=self.route_context,
            matched_paths=MATCHED_PATHS,
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
            route_context=self.route_context,
            matched_paths=MATCHED_PATHS,
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

    def test_missing_event_boundaries_fail_before_operation_or_fork(self) -> None:
        from zdecision.app_server.requested_capture import SourceEvidenceUnavailable

        self.source = replace(self.source, upper_stop_event_id=None)

        with self.assertRaises(SourceEvidenceUnavailable):
            self._run()

        self.assertEqual(0, self.gateway.fork_count)
        self.assertIsNone(
            self.operation_store.operation_for_source(
                REQUEST_ID, self.source.source_key, self.route_context.decision_space_id
            )
        )

    def test_empty_prompt_anchor_window_fails_before_fork(self) -> None:
        from zdecision.app_server.requested_capture import SourceEvidenceUnavailable

        with self.evidence_ledger._connection:
            self.evidence_ledger._connection.execute(
                "DELETE FROM events WHERE event_id = ?", (self.prompt_event_id,)
            )

        with self.assertRaises(SourceEvidenceUnavailable):
            self._run()

        self.assertEqual(0, self.gateway.fork_count)

    def test_untrusted_recall_gate_state_fails_before_fork(self) -> None:
        from zdecision.app_server.requested_capture import SourceEvidenceUnavailable

        cases = (("pending", None), ("committed", None))
        for state, reference_version in cases:
            with self.subTest(state=state, reference_version=reference_version):
                with self.recall_host_store._connection:
                    self.recall_host_store._connection.execute(
                        "DELETE FROM recall_turn_gates"
                    )
                    self.recall_host_store._connection.execute(
                        """
                        INSERT INTO recall_turn_gates(
                            gate_id, session_id, turn_id, context_epoch,
                            intent_epoch, active_generation, state,
                            result_digest, commit_fingerprint,
                            active_set_digest, reference_state_version,
                            plugin_root, plugin_bundle_digest
                        ) VALUES (?, ?, ?, 0, 0, NULL, ?, ?, ?, ?, ?, NULL, NULL)
                        """,
                        (
                            f"gate-{state}",
                            SOURCE_SESSION,
                            SOURCE_TURN,
                            state,
                            "a" * 64 if state == "committed" else None,
                            "b" * 64 if state == "committed" else None,
                            "c" * 64 if state == "committed" else None,
                            reference_version,
                        ),
                    )

                with self.assertRaises(SourceEvidenceUnavailable):
                    self._run()

                self.assertEqual(0, self.gateway.fork_count)

    def test_invalid_committed_reference_digest_fails_before_fork(self) -> None:
        from zdecision.app_server.requested_capture import SourceEvidenceUnavailable

        with self.recall_host_store._connection:
            self.recall_host_store._connection.execute(
                """
                INSERT INTO recall_turn_gates(
                    gate_id, session_id, turn_id, context_epoch,
                    intent_epoch, active_generation, state,
                    result_digest, commit_fingerprint,
                    active_set_digest, reference_state_version,
                    plugin_root, plugin_bundle_digest
                ) VALUES ('gate-invalid-digest', ?, ?, 0, 0, NULL,
                    'committed', ?, ?, 'not-a-digest', 1, NULL, NULL)
                """,
                (SOURCE_SESSION, SOURCE_TURN, "a" * 64, "b" * 64),
            )

        with self.assertRaises(SourceEvidenceUnavailable):
            self._run()

        self.assertEqual(0, self.gateway.fork_count)

    def test_manifest_copies_only_committed_v1_reference_digest(self) -> None:
        active_digest = "c" * 64
        with self.recall_host_store._connection:
            self.recall_host_store._connection.execute(
                """
                INSERT INTO recall_turn_gates(
                    gate_id, session_id, turn_id, context_epoch,
                    intent_epoch, active_generation, state,
                    result_digest, commit_fingerprint,
                    active_set_digest, reference_state_version,
                    plugin_root, plugin_bundle_digest
                ) VALUES ('gate-valid-digest', ?, ?, 0, 0, NULL,
                    'committed', ?, ?, ?, 1, NULL, NULL)
                """,
                (SOURCE_SESSION, SOURCE_TURN, "a" * 64, "b" * 64, active_digest),
            )

        self._run()

        operation = self.operation_store.operation_for_source(
            REQUEST_ID, self.source.source_key, self.route_context.decision_space_id
        )
        self.assertEqual(
            active_digest,
            operation.frozen.evidence_manifest.anchors[0].active_reference_set_digest,
        )

    def test_v5_manifest_is_immutable_across_retry_restart_and_later_events(
        self,
    ) -> None:
        from zdecision.app_server.requested_capture import (
            CaptureAttemptRetryable,
            RequestedCaptureRunner,
        )

        self.gateway.drop_first_fork_response = True
        with self.assertRaises(CaptureAttemptRetryable):
            self._run()
        operation = self.operation_store.operation_for_source(
            REQUEST_ID, self.source.source_key, self.route_context.decision_space_id
        )
        manifest_bytes = canonical_json_bytes(
            operation.frozen.evidence_manifest.to_dict()
        )
        repository = RepositorySnapshot(
            REPOSITORY_ID, str(self.root), "main", "d" * 40
        )
        self._record_event(
            "UserPromptSubmit",
            "019fb100-0000-7000-8000-000000000099",
            repository,
            "2026-08-07T04:00:02Z",
        )
        self._record_event(
            "Stop",
            "019fb100-0000-7000-8000-000000000099",
            repository,
            "2026-08-07T04:00:03Z",
        )
        self.runner = RequestedCaptureRunner(
            gateway=self.gateway,
            operation_store=self.operation_store,
            template_catalog=self.catalog,
            evidence_ledger=self.evidence_ledger,
            recall_host_store=self.recall_host_store,
        )

        self._run()

        replayed = self.operation_store.operation_for_source(
            REQUEST_ID, self.source.source_key, self.route_context.decision_space_id
        )
        self.assertEqual(
            manifest_bytes,
            canonical_json_bytes(replayed.frozen.evidence_manifest.to_dict()),
        )
        self.assertEqual(1, len(replayed.frozen.evidence_manifest.anchors))

    def test_legacy_operation_resumes_without_v5_manifest_or_sidecars(self) -> None:
        operation = self._legacy_operation()

        result = self._run()
        replay = self._run()

        self.assertEqual(operation.operation_id, result.capture_operation_id)
        self.assertEqual(result, replay)
        self.assertEqual(1, self.gateway.fork_count)
        self.assertEqual("extractor-v4", result.protocol_revision)
        self.assertEqual((), result.signal_provenance)
        self.assertEqual((), result.candidate_provenance)
        inventory_schema = self.gateway.schemas[0]
        signal_properties = inventory_schema["properties"]["signals"]["items"][
            "properties"
        ]
        self.assertNotIn("evidence_receipt_ids", signal_properties)

    def test_v3_operation_resumes_without_manifest_reinterpretation(self) -> None:
        operation = self._legacy_operation(record_version=3)

        self.assertEqual(
            self.request_profile,
            self.runner.operation_profile(self.source, self.route_context),
        )
        result = self._run()

        self.assertEqual(operation.operation_id, result.capture_operation_id)
        self.assertEqual("extractor-v3", result.protocol_revision)
        self.assertEqual((), result.signal_provenance)
        self.assertEqual((), result.candidate_provenance)

    def test_legacy_request_cannot_create_a_v5_sibling_operation(self) -> None:
        from zdecision.app_server.requested_capture import RequestedCaptureFailed

        self._legacy_operation()
        sibling_context = FrozenCaptureRouteContext(
            decision_space_id="dsp_" + "9" * 32,
            decision_space_kind="shared_unit",
            decision_space_name="Shared / sibling",
            route_id="drr_" + "a" * 32,
            route_configuration_version=1,
            compatibility_product_id="prod_" + "b" * 32,
            matched_path_digest=self.route_context.matched_path_digest,
        )

        with self.assertRaisesRegex(
            RequestedCaptureFailed, "legacy_capture_protocol_mixed"
        ):
            self.runner.run(
                self.source,
                route_context=sibling_context,
                matched_paths=MATCHED_PATHS,
                template_id="business",
                model_profile=self.request_profile,
            )

        self.assertEqual(0, self.gateway.fork_count)

    def test_v5_schemas_are_bounded_to_manifest_and_eligible_signals(self) -> None:
        self._run()

        operation = self.operation_store.operation_for_source(
            REQUEST_ID, self.source.source_key, self.route_context.decision_space_id
        )
        receipt_enum = self.gateway.schemas[0]["properties"]["signals"]["items"][
            "properties"
        ]["evidence_receipt_ids"]["items"]["enum"]
        ordinal_enum = self.gateway.schemas[1]["properties"]["candidates"]["items"][
            "properties"
        ]["source_signal_ordinal"]["enum"]
        self.assertEqual(
            [operation.frozen.evidence_manifest.anchors[0].receipt_id], receipt_enum
        )
        self.assertEqual([1], ordinal_enum)
        manifest_section = self.gateway.prompts[0].split(
            "ZDECISION_FROZEN_PROMPT_ANCHOR_MANIFEST\n", 1
        )[1].split("\nEND_ZDECISION_FROZEN_PROMPT_ANCHOR_MANIFEST", 1)[0]
        payload = json.loads(manifest_section.splitlines()[-1])
        self.assertEqual(
            {
                "anchors": [
                    {
                        "receipt_id": receipt_enum[0],
                        "anchor_ordinal": 1,
                        "active_reference_set_digest": None,
                    }
                ]
            },
            payload,
        )
        self.assertNotIn(self.prompt_event_id, manifest_section)
        self.assertNotIn(SOURCE_TURN, manifest_section)

    def test_invalid_v5_receipt_terminalizes_once_without_model_retry(self) -> None:
        from zdecision.app_server.requested_capture import (
            RequestedCaptureFailed,
            SourceBoundaryUnavailable,
        )

        self.gateway.invalid_inventory_receipt = True
        with self.assertRaisesRegex(
            RequestedCaptureFailed, "capture_provenance_invalid"
        ):
            self._run()
        counts = (self.gateway.fork_count, self.gateway.inventory_count)

        with self.assertRaises(SourceBoundaryUnavailable):
            self._run()

        operation = self.operation_store.operation_for_source(
            REQUEST_ID, self.source.source_key, self.route_context.decision_space_id
        )
        self.assertEqual("failed_terminal", operation.status)
        self.assertEqual("capture_provenance_invalid", operation.failure_code)
        self.assertEqual(counts, (self.gateway.fork_count, self.gateway.inventory_count))

    def test_invalid_v5_signal_link_terminalizes_without_retry(self) -> None:
        from zdecision.app_server.requested_capture import RequestedCaptureFailed

        self.gateway.invalid_extraction_ordinal = True

        with self.assertRaisesRegex(
            RequestedCaptureFailed, "capture_provenance_invalid"
        ):
            self._run()

        operation = self.operation_store.operation_for_source(
            REQUEST_ID, self.source.source_key, self.route_context.decision_space_id
        )
        self.assertEqual("failed_terminal", operation.status)
        self.assertEqual("capture_provenance_invalid", operation.failure_code)
        self.assertEqual(1, self.gateway.fork_count)
        self.assertEqual(1, self.gateway.extraction_count)

    def test_capture_thread_is_bound_before_first_turn_and_recall_denied(self) -> None:
        self._run()

        row = self.recall_host_store._connection.execute(
            "SELECT * FROM recall_internal_threads WHERE thread_id = 'fork-1'"
        ).fetchone()
        self.assertEqual("capture", row["purpose"])
        self.assertEqual(SOURCE_SESSION, row["parent_thread_id"])
        with self.assertRaises(RecallGateConflict):
            self.recall_host_store.bind_activation(
                session_id="fork-1",
                turn_id="capture-turn",
                cwd=str(self.root),
                binding_id="capture-activation",
                now=NOW,
            )

    def test_capture_binding_failure_archives_before_any_structured_turn(self) -> None:
        from zdecision.app_server.requested_capture import RequestedCaptureFailed

        with patch.object(
            self.recall_host_store,
            "bind_internal_thread",
            side_effect=RecallGateConflict("binding unavailable"),
        ):
            with self.assertRaises(RequestedCaptureFailed):
                self._run()

        self.assertEqual(0, self.gateway.inventory_count)
        self.assertEqual(["fork-1"], self.gateway.archived_threads)

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
                route_context=self.route_context,
                matched_paths=MATCHED_PATHS,
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

    def test_extraction_is_fixed_to_one_leaf_and_its_matched_paths(self) -> None:
        self._run()

        operation = self.operation_store.operation_for_source(
            REQUEST_ID, self.source.source_key
        )
        self.assertEqual(self.route_context, operation.frozen.route_context)
        extraction_prompt = self.gateway.prompts[1]
        self.assertIn(self.route_context.decision_space_id, extraction_prompt)
        self.assertIn(MATCHED_PATHS[0], extraction_prompt)
        self.assertIn(
            self.route_context.decision_space_name, extraction_prompt
        )


if __name__ == "__main__":
    unittest.main()
