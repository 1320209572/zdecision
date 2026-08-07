"""Trusted local Git routing and crash-safe multi-slice Capture delivery."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime

from zdecision.agent.capture_operation_store import CaptureOperationStoreError
from zdecision.agent.capture_routing import (
    CaptureGroupPlan,
    CaptureRoutingStore,
    CaptureSlicePlan,
)
from zdecision.agent.central_client import CentralClientError
from zdecision.agent.control_bindings import ControlBindingStore
from zdecision.agent.db import AgentDatabase
from zdecision.agent.git_path_evidence import GitPathEvidenceReader
from zdecision.agent.repository_routes import RepositoryRouteSnapshot
from zdecision.agent.request_state import BatchConflict, RequestStateError, RequestStateStore
from zdecision.agent.service import RetryableCaptureRequestError, TerminalCaptureRequestError
from zdecision.agent.session_index import (
    FrozenSessionSource,
    RequestModelProfileConflict,
    RequestModelProfileCorrupt,
    SessionIndex,
)
from zdecision.app_server.models import FeasibilityModelProfile
from zdecision.app_server.reconciliation_runner import (
    ReconciliationAttemptRetryable,
    ReconciliationRunnerError,
)
from zdecision.app_server.requested_capture import (
    CaptureAttemptRetryable,
    FrozenModelUnavailable,
    RequestedCaptureFailed,
    SessionCaptureResult,
    SourceBoundaryUnavailable,
    SourceEvidenceUnavailable,
    SourceNotInteractive,
)
from zdecision.capture.on_demand import FrozenCaptureRouteContext
from zdecision.capture.reconciliation import CandidateFamilyRevision, ReconciliationResult
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import (
    CandidateRevisionUpload,
    CandidateSliceBatchUpload,
    CaptureSliceView,
    ClaimedCaptureGroup,
    SliceUploadReceipt,
)


_TRANSIENT_CENTRAL_CODES = frozenset(
    ("central_connection_unavailable", "central_temporarily_unavailable")
)


class OnDemandCaptureProcessor:
    """Deliver one server-frozen Capture group one leaf slice at a time."""

    def __init__(
        self,
        *,
        database: AgentDatabase,
        session_index: SessionIndex,
        git_paths: GitPathEvidenceReader,
        routing_store: CaptureRoutingStore,
        capture_runner,
        reconciliation_runner,
        request_state: RequestStateStore,
        control_store: ControlBindingStore,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(database, AgentDatabase):
            raise TypeError("database must be an AgentDatabase")
        if not isinstance(session_index, SessionIndex):
            raise TypeError("session_index must be a SessionIndex")
        if not hasattr(git_paths, "freeze"):
            raise TypeError("git_paths must freeze Git evidence")
        if not isinstance(routing_store, CaptureRoutingStore):
            raise TypeError("routing_store must be a CaptureRoutingStore")
        if not isinstance(request_state, RequestStateStore):
            raise TypeError("request_state must be a RequestStateStore")
        if not isinstance(control_store, ControlBindingStore):
            raise TypeError("control_store must be a ControlBindingStore")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.database = database
        self.session_index = session_index
        self.git_paths = git_paths
        self.routing_store = routing_store
        self.capture_runner = capture_runner
        self.reconciliation_runner = reconciliation_runner
        self.request_state = request_state
        self.control_store = control_store
        self.clock = clock

    def process(self, group: ClaimedCaptureGroup, client) -> None:
        if not isinstance(group, ClaimedCaptureGroup):
            raise TypeError("group must be a ClaimedCaptureGroup")
        try:
            self._process(group, client)
        except (RetryableCaptureRequestError, TerminalCaptureRequestError):
            raise
        except CentralClientError as error:
            if error.code in _TRANSIENT_CENTRAL_CODES:
                raise RetryableCaptureRequestError(error.code) from error
            raise TerminalCaptureRequestError(error.code) from error
        except (ConnectionError, TimeoutError) as error:
            raise RetryableCaptureRequestError(
                "central_connection_unavailable"
            ) from error
        except BatchConflict as error:
            raise TerminalCaptureRequestError("local_delivery_conflict") from error
        except CaptureAttemptRetryable as error:
            raise RetryableCaptureRequestError("capture_attempt_retryable") from error
        except FrozenModelUnavailable as error:
            raise TerminalCaptureRequestError("frozen_model_unavailable") from error
        except RequestModelProfileConflict as error:
            raise TerminalCaptureRequestError("model_profile_mismatch") from error
        except RequestModelProfileCorrupt as error:
            raise TerminalCaptureRequestError("local_capture_state_invalid") from error
        except SourceBoundaryUnavailable as error:
            raise TerminalCaptureRequestError("source_boundary_unavailable") from error
        except CaptureOperationStoreError as error:
            raise TerminalCaptureRequestError("local_capture_state_invalid") from error
        except ReconciliationAttemptRetryable as error:
            raise RetryableCaptureRequestError(
                "reconciliation_attempt_retryable"
            ) from error
        except RequestedCaptureFailed as error:
            raise TerminalCaptureRequestError("capture_result_failed") from error
        except ReconciliationRunnerError as error:
            raise TerminalCaptureRequestError(
                "reconciliation_result_failed"
            ) from error
        except RequestStateError as error:
            raise TerminalCaptureRequestError("local_request_state_invalid") from error
        except OSError as error:
            raise RetryableCaptureRequestError("local_state_unavailable") from error
        except ValueError as error:
            code = str(error)
            if code in {
                "route_snapshot_mismatch",
                "route_snapshot_repository_mismatch",
                "route_repository_mismatch",
                "generic_shared_route_forbidden",
                "decision_space_route_ambiguous",
                "repository_identity_mismatch",
                "source_repository_mismatch",
                "repository_snapshot_unavailable",
                "capture_group_plan_conflict",
                "capture_group_plan_corrupt",
            }:
                raise TerminalCaptureRequestError(code) from error
            raise TerminalCaptureRequestError("local_capture_state_invalid") from error

    def _process(self, group: ClaimedCaptureGroup, client) -> None:
        snapshot = RepositoryRouteSnapshot.create(
            group.repository_id, group.route_snapshot
        )
        if snapshot.digest != group.route_snapshot_digest:
            raise TerminalCaptureRequestError("route_snapshot_mismatch")
        enabled = self.database.get_enabled_repository(group.repository_id)
        if enabled is None or not enabled.enabled:
            raise TerminalCaptureRequestError("repository_mapping_mismatch")

        self.capture_runner.sweep_archives()
        self.reconciliation_runner.sweep_archives()
        client.start(group.request_id, group.lease_token)
        sources = self.session_index.freeze_sources(
            group.request_id,
            group.repository_id,
            self._now(),
            capture_scope=group.capture_scope,
            selected_session_id=self._selected_session_id(group),
        )
        plan = self.routing_store.load_plan(group, snapshot)
        if plan is None:
            evidence = self.git_paths.freeze(
                self.database.get_repository_snapshot(group.repository_id),
                sources,
            )
            plan = self.routing_store.get_or_create_plan(
                group, snapshot, sources, evidence
            )
        slices = client.plan_slices(group, plan.route_selections())
        self._verify_slice_plan(group, plan, slices)

        if not plan.slices:
            # Task 2 atomically terminalizes an empty selection while returning
            # this response, so no live lease remains to complete again.
            self.session_index.acknowledge(
                group.request_id,
                self.request_state.receipts_digest(group.request_id, ()),
                self._now(),
            )
            return

        for slice_view, slice_plan in zip(
            slices, plan.slices, strict=True
        ):
            if self.request_state.has_receipt(
                group.request_id, slice_view.slice_id
            ):
                continue
            self._process_slice(
                group, slice_view, slice_plan, sources, client
            )

        ordered_slice_ids = tuple(item.slice_id for item in plan.slices)
        receipt_digest = self.request_state.receipts_digest(
            group.request_id, ordered_slice_ids
        )
        client.complete_group(group, receipt_digest)
        self.session_index.acknowledge(
            group.request_id, receipt_digest, self._now()
        )

    def _process_slice(
        self,
        group: ClaimedCaptureGroup,
        slice_view: CaptureSliceView,
        slice_plan: CaptureSlicePlan,
        sources: tuple[FrozenSessionSource, ...],
        client,
    ) -> None:
        route_context = self._route_context(slice_view, slice_plan)
        staged = self.request_state.staged_slice_batch(
            group.request_id, slice_view.slice_id
        )
        if staged is not None:
            self._deliver_slice(group, client, staged)
            return
        result = self.request_state.slice_reconciliation(
            group.request_id, slice_view.slice_id
        )
        profile: FeasibilityModelProfile | None = None
        if result is None and sources:
            profile = self._request_profile(
                group, sources, route_context
            )
        if result is None:
            captures = self._capture_sources(
                group,
                sources,
                route_context,
                slice_plan.matched_paths,
                profile,
                client,
            )
            observations = tuple(
                observation
                for _, capture in captures
                for observation in capture.observations
            )
            if observations:
                client.progress(
                    group.request_id,
                    group.lease_token,
                    "reconciling_candidates",
                )
                assert profile is not None
                result = self.reconciliation_runner.run(
                    request_id=group.request_id,
                    slice_id=slice_view.slice_id,
                    repository_id=group.repository_id,
                    decision_space_id=route_context.decision_space_id,
                    cwd=min(source.cwd for source, _ in captures),
                    observations=observations,
                    current=self.request_state.slice_current_families(
                        group.repository_id,
                        route_context.decision_space_id,
                    ),
                    profile=profile,
                    heartbeat=lambda: client.heartbeat(
                        group.request_id, group.lease_token
                    ),
                )
            else:
                result = ReconciliationResult.empty(
                    group.repository_id,
                    route_context.decision_space_id,
                )
                result = self.request_state.store_slice_reconciliation(
                    group.request_id, slice_view.slice_id, result
                )
        if (
            result.repository_id != group.repository_id
            or result.decision_space_id != route_context.decision_space_id
        ):
            raise TerminalCaptureRequestError(
                "reconciliation_ownership_mismatch"
            )
        batch = _candidate_slice_batch(
            group.request_id, slice_view, result.uploadable_revisions
        )
        batch = self.request_state.commit_slice_result(
            group.request_id, slice_view.slice_id, result, batch
        )
        client.progress(
            group.request_id, group.lease_token, "uploading_candidates"
        )
        self._deliver_slice(group, client, batch)

    def _request_profile(
        self,
        group: ClaimedCaptureGroup,
        sources: tuple[FrozenSessionSource, ...],
        route_context: FrozenCaptureRouteContext,
    ) -> FeasibilityModelProfile:
        frozen = self.session_index.request_model_profile(group.request_id)
        operation_profiles = tuple(
            profile
            for source in sources
            if (
                profile := self.capture_runner.operation_profile(
                    source, route_context
                )
            )
            is not None
        )
        distinct = set(operation_profiles)
        if len(distinct) > 1:
            raise TerminalCaptureRequestError("model_profile_mismatch")
        operation = next(iter(distinct), None)
        if frozen is not None and operation is not None and frozen != operation:
            raise TerminalCaptureRequestError("model_profile_mismatch")
        profile = self.capture_runner.resolve_request_profile(
            frozen if frozen is not None else operation
        )
        if not isinstance(profile, FeasibilityModelProfile):
            raise TerminalCaptureRequestError("model_profile_mismatch")
        return self.session_index.freeze_request_model_profile(
            group.request_id, profile
        )

    def _capture_sources(
        self,
        group: ClaimedCaptureGroup,
        sources: tuple[FrozenSessionSource, ...],
        route_context: FrozenCaptureRouteContext,
        matched_paths: tuple[str, ...],
        profile: FeasibilityModelProfile | None,
        client,
    ) -> tuple[tuple[FrozenSessionSource, SessionCaptureResult], ...]:
        if sources:
            assert profile is not None
            client.progress(
                group.request_id, group.lease_token, "capturing_sessions"
            )
        captures: list[tuple[FrozenSessionSource, SessionCaptureResult]] = []
        for source in sources:
            try:
                capture = self.capture_runner.run(
                    source,
                    route_context=route_context,
                    matched_paths=matched_paths,
                    template_id=group.template_id,
                    model_profile=profile,
                    heartbeat=lambda: client.heartbeat(
                        group.request_id, group.lease_token
                    ),
                )
            except SourceNotInteractive:
                self.session_index.mark_excluded(
                    group.request_id, source.source_key, "subagent_session"
                )
                continue
            except SourceEvidenceUnavailable:
                self.session_index.mark_excluded(
                    group.request_id,
                    source.source_key,
                    "user_prompt_evidence_unavailable",
                )
                continue
            if not isinstance(capture, SessionCaptureResult):
                raise TerminalCaptureRequestError("capture_result_invalid")
            if capture.source_key != source.source_key:
                raise TerminalCaptureRequestError("capture_source_mismatch")
            if capture.model_profile != profile:
                raise TerminalCaptureRequestError("model_profile_mismatch")
            captures.append((source, capture))
        protocols = {
            "v5"
            if capture.protocol_revision.startswith("extractor-v5")
            else "legacy"
            for _, capture in captures
        }
        if protocols == {"legacy", "v5"}:
            raise TerminalCaptureRequestError(
                "legacy_capture_protocol_mixed"
            )
        return tuple(captures)

    def _deliver_slice(
        self,
        group: ClaimedCaptureGroup,
        client,
        batch: CandidateSliceBatchUpload,
    ) -> None:
        receipt = self.request_state.slice_receipt(
            group.request_id, batch.slice_id
        )
        if receipt is None:
            receipt = client.upload_slice(group, batch)
            if not isinstance(receipt, SliceUploadReceipt):
                raise TerminalCaptureRequestError("upload_receipt_invalid")
            self.request_state.mark_slice_uploaded(receipt)
        if (
            receipt.request_id != group.request_id
            or receipt.slice_id != batch.slice_id
            or receipt.candidate_count != len(batch.items)
        ):
            raise TerminalCaptureRequestError("upload_receipt_conflict")

    @staticmethod
    def _verify_slice_plan(
        group: ClaimedCaptureGroup,
        plan: CaptureGroupPlan,
        slices: tuple[CaptureSliceView, ...],
    ) -> None:
        if not isinstance(slices, tuple):
            raise TerminalCaptureRequestError("capture_slice_plan_mismatch")
        if tuple(item.slice_id for item in slices) != tuple(
            item.slice_id for item in plan.slices
        ):
            raise TerminalCaptureRequestError("capture_slice_plan_mismatch")
        for view, local in zip(slices, plan.slices, strict=True):
            ownership = view.ownership
            if (
                view.request_id != group.request_id
                or ownership.repository_id != group.repository_id
                or ownership.route_id != local.route_id
                or ownership.route_configuration_version
                != local.route_configuration_version
                or ownership.decision_space_id != local.decision_space_id
                or ownership.source_boundary_digest
                != local.source_boundary_digest
            ):
                raise TerminalCaptureRequestError(
                    "capture_slice_plan_mismatch"
                )

    @staticmethod
    def _route_context(
        view: CaptureSliceView, plan: CaptureSlicePlan
    ) -> FrozenCaptureRouteContext:
        ownership = view.ownership
        return FrozenCaptureRouteContext(
            decision_space_id=ownership.decision_space_id,
            decision_space_kind=ownership.decision_space_kind,
            decision_space_name=ownership.compatibility_product_name,
            route_id=ownership.route_id,
            route_configuration_version=(
                ownership.route_configuration_version
            ),
            compatibility_product_id=ownership.compatibility_product_id,
            matched_path_digest=plan.matched_path_digest,
        )

    def _selected_session_id(
        self, group: ClaimedCaptureGroup
    ) -> str | None:
        if group.capture_scope == "all_valid_sessions":
            return None
        binding = self.control_store.get_by_client_action_id(
            group.client_action_id
        )
        if binding is None:
            raise TerminalCaptureRequestError(
                "current_session_intent_missing"
            )
        if (
            binding.repository_id != group.repository_id
            or binding.chosen_scope != group.capture_scope
        ):
            raise TerminalCaptureRequestError(
                "current_session_intent_mismatch"
            )
        return binding.session_id

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("clock must return an aware datetime")
        return value


def _candidate_slice_batch(
    request_id: str,
    slice_view: CaptureSliceView,
    revisions: tuple[CandidateFamilyRevision, ...],
) -> CandidateSliceBatchUpload:
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
    return CandidateSliceBatchUpload(
        request_id=request_id,
        slice_id=slice_view.slice_id,
        route_id=slice_view.ownership.route_id,
        route_configuration_version=(
            slice_view.ownership.route_configuration_version
        ),
        decision_space_id=slice_view.ownership.decision_space_id,
        items=items,
        batch_digest=hashlib.sha256(
            canonical_json_bytes(
                {"items": [item.to_dict() for item in items]}
            )
        ).hexdigest(),
    )
