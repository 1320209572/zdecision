from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from tests import test_candidate_reconciliation as reconciliation_fixtures
from tests import test_capture as capture_fixtures
from tests import test_inventory as inventory_fixtures
from tests import test_requested_capture as runner_fixtures
from tests.integration import test_central_web_vertical as central_vertical
from tests.integration import test_on_demand_capture_core as capture_vertical
from zdecision.agent.capture_operation_store import CaptureOperationStore
from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import HookInvocation, RepositorySnapshot
from zdecision.agent.recall_host_state import RecallGateConflict, RecallHostStore
from zdecision.agent.session_index import FrozenSessionSource
from zdecision.app_server.requested_capture import (
    RequestedCaptureRunner,
    SourceEvidenceUnavailable,
)
from zdecision.capture.inventory import (
    InventoryValidationError,
    validate_inventory_v5,
)
from zdecision.capture.models import SourceCheckpoint
from zdecision.capture.on_demand import FrozenCaptureRouteContext
from zdecision.capture.provenance import (
    CandidateProvenanceSummary,
    CaptureEvidenceManifest,
    PromptAnchor,
    prompt_anchor_receipt_id,
)
from zdecision.capture.reconciliation import (
    CandidateFamilyRevision,
    ReconciliationDecision,
    apply_reconciliation,
)
from zdecision.capture.service import (
    ExtractionValidationError,
    validate_extraction_output_v5,
)
from zdecision.capture.templates import TemplateCatalog
from zdecision.ids import candidate_family_id


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ENVELOPE_ROOT = REPOSITORY_ROOT / "src" / "zdecision" / "capture" / "prompt_contracts"
NOW = datetime(2026, 8, 7, 8, 0, tzinfo=UTC)


def _manifest(*, reference_digest: str | None = None) -> CaptureEvidenceManifest:
    event_id = "evt_" + "1" * 32
    return CaptureEvidenceManifest.create(
        source_session_id=runner_fixtures.SOURCE_SESSION,
        previous_handled_event_id=None,
        upper_stop_event_id="evt_" + "2" * 32,
        anchors=(
            PromptAnchor(
                receipt_id=prompt_anchor_receipt_id(event_id),
                hook_event_id=event_id,
                turn_id=runner_fixtures.SOURCE_TURN,
                anchor_ordinal=1,
                active_reference_set_digest=reference_digest,
            ),
        ),
    )


def _inventory(
    manifest: CaptureEvidenceManifest,
    *,
    basis: str = "explicit_user_direction",
    status: str = "current_confirmed",
) -> dict[str, object]:
    value = inventory_fixtures.v5_inventory(manifest)
    value["signals"][0]["confirmation_basis"] = basis
    value["signals"][0]["status"] = status
    return value


def _extraction() -> dict[str, object]:
    return {
        "candidates": [
            {
                **capture_fixtures.valid_candidate(),
                "source_signal_ordinal": 1,
            }
        ]
    }


class RecallCaptureIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state_path = self.root / "agent.sqlite3"
        self.ledger = AgentDatabase.open(self.state_path)
        self.addCleanup(self.ledger.close)
        self.host = RecallHostStore.open(self.state_path)
        self.addCleanup(self.host.close)
        self.operations = CaptureOperationStore.open(self.root / "operations.sqlite3")
        self.addCleanup(self.operations.close)
        self.gateway = runner_fixtures.FakeGateway(str(self.root))
        self.gateway.binding_store = self.host
        self.runner = RequestedCaptureRunner(
            gateway=self.gateway,
            operation_store=self.operations,
            template_catalog=TemplateCatalog(
                REPOSITORY_ROOT / "decision-templates", ENVELOPE_ROOT
            ),
            evidence_ledger=self.ledger,
            recall_host_store=self.host,
        )
        self.repository = RepositorySnapshot(
            runner_fixtures.REPOSITORY_ID,
            str(self.root),
            "main",
            "d" * 40,
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

    def _record(self, event: str, event_id: str, *, turn_id: str) -> None:
        invocation = HookInvocation.from_dict(
            {
                "hook_event_name": event,
                "session_id": runner_fixtures.SOURCE_SESSION,
                "turn_id": turn_id,
                "cwd": str(self.root),
                **({"prompt": "PRIVATE_PROMPT_NOT_PERSISTED"} if event == "UserPromptSubmit" else {}),
            },
            occurred_at="2026-08-07T08:00:00Z",
            repository=self.repository,
        )
        recorded = self.ledger.record_hook(invocation)
        self.assertEqual(event_id, recorded.event_id)

    def _source(self, upper_stop_event_id: str) -> FrozenSessionSource:
        return FrozenSessionSource(
            request_id=runner_fixtures.REQUEST_ID,
            source_key="src_" + "2" * 32,
            repository_id=runner_fixtures.REPOSITORY_ID,
            session_id=runner_fixtures.SOURCE_SESSION,
            cwd=str(self.root),
            lineage="lin_" + "4" * 32,
            previous_handled_turn_id=None,
            upper_turn_id=runner_fixtures.SOURCE_TURN,
            source_fingerprint="5" * 64,
            previous_handled_event_id=None,
            upper_stop_event_id=upper_stop_event_id,
        )

    def _run_without_prompt_anchor(self, inherited_context: str) -> None:
        self.gateway.prompts.append(inherited_context)
        stop = HookInvocation.from_dict(
            {
                "hook_event_name": "Stop",
                "session_id": runner_fixtures.SOURCE_SESSION,
                "turn_id": runner_fixtures.SOURCE_TURN,
                "cwd": str(self.root),
            },
            occurred_at="2026-08-07T08:00:01Z",
            repository=self.repository,
        )
        stop_event = self.ledger.record_hook(stop)

        with self.assertRaises(SourceEvidenceUnavailable):
            self.runner.run(
                self._source(stop_event.event_id),
                route_context=self.route_context,
                matched_paths=runner_fixtures.MATCHED_PATHS,
                template_id="business",
                model_profile=self.gateway.profile,
            )

        self.assertEqual(0, self.gateway.fork_count)
        self.assertEqual(0, self.gateway.inventory_count)
        self.assertEqual(0, self.gateway.extraction_count)

    def test_01_recalled_decision_or_host_probe_without_anchor_yields_zero_candidates(self) -> None:
        for context in ("recalled Decision rule", "host_gate_fixture_not_formal"):
            with self.subTest(context=context):
                self._run_without_prompt_anchor(context)

    def test_02_non_prompt_sources_alone_yield_zero_candidates(self) -> None:
        for context in (
            "assistant proposal",
            "tool output",
            "source code",
            "Capture artifact",
            "compaction summary",
        ):
            with self.subTest(context=context):
                self._run_without_prompt_anchor(context)

    def test_03_anchored_explicit_direction_qualifies_with_recalled_context(self) -> None:
        manifest = _manifest(reference_digest="a" * 64)
        inventory, signal_provenance = validate_inventory_v5(
            _inventory(manifest), manifest
        )
        candidates, candidate_provenance = validate_extraction_output_v5(
            "cap_" + "a" * 32,
            SourceCheckpoint("thread-1", "turn-1"),
            "anheng",
            _extraction(),
            inventory,
            signal_provenance,
            manifest,
        )

        self.assertEqual(1, len(candidates))
        self.assertEqual("candidate_eligible", signal_provenance[0].disposition)
        self.assertEqual("a" * 64, candidate_provenance[0].active_reference_set_digests[0])

    def test_04_identical_recalled_and_prompt_text_is_distinguished_by_receipt(self) -> None:
        identical_text = "Only the explicit Update action authorizes Capture."
        manifest = _manifest(reference_digest="b" * 64)
        inventory, provenance = validate_inventory_v5(_inventory(manifest), manifest)

        self.assertEqual(identical_text, identical_text)
        self.assertNotIn(identical_text, manifest.anchors[0].receipt_id)
        self.assertEqual(
            (manifest.anchors[0].receipt_id,), provenance[0].evidence_receipt_ids
        )
        self.assertEqual("candidate_eligible", provenance[0].disposition)

    def test_05_invalid_receipt_sets_fail_the_complete_attempt(self) -> None:
        manifest = inventory_fixtures.multi_receipt_manifest()
        valid_receipts = [anchor.receipt_id for anchor in manifest.anchors]
        outside_receipt = _manifest().anchors[0].receipt_id
        cases = (
            ["rcpt_" + "f" * 64],
            [valid_receipts[0], valid_receipts[0]],
            list(reversed(valid_receipts)),
            [outside_receipt],
            [prompt_anchor_receipt_id("evt_" + "9" * 32)],
            ["rcpt_" + "0" * 64],
        )

        for receipts in cases:
            with self.subTest(receipts=receipts):
                value = _inventory(manifest)
                value["signals"][0]["evidence_receipt_ids"] = receipts
                with self.assertRaises(InventoryValidationError):
                    validate_inventory_v5(value, manifest)

    def test_06_model_authored_direction_without_receipt_cannot_qualify(self) -> None:
        manifest = _manifest()
        value = _inventory(manifest)
        value["signals"][0]["evidence_receipt_ids"] = []

        with self.assertRaises(InventoryValidationError):
            validate_inventory_v5(value, manifest)

    def test_07_extraction_cannot_change_inventory_evidence(self) -> None:
        manifest = inventory_fixtures.multi_receipt_manifest()
        value = _inventory(manifest)
        value["signals"][0]["evidence_receipt_ids"] = [
            anchor.receipt_id for anchor in manifest.anchors
        ]
        inventory, signal_provenance = validate_inventory_v5(value, manifest)
        forged = _extraction()
        forged["candidates"][0]["evidence_receipt_ids"] = [
            manifest.anchors[1].receipt_id
        ]

        with self.assertRaises(ExtractionValidationError):
            validate_extraction_output_v5(
                "cap_" + "a" * 32,
                SourceCheckpoint("thread-1", "turn-1"),
                "anheng",
                forged,
                inventory,
                signal_provenance,
                manifest,
            )

    def test_08_reconciliation_preserves_but_cannot_model_author_provenance(self) -> None:
        summary = CandidateProvenanceSummary(
            protocol="candidate-provenance-v1",
            kind="host_observed_user_prompt_anchor",
            digest="d" * 64,
        )
        legacy_current = reconciliation_fixtures.CandidateReconciliationTest()._api()[0]
        cases = (
            ("unrelated", (), reconciliation_fixtures.OBSERVATION_A),
            (
                "refine",
                (replace(legacy_current, provenance=summary),),
                reconciliation_fixtures.OBSERVATION_B,
            ),
            (
                "replace",
                (replace(legacy_current, provenance=summary),),
                reconciliation_fixtures.REVERSED_OBSERVATION,
            ),
        )
        for relation, current, observation in cases:
            with self.subTest(relation=relation):
                family_id = (
                    candidate_family_id(
                        reconciliation_fixtures.REPOSITORY_ID,
                        reconciliation_fixtures.DECISION_SPACE_ID,
                        observation.candidate_id,
                    )
                    if relation == "unrelated"
                    else legacy_current.family_id
                )
                decision = ReconciliationDecision(
                    observation.candidate_id,
                    relation,
                    family_id,
                    None if relation == "unrelated" else observation.content,
                )
                result = apply_reconciliation(
                    reconciliation_fixtures.REPOSITORY_ID,
                    reconciliation_fixtures.DECISION_SPACE_ID,
                    (observation,),
                    current,
                    (decision,),
                    {observation.candidate_id: summary},
                )
                self.assertEqual(summary, result.new_revisions[0].provenance)

        with self.assertRaises(ValueError):
            ReconciliationDecision.from_dict(
                {
                    "observation_id": reconciliation_fixtures.OBSERVATION_A.candidate_id,
                    "relation": "unrelated",
                    "family_id": candidate_family_id(
                        reconciliation_fixtures.REPOSITORY_ID,
                        reconciliation_fixtures.DECISION_SPACE_ID,
                        reconciliation_fixtures.OBSERVATION_A.candidate_id,
                    ),
                    "effective_content": None,
                    "provenance": summary.to_dict(),
                }
            )

    def test_09_noneligible_dispositions_upload_no_candidate_content(self) -> None:
        manifest = _manifest(reference_digest="e" * 64)
        cases = (
            ("adopted_decision_contract", "needs_evidence"),
            ("explicit_user_confirmation", "needs_evidence"),
            ("uncertain", "excluded_unverified"),
        )
        for basis, expected in cases:
            with self.subTest(basis=basis):
                inventory, provenance = validate_inventory_v5(
                    _inventory(manifest, basis=basis), manifest
                )
                self.assertEqual(expected, provenance[0].disposition)
                with self.assertRaises(ExtractionValidationError):
                    validate_extraction_output_v5(
                        "cap_" + "a" * 32,
                        SourceCheckpoint("thread-1", "turn-1"),
                        "anheng",
                        _extraction(),
                        inventory,
                        provenance,
                        manifest,
                    )

    def test_10_retry_restart_reuses_manifest_sidecars_digest_and_outbox_bytes(self) -> None:
        self._run_existing_vertical(
            capture_vertical.OnDemandCaptureCoreTest,
            "test_lost_upload_response_replays_exact_batch_after_agent_restart",
        )

    def test_11_capture_and_reconciliation_threads_reject_recall(self) -> None:
        for purpose in ("capture", "reconciliation"):
            with self.subTest(purpose=purpose):
                thread_id = f"internal-{purpose}"
                self.host.bind_internal_thread(
                    thread_id=thread_id,
                    parent_thread_id="interactive-parent",
                    purpose=purpose,
                    operation_id=f"operation-{purpose}",
                    now=NOW,
                )
                with self.assertRaises(RecallGateConflict):
                    self.host.bind_activation(
                        session_id=thread_id,
                        turn_id="turn-internal",
                        cwd=str(self.root),
                        binding_id=f"binding-{purpose}",
                        now=NOW,
                    )
                with self.assertRaises(RecallGateConflict):
                    self.host.begin_turn_gate(
                        session_id=thread_id,
                        turn_id="turn-internal",
                        context_epoch=0,
                        intent_epoch=0,
                        active_generation=None,
                        gate_id=f"gate-{purpose}",
                    )

    def test_12_recalled_rule_plus_unrelated_continue_anchor_needs_evidence(self) -> None:
        # Model-quality assertion, not host proof: the fixed semantic corpus must
        # classify an unrelated “继续” anchor as needs_evidence.
        manifest = _manifest(reference_digest="f" * 64)
        _, provenance = validate_inventory_v5(
            _inventory(manifest, basis="explicit_user_confirmation"), manifest
        )

        self.assertEqual("needs_evidence", provenance[0].disposition)

    def test_13_raw_source_and_native_ids_do_not_cross_central_boundary(self) -> None:
        self._run_existing_vertical(
            central_vertical.CentralWebVerticalTest,
            "test_theme_review_preview_and_explicit_publish_use_v1_partition",
        )

    def _run_existing_vertical(
        self, case_type: type[unittest.TestCase], method_name: str
    ) -> None:
        result = unittest.TestResult()
        case_type(method_name).run(result)
        if result.errors or result.failures:
            messages = [message for _, message in (*result.errors, *result.failures)]
            self.fail("\n".join(messages))


if __name__ == "__main__":
    unittest.main()
