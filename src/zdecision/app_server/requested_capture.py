"""Request-authorized Capture through disposable whole-pipeline attempts."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from zdecision.agent.capture_operation_store import CaptureOperationStore
from zdecision.agent.session_index import FrozenSessionSource
from zdecision.app_server.gateway import (
    AppServerGateway,
    AppServerGatewayError,
    FrozenModelProfileUnavailable,
    IncompleteSourceTurn,
    InvalidAppServerResponse,
    UnknownSourceTurn,
)
from zdecision.app_server.jsonl import (
    AppServerEOF,
    AppServerError,
    AppServerProtocolError,
    AppServerRequestError,
    AppServerTimeout,
)
from zdecision.app_server.models import (
    AppServerTurnReceipt,
    FeasibilityModelProfile,
    extraction_output_schema,
    inventory_output_schema,
)
from zdecision.capture.inventory import (
    InventoryValidationError,
    validate_inventory,
)
from zdecision.capture.models import Candidate
from zdecision.capture.on_demand import (
    CaptureCommit,
    CaptureOperation,
    FrozenCaptureInput,
    FrozenCaptureRouteContext,
    ValidatedCaptureResult,
)
from zdecision.capture.service import ExtractionValidationError
from zdecision.capture.templates import TemplateCatalog
from zdecision.jsonio import canonical_json_bytes


class RequestedCaptureError(Exception):
    """Base class for request-authorized Capture failures."""


class SourceNotInteractive(RequestedCaptureError):
    """The observed Session is not an interactive Codex source."""


class SourceBoundaryUnavailable(RequestedCaptureError):
    """The exact frozen source boundary can no longer be read."""


class CaptureAttemptRetryable(RequestedCaptureError):
    """One disposable model-compute generation must be replaced."""


class RequestedCaptureFailed(RequestedCaptureError):
    """The persisted Capture cannot continue safely."""


class FrozenModelUnavailable(RequestedCaptureError):
    """The exact model profile frozen for the request cannot run."""


@dataclass(frozen=True)
class SessionCaptureResult:
    status: Literal["completed"]
    source_key: str
    capture_operation_id: str
    inventory_turn_id: str
    extraction_turn_id: str
    observations: tuple[Candidate, ...]
    evidence_digest: str
    model_profile: FeasibilityModelProfile


class RequestedCaptureRunner:
    """Run Inventory and Extraction in one fresh read-only attempt."""

    def __init__(
        self,
        *,
        gateway: AppServerGateway,
        operation_store: CaptureOperationStore,
        template_catalog: TemplateCatalog,
    ) -> None:
        self.gateway = gateway
        if not isinstance(operation_store, CaptureOperationStore):
            raise TypeError(
                "operation_store must be a CaptureOperationStore"
            )
        if not isinstance(template_catalog, TemplateCatalog):
            raise TypeError("template_catalog must be a TemplateCatalog")
        self.operation_store = operation_store
        self.template_catalog = template_catalog

    def run(
        self,
        source: FrozenSessionSource,
        *,
        route_context: FrozenCaptureRouteContext,
        matched_paths: tuple[str, ...],
        template_id: str,
        model_profile: FeasibilityModelProfile,
        heartbeat: Callable[[], None] | None = None,
    ) -> SessionCaptureResult:
        if not isinstance(source, FrozenSessionSource):
            raise TypeError("source must be a FrozenSessionSource")
        if not isinstance(model_profile, FeasibilityModelProfile):
            raise TypeError("model_profile must be a FeasibilityModelProfile")
        if not isinstance(route_context, FrozenCaptureRouteContext):
            raise TypeError(
                "route_context must be a FrozenCaptureRouteContext"
            )
        if (
            not isinstance(matched_paths, tuple)
            or any(not isinstance(path, str) or not path for path in matched_paths)
        ):
            raise TypeError("matched_paths must contain path strings")
        matched_digest = hashlib.sha256(
            canonical_json_bytes({"paths": list(matched_paths)})
        ).hexdigest()
        if matched_digest != route_context.matched_path_digest:
            raise ValueError("matched_path_digest_mismatch")
        product = route_context.decision_space_name
        template_id_value = _nonempty(template_id, "template_id")
        self.sweep_archives()

        existing = self.operation_store.operation_for_source(
            source.request_id,
            source.source_key,
            route_context.decision_space_id,
        )
        try:
            interactive = self.gateway.list_interactive_thread_ids(
                source.cwd
            )
        except (AppServerError, AppServerGatewayError) as error:
            self._fail_unavailable(existing)
            raise SourceBoundaryUnavailable(
                "Interactive source discovery is unavailable"
            ) from error
        if source.session_id not in interactive:
            raise SourceNotInteractive(
                "Frozen source is not an interactive Codex Session"
            )
        try:
            boundary = self.gateway.read_completed_boundary(
                source.session_id, source.upper_turn_id
            )
        except (
            AppServerError,
            UnknownSourceTurn,
            IncompleteSourceTurn,
            InvalidAppServerResponse,
        ) as error:
            self._fail_unavailable(existing)
            raise SourceBoundaryUnavailable(
                "Frozen source boundary is unavailable"
            ) from error
        if boundary.cwd != source.cwd:
            self._fail_unavailable(existing)
            raise SourceBoundaryUnavailable(
                "Frozen source cwd no longer matches"
            )

        if existing is None:
            profile = model_profile
            template = self.template_catalog.render(
                template_id_value, product
            )
            frozen = FrozenCaptureInput.create(
                request_id=source.request_id,
                repository_id=source.repository_id,
                source_key=source.source_key,
                session_id=source.session_id,
                cwd=source.cwd,
                lineage=source.lineage,
                previous_handled_turn_id=(
                    source.previous_handled_turn_id
                ),
                upper_turn_id=source.upper_turn_id,
                source_fingerprint=source.source_fingerprint,
                product=product,
                template=template,
                model_profile_id=profile.profile_id,
                model_id=profile.model_id,
                reasoning_effort=profile.reasoning_effort,
                model_discovery_digest=profile.discovery_digest,
                model_discovered_at=profile.discovered_at,
                route_context=route_context,
            )
            operation = self.operation_store.ensure_operation(frozen)
        else:
            operation = existing
            self._verify_replay_input(
                operation,
                source,
                product,
                route_context,
                template_id_value,
                model_profile,
            )
            profile = model_profile

        if operation.status == "failed_terminal":
            raise SourceBoundaryUnavailable(
                "Frozen source operation has failed terminally"
            )
        committed = self.operation_store.committed_capture(
            operation.operation_id
        )
        if committed is not None:
            return self._result(source, committed, profile)

        validated_attempt = self.operation_store.active_validated_attempt(
            operation.operation_id
        )
        if validated_attempt is not None:
            committed = self.operation_store.commit_attempt(
                validated_attempt.attempt_id
            )
            self.sweep_archives()
            if committed.result is None:
                raise CaptureAttemptRetryable(
                    "Validated Capture generation was superseded before commit"
                )
            return self._result(source, committed, profile)

        attempt = self.operation_store.begin_attempt(
            operation.operation_id, _now()
        )
        try:
            fork_thread_id = self.gateway.fork_disposable_thread(
                operation.frozen.session_id,
                operation.frozen.upper_turn_id,
            )
        except (AppServerError, AppServerGatewayError) as error:
            self._abandon(
                attempt.attempt_id,
                _attempt_failure_code("fork", error),
            )
            raise CaptureAttemptRetryable(
                "Disposable Capture fork must be retried"
            ) from error
        attempt = self.operation_store.attach_thread(
            attempt.attempt_id, fork_thread_id
        )

        try:
            _heartbeat(heartbeat)
            inventory_receipt = self.gateway.run_structured_turn(
                thread_id=fork_thread_id,
                prompt=operation.frozen.template.inventory_prompt,
                output_schema=inventory_output_schema(),
                profile=profile,
                cwd=operation.frozen.cwd,
            )
            _heartbeat(heartbeat)
            _verify_receipt(
                inventory_receipt, fork_thread_id, profile
            )
            self.operation_store.attach_turn(
                attempt.attempt_id,
                "inventory",
                inventory_receipt.turn_id,
            )
            validate_inventory(inventory_receipt.structured_output)
        except (
            AppServerError,
            AppServerGatewayError,
            InventoryValidationError,
            RequestedCaptureFailed,
        ) as error:
            self._abandon(
                attempt.attempt_id,
                _attempt_failure_code("inventory", error),
            )
            raise CaptureAttemptRetryable(
                "Disposable Inventory attempt must be retried"
            ) from error

        try:
            _heartbeat(heartbeat)
            extraction_receipt = self.gateway.run_structured_turn(
                thread_id=fork_thread_id,
                prompt=(
                    operation.frozen.template.extraction_prompt
                    + _leaf_instruction(route_context, matched_paths)
                ),
                output_schema=extraction_output_schema(
                    operation.frozen.product
                ),
                profile=profile,
                cwd=operation.frozen.cwd,
            )
            _heartbeat(heartbeat)
            _verify_receipt(
                extraction_receipt, fork_thread_id, profile
            )
            self.operation_store.attach_turn(
                attempt.attempt_id,
                "extraction",
                extraction_receipt.turn_id,
            )
            validated = ValidatedCaptureResult.create(
                operation.frozen,
                inventory_receipt.structured_output,
                extraction_receipt.structured_output,
            )
        except (
            AppServerError,
            AppServerGatewayError,
            InventoryValidationError,
            ExtractionValidationError,
            RequestedCaptureFailed,
        ) as error:
            self._abandon(
                attempt.attempt_id,
                _attempt_failure_code("extraction", error),
            )
            raise CaptureAttemptRetryable(
                "Disposable Extraction attempt must be retried"
            ) from error

        self.operation_store.store_validated_attempt(
            attempt.attempt_id, validated, _now()
        )
        committed = self.operation_store.commit_attempt(
            attempt.attempt_id
        )
        self.sweep_archives()
        if committed.result is None:
            raise CaptureAttemptRetryable(
                "Capture generation was superseded before commit"
            )
        return self._result(source, committed, profile)

    def operation_profile(
        self,
        source: FrozenSessionSource,
        route_context: FrozenCaptureRouteContext | None = None,
    ) -> FeasibilityModelProfile | None:
        if not isinstance(source, FrozenSessionSource):
            raise TypeError("source must be a FrozenSessionSource")
        operation = self.operation_store.operation_for_source(
            source.request_id,
            source.source_key,
            None
            if route_context is None
            else route_context.decision_space_id,
        )
        return None if operation is None else _profile(operation.frozen)

    def resolve_request_profile(
        self, profile: FeasibilityModelProfile | None
    ) -> FeasibilityModelProfile:
        if profile is not None and not isinstance(
            profile, FeasibilityModelProfile
        ):
            raise TypeError("profile must be a FeasibilityModelProfile or None")
        try:
            if profile is None:
                return self.gateway.resolve_active_profile()
            return self.gateway.require_supported_profile(profile)
        except FrozenModelProfileUnavailable as error:
            raise FrozenModelUnavailable(
                "Frozen Capture model is unavailable"
            ) from error
        except (AppServerError, AppServerGatewayError) as error:
            raise CaptureAttemptRetryable(
                "Capture model resolution must be retried"
            ) from error

    def sweep_archives(self) -> None:
        """Retry archive work without reopening model computation."""

        for attempt in self.operation_store.pending_archives():
            assert attempt.thread_id is not None
            try:
                self.gateway.archive_thread(attempt.thread_id)
            except (AppServerError, AppServerGatewayError):
                continue
            self.operation_store.mark_archived(attempt.attempt_id)

    def _abandon(self, attempt_id: str, failure_code: str) -> None:
        self.operation_store.abandon_attempt(
            attempt_id, failure_code, _now()
        )
        self.sweep_archives()

    def _fail_unavailable(
        self, operation: CaptureOperation | None
    ) -> None:
        if operation is not None and operation.status == "open":
            self.operation_store.fail_operation_terminal(
                operation.operation_id,
                "source_boundary_unavailable",
            )

    @staticmethod
    def _verify_replay_input(
        operation: CaptureOperation,
        source: FrozenSessionSource,
        product: str,
        route_context: FrozenCaptureRouteContext,
        template_id: str,
        profile: FeasibilityModelProfile,
    ) -> None:
        frozen = operation.frozen
        expected = (
            source.request_id,
            source.repository_id,
            source.source_key,
            source.session_id,
            source.cwd,
            source.lineage,
            source.previous_handled_turn_id,
            source.upper_turn_id,
            source.source_fingerprint,
            product,
            route_context,
            template_id,
            profile.profile_id,
            profile.model_id,
            profile.reasoning_effort,
            profile.discovery_digest,
            profile.discovered_at,
        )
        actual = (
            frozen.request_id,
            frozen.repository_id,
            frozen.source_key,
            frozen.session_id,
            frozen.cwd,
            frozen.lineage,
            frozen.previous_handled_turn_id,
            frozen.upper_turn_id,
            frozen.source_fingerprint,
            frozen.product,
            frozen.route_context,
            frozen.template.template_id,
            frozen.model_profile_id,
            frozen.model_id,
            frozen.reasoning_effort,
            frozen.model_discovery_digest,
            frozen.model_discovered_at,
        )
        if actual != expected:
            raise RequestedCaptureFailed(
                "Capture replay input conflicts with its frozen operation"
            )

    @staticmethod
    def _result(
        source: FrozenSessionSource,
        commit: CaptureCommit,
        profile: FeasibilityModelProfile,
    ) -> SessionCaptureResult:
        if (
            commit.result is None
            or commit.attempt.inventory_turn_id is None
            or commit.attempt.extraction_turn_id is None
        ):
            raise RequestedCaptureFailed(
                "Committed Capture evidence is incomplete"
            )
        result = commit.result
        evidence_digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "capture_operation_id": result.operation_id,
                    "extraction": {
                        "output_sha256": result.extraction_sha256,
                        "turn_id": commit.attempt.extraction_turn_id,
                    },
                    "inventory": {
                        "output_sha256": result.inventory_sha256,
                        "turn_id": commit.attempt.inventory_turn_id,
                    },
                    "model_profile_id": profile.profile_id,
                    "result_digest": result.result_digest,
                    "source": {
                        "fingerprint": source.source_fingerprint,
                        "source_key": source.source_key,
                        "upper_turn_id": source.upper_turn_id,
                    },
                }
            )
        ).hexdigest()
        return SessionCaptureResult(
            status="completed",
            source_key=source.source_key,
            capture_operation_id=result.operation_id,
            inventory_turn_id=commit.attempt.inventory_turn_id,
            extraction_turn_id=commit.attempt.extraction_turn_id,
            observations=result.observations,
            evidence_digest=evidence_digest,
            model_profile=profile,
        )


def _verify_receipt(
    receipt: AppServerTurnReceipt,
    thread_id: str,
    profile: FeasibilityModelProfile,
) -> None:
    if receipt.thread_id != thread_id:
        raise RequestedCaptureFailed(
            "Structured Turn returned the wrong Thread"
        )
    if receipt.model_profile_id != profile.profile_id:
        raise RequestedCaptureFailed(
            "Structured Turn returned the wrong model profile"
        )


def _profile(frozen: FrozenCaptureInput) -> FeasibilityModelProfile:
    return FeasibilityModelProfile(
        profile_id=frozen.model_profile_id,
        model_id=frozen.model_id,
        reasoning_effort=frozen.reasoning_effort,
        discovery_digest=frozen.model_discovery_digest,
        discovered_at=frozen.model_discovered_at,
    )


def _attempt_failure_code(stage: str, error: Exception) -> str:
    if isinstance(
        error,
        (AppServerTimeout, AppServerEOF, AppServerProtocolError),
    ):
        return f"{stage}_result_unknown"
    if isinstance(error, AppServerRequestError):
        return f"{stage}_request_rejected"
    if isinstance(error, InventoryValidationError):
        return "invalid_inventory"
    if isinstance(error, ExtractionValidationError):
        return "invalid_extraction"
    return f"{stage}_result_invalid"


def _nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _leaf_instruction(
    route_context: FrozenCaptureRouteContext,
    matched_paths: tuple[str, ...],
) -> str:
    payload = canonical_json_bytes(
        {
            "decision_space_id": route_context.decision_space_id,
            "decision_space_kind": route_context.decision_space_kind,
            "decision_space_name": route_context.decision_space_name,
            "matched_paths": list(matched_paths),
        }
    ).decode("utf-8")
    return (
        "\n\nZDECISION_FIXED_DECISION_SPACE\n"
        "Extract Candidates only for this exact Decision space and only from "
        "the listed matched repository paths. Every Candidate product must "
        "equal decision_space_name. Do not infer or emit any other product or "
        "Shared leaf.\n"
        f"{payload}\n"
        "END_ZDECISION_FIXED_DECISION_SPACE"
    )


def _heartbeat(callback: Callable[[], None] | None) -> None:
    if callback is not None:
        callback()


def _now() -> str:
    return (
        datetime.now(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
