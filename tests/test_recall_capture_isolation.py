from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from tests import test_candidate_reconciliation as reconciliation_fixtures
from tests import test_capture as capture_fixtures
from tests import test_capture_request_processor as processor_fixtures
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
    RequestedCaptureFailed,
    SourceBoundaryUnavailable,
)
from zdecision.app_server.models import AppServerTurnReceipt
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
    SignalProvenance,
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
from zdecision.recall.session import TurnGateResult


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


@dataclass(frozen=True)
class CorpusItem:
    source_channel: str
    text: str
    semantic_role: str
    receipt_id: str | None = None


class DeterministicCorpusModel:
    """Fixed semantic model whose only evidence authority is the manifest."""

    def __init__(self) -> None:
        self.consumed_runs: list[tuple[CorpusItem, ...]] = []
        self.manifest_runs: list[CaptureEvidenceManifest] = []

    def inventory(
        self,
        corpus: tuple[CorpusItem, ...],
        manifest: CaptureEvidenceManifest,
    ) -> dict[str, object]:
        self.consumed_runs.append(corpus)
        self.manifest_runs.append(manifest)
        issued = {anchor.receipt_id for anchor in manifest.anchors}
        anchored = tuple(
            item
            for item in corpus
            if item.source_channel == "hook_observed_user_prompt_anchor"
            and item.receipt_id in issued
        )
        direction = next(
            (
                item
                for item in anchored
                if item.semantic_role == "explicit_user_direction"
            ),
            None,
        )
        recalled_rule = any(
            item.source_channel == "recalled_decision_envelope"
            and item.semantic_role == "explicit_user_direction"
            for item in corpus
        )
        continuation = next(
            (
                item
                for item in anchored
                if item.semantic_role == "unrelated_continue"
            ),
            None,
        )
        evidence = direction
        basis = "explicit_user_direction"
        if evidence is None and recalled_rule and continuation is not None:
            evidence = continuation
            basis = "explicit_user_confirmation"

        value = inventory_fixtures.v5_inventory(manifest)
        if evidence is None:
            value["signals"] = []
            return value
        value["signals"][0]["rule"] = evidence.text
        value["signals"][0]["confirmation_basis"] = basis
        value["signals"][0]["evidence_receipt_ids"] = [evidence.receipt_id]
        return value


