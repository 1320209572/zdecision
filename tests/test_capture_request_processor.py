from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from zdecision.agent.capture_routing import CaptureRoutingStore
from zdecision.agent.control_bindings import ControlBindingStore
from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import AgentEvent, HookInvocation, RepositorySnapshot, event_id_for
from zdecision.agent.git_path_evidence import FrozenGitPathEvidence
from zdecision.agent.request_state import RequestStateStore
from zdecision.agent.session_index import SessionIndex
from zdecision.app_server.models import FeasibilityModelProfile
from zdecision.app_server.requested_capture import SessionCaptureResult
from zdecision.capture.models import Candidate, CandidateContent, SourceCheckpoint
from zdecision.capture.reconciliation import (
    ReconciliationDecision,
    ReconciliationResult,
    apply_reconciliation,
)
from zdecision.capture.provenance import CandidateProvenance, CandidateProvenanceSummary
from zdecision.central.decision_spaces import EnabledRepository, RepositoryDecisionRoute
from zdecision.ids import candidate_family_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import (
    CandidateOwnershipSnapshot,
    CaptureSliceView,
    ClaimedCaptureGroup,
    SliceUploadReceipt,
)


NOW = datetime(2026, 8, 5, 5, 0, tzinfo=UTC)
REQUEST_ID = "crq_" + "1" * 32
OTHER_REQUEST_ID = "crq_" + "9" * 32
REPOSITORY_ID = "repo_" + "2" * 32
SESSION_ID = "019fb100-0000-7000-8000-000000000001"
TURN_ID = "019fb100-0000-7000-8000-000000000002"


class FakeGitPaths:
    def __init__(self, evidence: FrozenGitPathEvidence) -> None:
        self.evidence = evidence
        self.calls = 0
        self.fail = False

    def freeze(self, repository, sources):
        self.calls += 1
        if self.fail:
            raise OSError("repository is unavailable")
        return self.evidence


class FakeCaptureRunner:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.source_calls: list[str] = []
        self.profile = FeasibilityModelProfile.create(
            model_id="model-default",
            reasoning_effort="medium",
            discovery_digest="a" * 64,
            discovered_at="2026-08-05T05:00:00Z",
        )
        self.protocol_by_session: dict[str, str] = {}
        self.unavailable_sessions: set[str] = set()

    def sweep_archives(self) -> None:
        pass

    def operation_profile(self, source, route_context):
        return None

    def resolve_request_profile(self, profile):
        return self.profile if profile is None else profile

    def run(
        self,
        source,
        *,
        route_context,
        matched_paths,
        template_id,
        model_profile,
        heartbeat=None,
    ):
        self.source_calls.append(source.session_id)
        if source.session_id in self.unavailable_sessions:
            from zdecision.app_server.requested_capture import (
                SourceEvidenceUnavailable,
            )

            raise SourceEvidenceUnavailable("missing prompt anchors")
        self.calls.append(route_context.decision_space_id)
        seed = route_context.route_id[4]
        observation = Candidate(
            candidate_id="cand_" + seed * 32 + "_01",
            capture_id="cap_" + seed * 32,
            ordinal=1,
            content=CandidateContent(
                product=route_context.decision_space_name,
                claim=f"Decision for {route_context.decision_space_name}",
                future_action="Keep this leaf behavior stable.",
                scope_summary="One routed Decision space",
                repositories=(REPOSITORY_ID,),
                paths=matched_paths,
                invalidation_conditions=("The routed behavior changes.",),
            ),
            source=SourceCheckpoint(source.session_id, source.upper_turn_id),
        )
        return SessionCaptureResult(
            status="completed",
            source_key=source.source_key,
            capture_operation_id=observation.capture_id,
            inventory_turn_id="inventory-turn",
            extraction_turn_id="extraction-turn",
            observations=(observation,),
            evidence_digest="b" * 64,
            model_profile=model_profile,
            protocol_revision=self.protocol_by_session.get(
                source.session_id, "extractor-v5"
            ),
            signal_provenance=(),
            candidate_provenance=(
                CandidateProvenance.create(
                    candidate_id=observation.candidate_id,
                    manifest_digest="1" * 64,
                    source_signal_ordinal=1,
                    evidence_receipt_ids=("rcpt_" + "2" * 64,),
                    active_reference_set_digests=(),
                    reference_decision_ids=(),
                ),
            ),
        )