class InvalidReceiptGateway(runner_fixtures.FakeGateway):
    def __init__(
        self, cwd: str, receipts: tuple[str, ...], *, thread_prefix: str
    ) -> None:
        super().__init__(cwd)
        self.receipts = receipts
        self.thread_prefix = thread_prefix

    def fork_disposable_thread(self, thread_id: str, last_turn_id: str) -> str:
        self.fork_count += 1
        return f"{self.thread_prefix}-{self.fork_count}"

    def run_structured_turn(
        self, thread_id, prompt, output_schema, profile, cwd
    ) -> AppServerTurnReceipt:
        self.prompts.append(prompt)
        self.schemas.append(output_schema)
        if self.binding_store is not None and not self.binding_store.is_internal_thread(
            thread_id
        ):
            raise AssertionError("structured Turn started before internal binding")
        properties = output_schema.get("properties", {})
        if "signals" not in properties:
            raise AssertionError("invalid receipt attempt reached Extraction")
        self.inventory_count += 1
        output = deepcopy(runner_fixtures.VALID_INVENTORY)
        output["signals"][0]["signal_ordinal"] = 1
        output["signals"][0]["evidence_receipt_ids"] = list(self.receipts)
        return AppServerTurnReceipt.create(
            thread_id=thread_id,
            turn_id=f"inventory-turn-{self.inventory_count}",
            structured_output=output,
            model_profile_id=profile.profile_id,
        )


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
        self.corpus_model = DeterministicCorpusModel()
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

    def _record_event(
        self,
        event: str,
        *,
        session_id: str,
        turn_id: str,
        occurred_at: str,
        prompt: str = "PRIVATE_PROMPT_NOT_PERSISTED",
    ):
        invocation = HookInvocation.from_dict(
            {
                "hook_event_name": event,
                "session_id": session_id,
                "turn_id": turn_id,
                "cwd": str(self.root),
                **({"prompt": prompt} if event == "UserPromptSubmit" else {}),
            },
            occurred_at=occurred_at,
            repository=self.repository,
        )
        return self.ledger.record_hook(invocation)

    def _source(
        self,
        upper_stop_event_id: str,
        *,
        seed: str = "2",
        session_id: str = runner_fixtures.SOURCE_SESSION,
        upper_turn_id: str = runner_fixtures.SOURCE_TURN,
    ) -> FrozenSessionSource:
        return FrozenSessionSource(
            request_id="crq_" + seed * 32,
            source_key="src_" + seed * 32,
            repository_id=runner_fixtures.REPOSITORY_ID,
            session_id=session_id,
            cwd=str(self.root),
            lineage="lin_" + seed * 32,
            previous_handled_turn_id=None,
            upper_turn_id=upper_turn_id,
            source_fingerprint=seed * 64,
            previous_handled_event_id=None,
            upper_stop_event_id=upper_stop_event_id,
        )

    def _corpus_inventory(
        self,
        *,
        seed: str,
        inherited: tuple[CorpusItem, ...],
        prompt_text: str,
        prompt_role: str,
        reference_digest: str | None = None,
    ) -> tuple[
        dict[str, object], CaptureEvidenceManifest, tuple[CorpusItem, ...]
    ]:
        session_id = f"019fb100-0000-7000-8000-{int(seed, 16):012x}"
        turn_id = f"019fb100-0000-7000-9000-{int(seed, 16):012x}"
        prompt = self._record_event(
            "UserPromptSubmit",
            session_id=session_id,
            turn_id=turn_id,
            occurred_at="2026-08-07T08:00:00Z",
            prompt=prompt_text,
        )
        if reference_digest is not None:
            self.host.bind_activation(
                session_id=session_id,
                turn_id="activation-" + seed,
                cwd=str(self.root),
                binding_id="binding-" + seed,
                now=NOW,
            )
            self.host.begin_turn_gate(
                session_id=session_id,
                turn_id=turn_id,
                context_epoch=0,
                intent_epoch=0,
                active_generation=None,
                gate_id="gate-" + seed,
            )
            self.host.commit_turn_gate(
                session_id=session_id,
                turn_id=turn_id,
                gate_id="gate-" + seed,
                result=TurnGateResult("retrieve", "intent-" + seed, 0, 1),
                active_set_digest=reference_digest,
            )
        stop = self._record_event(
            "Stop",
            session_id=session_id,
            turn_id=turn_id,
            occurred_at="2026-08-07T08:00:01Z",
        )
        source = self._source(
            stop.event_id,
            seed=seed,
            session_id=session_id,
            upper_turn_id=turn_id,
        )
        manifest = self.runner._build_manifest(source)
        self.assertEqual(
            (prompt_anchor_receipt_id(prompt.event_id),),
            tuple(anchor.receipt_id for anchor in manifest.anchors),
        )
        corpus = inherited + (
            CorpusItem(
                "hook_observed_user_prompt_anchor",
                prompt_text,
                prompt_role,
                prompt_anchor_receipt_id(prompt.event_id),
            ),
        )
        return self.corpus_model.inventory(corpus, manifest), manifest, corpus

    def test_01_recalled_decision_or_application_instruction_without_anchor_yields_zero_candidates(self) -> None:
        cases = (
            ("1", "recalled_decision_envelope", "recalled Decision rule"),
            ("2", "application_instruction", "Apply only delivered Decisions."),
        )
        for seed, channel, text in cases:
            with self.subTest(channel=channel):
                output, manifest, corpus = self._corpus_inventory(
                    seed=seed,
                    inherited=(CorpusItem(channel, text, "context_only"),),
                    prompt_text="What files changed?",
                    prompt_role="unrelated_question",
                )
                inventory, provenance = validate_inventory_v5(output, manifest)
                self.assertEqual((), inventory.signals)
                self.assertEqual((), provenance)
                observations, candidate_provenance = validate_extraction_output_v5(
                    "cap_" + seed * 32,
                    SourceCheckpoint("thread-" + seed, "turn-" + seed),
                    "anheng",
                    {"candidates": []},
                    inventory,
                    provenance,
                    manifest,
                )
                self.assertEqual((), observations)
                self.assertEqual((), candidate_provenance)
                self.assertEqual(corpus, self.corpus_model.consumed_runs[-1])
                self.assertIsNone(corpus[0].receipt_id)

    def test_02_non_prompt_sources_alone_yield_zero_candidates(self) -> None:
        cases = (
            ("3", "assistant", "assistant proposal"),
            ("4", "tool", "tool output"),
            ("5", "code", "source code"),
            ("6", "capture_artifact", "Capture artifact"),
            ("7", "compaction_summary", "compaction summary"),
        )
        for seed, channel, text in cases:
            with self.subTest(channel=channel):
                output, manifest, corpus = self._corpus_inventory(
                    seed=seed,
                    inherited=(
                        CorpusItem(channel, text, "explicit_user_direction"),
                    ),
                    prompt_text="Summarize the workspace.",
                    prompt_role="unrelated_question",
                )
                inventory, provenance = validate_inventory_v5(output, manifest)
                self.assertEqual((), inventory.signals)
                self.assertEqual((), provenance)
                observations, candidate_provenance = validate_extraction_output_v5(
                    "cap_" + seed * 32,
                    SourceCheckpoint("thread-" + seed, "turn-" + seed),
                    "anheng",
                    {"candidates": []},
                    inventory,
                    provenance,
                    manifest,
                )
                self.assertEqual((), observations)
                self.assertEqual((), candidate_provenance)
                self.assertIsNone(corpus[0].receipt_id)

    def test_03_anchored_explicit_direction_qualifies_with_recalled_context(self) -> None:
        same_topic = "Only the explicit Update action authorizes Capture."
        output, manifest, corpus = self._corpus_inventory(
            seed="8",
            inherited=(
                CorpusItem(
                    "recalled_decision_envelope",
                    same_topic,
                    "explicit_user_direction",
                ),
            ),
            prompt_text=same_topic,
            prompt_role="explicit_user_direction",
            reference_digest="a" * 64,
        )
        inventory, signal_provenance = validate_inventory_v5(
            output, manifest
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
        self.assertIsNone(corpus[0].receipt_id)
        self.assertEqual(
            corpus[1].receipt_id,
            signal_provenance[0].evidence_receipt_ids[0],
        )

    def test_04_identical_recalled_and_prompt_text_is_distinguished_by_receipt(self) -> None:
        identical_text = "Only the explicit Update action authorizes Capture."
        recalled_only, recalled_manifest, recalled_corpus = self._corpus_inventory(
            seed="9",
            inherited=(
                CorpusItem(
                    "recalled_decision_envelope",
                    identical_text,
                    "explicit_user_direction",
                ),
            ),
            prompt_text="What happened next?",
            prompt_role="unrelated_question",
        )
        recalled_inventory, recalled_provenance = validate_inventory_v5(
            recalled_only, recalled_manifest
        )
        anchored, anchored_manifest, anchored_corpus = self._corpus_inventory(
            seed="a",
            inherited=(
                CorpusItem(
                    "recalled_decision_envelope",
                    identical_text,
                    "explicit_user_direction",
                ),
            ),
            prompt_text=identical_text,
            prompt_role="explicit_user_direction",
        )
        anchored_inventory, anchored_provenance = validate_inventory_v5(
            anchored, anchored_manifest
        )
        recalled_observations, _ = validate_extraction_output_v5(
            "cap_" + "9" * 32,
            SourceCheckpoint("thread-9", "turn-9"),
            "anheng",
            {"candidates": []},
            recalled_inventory,
            recalled_provenance,
            recalled_manifest,
        )
        anchored_observations, _ = validate_extraction_output_v5(
            "cap_" + "a" * 32,
            SourceCheckpoint("thread-a", "turn-a"),
            "anheng",
            _extraction(),
            anchored_inventory,
            anchored_provenance,
            anchored_manifest,
        )

        self.assertEqual((), recalled_provenance)
        self.assertEqual((), recalled_observations)
        self.assertEqual(1, len(anchored_observations))
        self.assertEqual(identical_text, recalled_corpus[0].text)
        self.assertEqual(identical_text, anchored_corpus[0].text)
        self.assertEqual(identical_text, anchored_corpus[1].text)
        self.assertIsNone(anchored_corpus[0].receipt_id)
        self.assertEqual(
            (anchored_corpus[1].receipt_id,),
            anchored_provenance[0].evidence_receipt_ids,
        )
        self.assertEqual("candidate_eligible", anchored_provenance[0].disposition)

    def test_05_invalid_receipt_sets_fail_the_complete_attempt(self) -> None:
        for index, invalid_class in enumerate(
            (
                "unknown",
                "duplicate",
                "reordered",
                "cross_session",
                "post_boundary",
                "forged",
            ),
            start=1,
        ):
            with self.subTest(invalid_class=invalid_class):
                seed = f"{index:x}"
                source_session = f"019fb200-0000-7000-8000-{index:012x}"
                other_session = f"019fb300-0000-7000-8000-{index:012x}"
                first_turn = f"019fb200-0000-7000-9000-{index:012x}"
                second_turn = f"019fb200-0000-7000-a000-{index:012x}"
                post_turn = f"019fb200-0000-7000-b000-{index:012x}"
                cross = self._record_event(
                    "UserPromptSubmit",
                    session_id=other_session,
                    turn_id=first_turn,
                    occurred_at="2026-08-07T09:00:00Z",
                )
                first = self._record_event(
                    "UserPromptSubmit",
                    session_id=source_session,
                    turn_id=first_turn,
                    occurred_at="2026-08-07T09:00:01Z",
                )
                second = self._record_event(
                    "UserPromptSubmit",
                    session_id=source_session,
                    turn_id=second_turn,
                    occurred_at="2026-08-07T09:00:02Z",
                )
                stop = self._record_event(
                    "Stop",
                    session_id=source_session,
                    turn_id=second_turn,
                    occurred_at="2026-08-07T09:00:03Z",
                )
                post = self._record_event(
                    "UserPromptSubmit",
                    session_id=source_session,
                    turn_id=post_turn,
                    occurred_at="2026-08-07T09:00:04Z",
                )
                valid = (
                    prompt_anchor_receipt_id(first.event_id),
                    prompt_anchor_receipt_id(second.event_id),
                )
                invalid_receipts = {
                    "unknown": ("rcpt_" + "e" * 64,),
                    "duplicate": (valid[0], valid[0]),
                    "reordered": tuple(reversed(valid)),
                    "cross_session": (prompt_anchor_receipt_id(cross.event_id),),
                    "post_boundary": (prompt_anchor_receipt_id(post.event_id),),
                    "forged": ("forged-receipt",),
                }[invalid_class]
                gateway = InvalidReceiptGateway(
                    str(self.root),
                    invalid_receipts,
                    thread_prefix=f"invalid-{seed}",
                )
                gateway.binding_store = self.host
                gateway.interactive_ids = frozenset((source_session,))
                runner = RequestedCaptureRunner(
                    gateway=gateway,
                    operation_store=self.operations,
                    template_catalog=TemplateCatalog(
                        REPOSITORY_ROOT / "decision-templates", ENVELOPE_ROOT
                    ),
                    evidence_ledger=self.ledger,
                    recall_host_store=self.host,
                )
                source = self._source(
                    stop.event_id,
                    seed=seed,
                    session_id=source_session,
                    upper_turn_id=second_turn,
                )
                manifest = runner._build_manifest(source)
                self.assertEqual(valid, tuple(a.receipt_id for a in manifest.anchors))
                self.assertNotIn(prompt_anchor_receipt_id(cross.event_id), valid)
                self.assertNotIn(prompt_anchor_receipt_id(post.event_id), valid)

                with self.assertRaisesRegex(
                    RequestedCaptureFailed, "capture_provenance_invalid"
                ):
                    runner.run(
                        source,
                        route_context=self.route_context,
                        matched_paths=runner_fixtures.MATCHED_PATHS,
                        template_id="business",
                        model_profile=gateway.profile,
                    )
                operation = self.operations.operation_for_source(
                    source.request_id,
                    source.source_key,
                    self.route_context.decision_space_id,
                )
                self.assertIsNotNone(operation)
                assert operation is not None
                self.assertEqual("failed_terminal", operation.status)
                self.assertEqual("capture_provenance_invalid", operation.failure_code)
                attempt = self.operations._connection.execute(
                    """
                    SELECT state, archive_state, failure_code
                    FROM capture_execution_attempts WHERE operation_id = ?
                    """,
                    (operation.operation_id,),
                ).fetchone()
                self.assertEqual(
                    ("abandoned", "archived", "capture_provenance_invalid"),
                    tuple(attempt),
                )
                counts = (
                    gateway.fork_count,
                    gateway.inventory_count,
                    gateway.extraction_count,
                )
                with self.assertRaises(SourceBoundaryUnavailable):
                    runner.run(
                        source,
                        route_context=self.route_context,
                        matched_paths=runner_fixtures.MATCHED_PATHS,
                        template_id="business",
                        model_profile=gateway.profile,
                    )
                self.assertEqual((1, 1, 0), counts)
                self.assertEqual(
                    counts,
                    (
                        gateway.fork_count,
                        gateway.inventory_count,
                        gateway.extraction_count,
                    ),
                )
                self.assertEqual([f"invalid-{seed}-1"], gateway.archived_threads)

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
        dispositions = (
            "existing_decision_adoption",
            "needs_evidence",
            "excluded_reference_only",
            "excluded_code_fact_only",
            "excluded_unverified",
        )
        for disposition in dispositions:
            with self.subTest(disposition=disposition):
                fixture = processor_fixtures.CaptureRequestProcessorTest()
                fixture.setUp()
                try:
                    original_run = fixture.capture_runner.run
                    sidecar = SignalProvenance.create(
                        signal_ordinal=1,
                        evidence_receipt_ids=("rcpt_" + "2" * 64,),
                        active_reference_set_digests=(),
                        disposition=disposition,
                    )

                    def run_without_candidate_content(*args, **kwargs):
                        captured = original_run(*args, **kwargs)
                        return replace(
                            captured,
                            observations=(),
                            signal_provenance=(sidecar,),
                            candidate_provenance=(),
                        )

                    fixture.capture_runner.run = run_without_candidate_content
                    views = fixture.views()
                    client = processor_fixtures.FakeCentralClient(
                        fixture.group, views
                    )
                    fixture.processor().process(fixture.group, client)

                    self.assertEqual(0, fixture.reconciliation_runner.calls)
                    self.assertEqual(2, len(client.uploads))
                    self.assertTrue(all(batch.items == () for batch in client.uploads))
                    self.assertTrue(
                        all(
                            batch.item_protocol == "candidate-provenance-v1"
                            for batch in client.uploads
                        )
                    )
                    self.assertEqual(
                        0,
                        fixture.request_state._connection.execute(
                            "SELECT COUNT(*) FROM slice_candidate_family_revisions"
                        ).fetchone()[0],
                    )
                    self.assertEqual(
                        2,
                        fixture.request_state._connection.execute(
                            "SELECT COUNT(*) FROM slice_candidate_outbox"
                        ).fetchone()[0],
                    )
                    self.assertTrue(
                        all(
                            fixture.request_state.staged_slice_batch(
                                fixture.group.request_id, view.slice_id
                            ).items
                            == ()
                            for view in views
                        )
                    )
                finally:
                    fixture.doCleanups()

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
        output, manifest, corpus = self._corpus_inventory(
            seed="b",
            inherited=(
                CorpusItem(
                    "recalled_decision_envelope",
                    "Only the explicit Update action authorizes Capture.",
                    "explicit_user_direction",
                ),
            ),
            prompt_text="继续",
            prompt_role="unrelated_continue",
            reference_digest="f" * 64,
        )
        _, provenance = validate_inventory_v5(
            output, manifest
        )

        self.assertEqual("needs_evidence", provenance[0].disposition)
        self.assertEqual("继续", corpus[1].text)
        self.assertEqual(corpus, self.corpus_model.consumed_runs[-1])

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