class FakeReconciliationRunner:
    def __init__(self) -> None:
        self.calls = 0
        self.observation_ids: list[tuple[str, ...]] = []
        self.provenance_keys: list[tuple[str, ...]] = []

    def sweep_archives(self) -> None:
        pass

    def run(
        self,
        *,
        request_id,
        slice_id,
        repository_id,
        decision_space_id,
        cwd,
        observations,
        candidate_provenance,
        current,
        profile,
        heartbeat=None,
    ):
        self.calls += 1
        self.observation_ids.append(tuple(item.candidate_id for item in observations))
        self.provenance_keys.append(tuple(sorted(candidate_provenance)))
        decisions = tuple(
            ReconciliationDecision(
                item.candidate_id,
                "unrelated",
                candidate_family_id(
                    repository_id, decision_space_id, item.candidate_id
                ),
                None,
            )
            for item in observations
        )
        return apply_reconciliation(
            repository_id,
            decision_space_id,
            observations,
            current,
            decisions,
            {
                candidate_id: CandidateProvenanceSummary(
                    protocol="candidate-provenance-v1",
                    kind="host_observed_user_prompt_anchor",
                    digest=sidecar.provenance_digest,
                )
                for candidate_id, sidecar in candidate_provenance.items()
            },
        )


class FakeCentralClient:
    def __init__(self, group: ClaimedCaptureGroup, views) -> None:
        self.group = group
        self.views = tuple(views)
        self.uploads = []
        self.completed = []
        self.fail_on_upload_number: int | None = None
        self.corrupt_receipt = False
        self.completion_error: Exception | None = None

    def start(self, request_id, lease_token):
        pass

    def heartbeat(self, request_id, lease_token):
        pass

    def progress(self, request_id, lease_token, code):
        pass

    def plan_slices(self, group, selections):
        return self.views

    def upload_slice(self, group, batch):
        self.uploads.append(batch)
        if self.fail_on_upload_number == len(self.uploads):
            raise ConnectionError("offline")
        core = {
            "request_id": batch.request_id,
            "slice_id": batch.slice_id,
            "candidate_count": len(batch.items),
            "batch_digest": batch.batch_digest,
        }
        receipt = SliceUploadReceipt(
            batch.request_id,
            batch.slice_id,
            len(batch.items),
            __import__("hashlib").sha256(canonical_json_bytes(core)).hexdigest(),
        )
        if self.corrupt_receipt:
            return SliceUploadReceipt(
                receipt.request_id,
                receipt.slice_id,
                receipt.candidate_count,
                "f" * 64,
            )
        return receipt

    def complete_group(self, group, receipt_digest):
        if self.completion_error is not None:
            raise self.completion_error
        self.completed.append(receipt_digest)


class CaptureRequestProcessorTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state.sqlite3"
        self.database = AgentDatabase.open(self.state)
        self.addCleanup(self.database.close)
        self.database.put_enabled_repository(
            EnabledRepository(REPOSITORY_ID, True)
        )
        repository = RepositorySnapshot(
            REPOSITORY_ID, str(self.root), "main", "d" * 40
        )
        invocation = HookInvocation.from_dict(
            {
                "hook_event_name": "Stop",
                "session_id": SESSION_ID,
                "turn_id": TURN_ID,
                "cwd": str(self.root),
            },
            occurred_at="2026-08-05T05:00:00Z",
            repository=repository,
        )
        event = AgentEvent(
            event_id_for(invocation), invocation, "recorded", None
        )
        self.database.record_hook(invocation)
        self.session_index = SessionIndex.open(self.state)
        self.addCleanup(self.session_index.close)
        self.session_index.observe(event)
        self.request_state = RequestStateStore.open(self.state)
        self.addCleanup(self.request_state.close)
        self.routing_store = CaptureRoutingStore.open(self.state)
        self.addCleanup(self.routing_store.close)
        self.control_store = ControlBindingStore.open(self.state)
        self.addCleanup(self.control_store.close)
        self.routes = (
            self.route("a", "packages/products/cloud", "dsp_" + "3" * 32),
            self.route("b", "packages/shared/theme", "dsp_" + "4" * 32),
        )
        digest = __import__("hashlib").sha256(
            canonical_json_bytes(
                {"routes": [route.to_dict() for route in self.routes]}
            )
        ).hexdigest()
        self.group = ClaimedCaptureGroup(
            REQUEST_ID,
            REPOSITORY_ID,
            "business",
            "all_valid_sessions",
            "web_action_task_3",
            self.routes,
            digest,
            "lease_0123456789abcdef",
            "2026-08-05T05:00:30Z",
        )
        evidence = FrozenGitPathEvidence.create(
            repository_id=REPOSITORY_ID,
            head_commit="d" * 40,
            commit_ranges=(),
            paths=(
                "packages/products/cloud/src/app.tsx",
                "packages/shared/theme/src/index.ts",
            ),
        )
        self.git_paths = FakeGitPaths(evidence)
        self.capture_runner = FakeCaptureRunner()
        self.reconciliation_runner = FakeReconciliationRunner()

    def observe_stop(
        self,
        *,
        session_id: str,
        turn_id: str,
        occurred_at: str,
    ) -> None:
        repository = RepositorySnapshot(
            REPOSITORY_ID, str(self.root), "main", "d" * 40
        )
        invocation = HookInvocation.from_dict(
            {
                "hook_event_name": "Stop",
                "session_id": session_id,
                "turn_id": turn_id,
                "cwd": str(self.root),
            },
            occurred_at=occurred_at,
            repository=repository,
        )
        event = self.database.record_hook(invocation)
        self.session_index.observe(event)

    def route(self, seed, prefix, decision_space_id):
        return RepositoryDecisionRoute(
            "drr_" + seed * 32,
            REPOSITORY_ID,
            decision_space_id,
            (prefix,),
            (),
            True,
            1,
        )

    def views(self):
        from zdecision.ids import capture_slice_id

        sources = self.session_index.freeze_sources(
            REQUEST_ID,
            REPOSITORY_ID,
            NOW,
            capture_scope="all_valid_sessions",
        )
        source_boundary_digest = __import__("hashlib").sha256(
            canonical_json_bytes(
                {
                    "sources": [
                        {
                            "source_key": source.source_key,
                            "source_fingerprint": source.source_fingerprint,
                            "previous_handled_head_commit": (
                                source.previous_handled_head_commit
                            ),
                            "upper_head_commit": source.upper_head_commit,
                        }
                        for source in sources
                    ]
                }
            )
        ).hexdigest()

        return tuple(
            CaptureSliceView(
                REQUEST_ID,
                capture_slice_id(REQUEST_ID, route.route_id, 1),
                index,
                CandidateOwnershipSnapshot(
                    REPOSITORY_ID,
                    route.route_id,
                    1,
                    route.decision_space_id,
                    "product" if index == 0 else "shared_unit",
                    "Cloud" if index == 0 else "theme",
                    () if index == 0 else ("Shared",),
                    route.path_prefixes[0],
                    "prod_" + str(index + 5) * 32,
                    "Cloud" if index == 0 else "Shared / packages/shared/theme",
                    source_boundary_digest,
                ),
                "planned",
            )
            for index, route in enumerate(self.routes)
        )

    def processor(self):
        from zdecision.agent.capture_processor import OnDemandCaptureProcessor

        return OnDemandCaptureProcessor(
            database=self.database,
            session_index=self.session_index,
            git_paths=self.git_paths,
            routing_store=self.routing_store,
            capture_runner=self.capture_runner,
            reconciliation_runner=self.reconciliation_runner,
            request_state=self.request_state,
            control_store=self.control_store,
            clock=lambda: NOW,
        )

    def persist_empty_reconciliation_and_restart(
        self, *, item_protocol: str | None
    ):
        view = self.views()[0]
        result = ReconciliationResult.empty(
            REPOSITORY_ID,
            view.ownership.decision_space_id,
            item_protocol=item_protocol,
        )
        self.request_state.store_slice_reconciliation(
            REQUEST_ID, view.slice_id, result
        )
        self.request_state.close()
        self.request_state = RequestStateStore.open(self.state)
        self.addCleanup(self.request_state.close)
        client = FakeCentralClient(self.group, self.views())

        self.processor().process(self.group, client)

        return client.uploads[0]

    def test_one_request_processes_two_independent_leaf_slices(self) -> None:
        client = FakeCentralClient(self.group, self.views())

        self.processor().process(self.group, client)

        self.assertEqual(2, len(client.uploads))
        self.assertEqual(
            tuple(route.decision_space_id for route in self.routes),
            tuple(self.capture_runner.calls),
        )
        self.assertEqual(1, len(client.completed))
        self.assertTrue(
            all(batch.item_protocol == "candidate-provenance-v1" for batch in client.uploads)
        )
        self.assertTrue(
            all(
                item.provenance.to_dict()
                == {
                    "protocol": "candidate-provenance-v1",
                    "kind": "host_observed_user_prompt_anchor",
                    "digest": item.provenance.digest,
                }
                for batch in client.uploads
                for item in batch.items
            )
        )

    def test_empty_legacy_reconciliation_restart_keeps_legacy_slice_protocol(
        self,
    ) -> None:
        batch = self.persist_empty_reconciliation_and_restart(
            item_protocol=None
        )

        self.assertIsNone(batch.item_protocol)
        self.assertNotIn("item_protocol", batch.to_dict())
        self.assertEqual((), batch.items)

    def test_empty_v1_reconciliation_restart_keeps_v1_slice_protocol(
        self,
    ) -> None:
        batch = self.persist_empty_reconciliation_and_restart(
            item_protocol="candidate-provenance-v1"
        )

        self.assertEqual("candidate-provenance-v1", batch.item_protocol)
        self.assertEqual(
            "candidate-provenance-v1", batch.to_dict()["item_protocol"]
        )
        self.assertEqual((), batch.items)

    def test_only_observations_with_candidate_provenance_are_reconciled(self) -> None:
        original_run = self.capture_runner.run

        def run_with_unrelated_observation(*args, **kwargs):
            capture = original_run(*args, **kwargs)
            extra = Candidate(
                candidate_id="cand_" + "f" * 32 + "_01",
                capture_id="cap_" + "f" * 32,
                ordinal=1,
                content=capture.observations[0].content,
                source=capture.observations[0].source,
            )
            return SessionCaptureResult(
                **{**capture.__dict__, "observations": capture.observations + (extra,)}
            )

        self.capture_runner.run = run_with_unrelated_observation
        client = FakeCentralClient(self.group, self.views())

        self.processor().process(self.group, client)

        self.assertTrue(all(len(ids) == 1 for ids in self.reconciliation_runner.observation_ids))
        self.assertEqual(
            self.reconciliation_runner.observation_ids,
            self.reconciliation_runner.provenance_keys,
        )

    def test_route_digest_mismatch_stops_before_git_or_model(self) -> None:
        invalid = object.__new__(ClaimedCaptureGroup)
        for name, value in self.group.__dict__.items():
            object.__setattr__(invalid, name, value)
        object.__setattr__(invalid, "route_snapshot_digest", "f" * 64)

        from zdecision.agent.service import TerminalCaptureRequestError
        with self.assertRaisesRegex(
            TerminalCaptureRequestError, "route_snapshot_mismatch"
        ):
            self.processor().process(
                invalid, FakeCentralClient(invalid, self.views())
            )

        self.assertEqual(0, self.git_paths.calls)
        self.assertEqual([], self.capture_runner.calls)

    def test_no_route_match_completes_without_model(self) -> None:
        self.git_paths.evidence = FrozenGitPathEvidence.create(
            repository_id=REPOSITORY_ID,
            head_commit="d" * 40,
            commit_ranges=(),
            paths=("docs/architecture.md",),
        )
        client = FakeCentralClient(self.group, ())

        self.processor().process(self.group, client)

        self.assertEqual([], self.capture_runner.calls)
        self.assertEqual([], client.completed)

    def test_restart_after_one_receipt_skips_all_completed_model_work(self) -> None:
        first = FakeCentralClient(self.group, self.views())
        first.fail_on_upload_number = 2
        from zdecision.agent.service import RetryableCaptureRequestError
        with self.assertRaises(RetryableCaptureRequestError):
            self.processor().process(self.group, first)
        self.assertTrue(
            self.request_state.has_receipt(
                REQUEST_ID, self.views()[0].slice_id
            )
        )
        calls_before = tuple(self.capture_runner.calls)

        second = FakeCentralClient(self.group, self.views())
        self.git_paths.fail = True
        self.processor().process(self.group, second)

        self.assertEqual(calls_before, tuple(self.capture_runner.calls))
        self.assertEqual(1, self.git_paths.calls)
        self.assertEqual(1, len(second.uploads))
        self.assertEqual(self.views()[1].slice_id, second.uploads[0].slice_id)

    def test_receipt_must_bind_the_exact_staged_slice_batch(self) -> None:
        client = FakeCentralClient(self.group, self.views())
        client.corrupt_receipt = True
        from zdecision.agent.service import TerminalCaptureRequestError

        with self.assertRaisesRegex(
            TerminalCaptureRequestError, "local_delivery_conflict"
        ):
            self.processor().process(self.group, client)

        self.assertFalse(
            self.request_state.has_receipt(
                REQUEST_ID, self.views()[0].slice_id
            )
        )

    def test_invalid_completion_response_does_not_acknowledge_sources(self) -> None:
        from zdecision.agent.central_client import CentralClientError
        from zdecision.agent.service import TerminalCaptureRequestError

        client = FakeCentralClient(self.group, self.views())
        client.completion_error = CentralClientError("central_response_invalid")

        with self.assertRaisesRegex(
            TerminalCaptureRequestError, "central_response_invalid"
        ):
            self.processor().process(self.group, client)

        next_sources = self.session_index.freeze_sources(
            OTHER_REQUEST_ID,
            REPOSITORY_ID,
            NOW,
            capture_scope="all_valid_sessions",
        )
        self.assertEqual(1, len(next_sources))
        self.assertIsNone(next_sources[0].previous_handled_turn_id)

    def test_unavailable_source_evidence_is_terminally_excluded_without_retry(
        self,
    ) -> None:
        self.capture_runner.unavailable_sessions.add(SESSION_ID)
        client = FakeCentralClient(self.group, self.views())

        self.processor().process(self.group, client)

        row = self.session_index._connection.execute(
            """
            SELECT excluded_reason FROM capture_request_sources
            WHERE request_id = ?
            """,
            (REQUEST_ID,),
        ).fetchone()
        self.assertEqual("user_prompt_evidence_unavailable", row["excluded_reason"])
        self.assertEqual(0, self.reconciliation_runner.calls)
        self.assertEqual([SESSION_ID], self.capture_runner.source_calls)
        self.assertEqual(2, len(client.uploads))
        self.assertTrue(all(not batch.items for batch in client.uploads))

    def test_unavailable_source_is_not_reinvoked_in_later_slices(self) -> None:
        second_session = "019fb100-0000-7000-8000-000000000010"
        self.observe_stop(
            session_id=second_session,
            turn_id="019fb100-0000-7000-8000-000000000011",
            occurred_at="2026-08-05T05:00:01Z",
        )
        self.capture_runner.unavailable_sessions.add(SESSION_ID)
        client = FakeCentralClient(self.group, self.views())

        self.processor().process(self.group, client)

        self.assertEqual(1, self.capture_runner.source_calls.count(SESSION_ID))
        self.assertEqual(2, self.capture_runner.source_calls.count(second_session))
        self.assertEqual(2, self.reconciliation_runner.calls)
        self.assertEqual(2, len(client.uploads))
        self.assertTrue(all(len(batch.items) == 1 for batch in client.uploads))

    def test_slice_rejects_mixed_legacy_and_v5_capture_results(self) -> None:
        from zdecision.agent.service import TerminalCaptureRequestError

        second_session = "019fb100-0000-7000-8000-000000000010"
        self.observe_stop(
            session_id=second_session,
            turn_id="019fb100-0000-7000-8000-000000000011",
            occurred_at="2026-08-05T05:00:01Z",
        )
        self.capture_runner.protocol_by_session = {
            SESSION_ID: "extractor-v4",
            second_session: "extractor-v5",
        }
        client = FakeCentralClient(self.group, self.views())

        with self.assertRaisesRegex(
            TerminalCaptureRequestError, "legacy_capture_protocol_mixed"
        ):
            self.processor().process(self.group, client)

        self.assertEqual(0, self.reconciliation_runner.calls)
        self.assertEqual([], client.uploads)


if __name__ == "__main__":
    unittest.main()
