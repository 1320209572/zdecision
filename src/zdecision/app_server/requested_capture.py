"""Request-authorized, exact-boundary two-stage decision Capture."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from zdecision.agent.request_state import (
    NativeCallCoordinator,
    RequestStateStore,
)
from zdecision.agent.session_index import FrozenSessionSource
from zdecision.app_server.gateway import AppServerGateway
from zdecision.app_server.models import (
    AppServerTurnReceipt,
    FeasibilityModelProfile,
    extraction_output_schema,
    inventory_output_schema,
)
from zdecision.capture.models import Candidate, CaptureRecord
from zdecision.capture.service import (
    CaptureForkAmbiguous,
    CaptureService,
)
from zdecision.jsonio import canonical_json_bytes


class RequestedCaptureError(Exception):
    """Base class for request-authorized Capture failures."""


class SourceNotInteractive(RequestedCaptureError):
    """The frozen source is not an exact interactive Codex boundary."""


class RequestedCaptureFailed(RequestedCaptureError):
    """The persisted Capture cannot continue to a completed result."""


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
    """Run Inventory and Extraction only after a browser Capture Request."""

    def __init__(
        self,
        *,
        gateway: AppServerGateway,
        capture_service: CaptureService,
        request_state: RequestStateStore,
    ) -> None:
        self.gateway = gateway
        if not isinstance(capture_service, CaptureService):
            raise TypeError("capture_service must be a CaptureService")
        if not isinstance(request_state, RequestStateStore):
            raise TypeError("request_state must be a RequestStateStore")
        self.capture_service = capture_service
        self.request_state = request_state
        self.native_calls = NativeCallCoordinator(request_state)

    def run(
        self,
        source: FrozenSessionSource,
        *,
        product_name: str,
        template_id: str,
    ) -> SessionCaptureResult:
        if not isinstance(source, FrozenSessionSource):
            raise TypeError("source must be a FrozenSessionSource")
        product = _nonempty(product_name, "product_name")
        template = _nonempty(template_id, "template_id")

        interactive = self.gateway.list_interactive_thread_ids(source.cwd)
        if source.session_id not in interactive:
            raise SourceNotInteractive(
                "Frozen source is not an interactive Codex Session"
            )
        boundary = self.gateway.read_completed_boundary(
            source.session_id, source.upper_turn_id
        )
        if boundary.cwd != source.cwd:
            raise SourceNotInteractive(
                "Frozen source and app-server cwd do not match"
            )
        profile = self.gateway.discover_and_freeze_profile(boundary)

        try:
            plan = self.capture_service.prepare(
                source.session_id,
                source.upper_turn_id,
                product,
                template,
            )
        except CaptureForkAmbiguous as error:
            plan = self.capture_service.resume(error.operation_id)
        operation_id = plan.record.operation_id
        record = _record(self.capture_service.get(operation_id))
        if record.status == "failed":
            raise RequestedCaptureFailed(
                "The requested Capture previously failed"
            )
        if record.status == "completed":
            return self._result(source, record, profile)

        fork_tag = f"zdecision/capture/{operation_id}"
        fork_thread_id = self.native_calls.resolve_thread(
            request_id=source.request_id,
            operation_key=source.source_key,
            stage="capture_fork",
            stable_tag=fork_tag,
            find=lambda tag: self.gateway.find_thread_by_source(
                tag, cwd=boundary.cwd
            ),
            create=lambda: self.gateway.fork_ephemeral(
                source.session_id,
                source.upper_turn_id,
                thread_source=fork_tag,
            ),
        )
        self.capture_service.attach_fork(operation_id, fork_thread_id)
        record = _record(self.capture_service.get(operation_id))

        if record.status in {"fork_attached", "inventory_running"}:
            inventory_tag = f"zdecision/{operation_id}/inventory"
            inventory_receipt = self.native_calls.resolve_structured_turn(
                request_id=source.request_id,
                operation_key=source.source_key,
                stage="inventory",
                stable_tag=inventory_tag,
                read=lambda tag: self.gateway.read_structured_turn_by_client_id(
                    fork_thread_id, tag, profile
                ),
                create=lambda: self.gateway.run_structured_turn(
                    thread_id=fork_thread_id,
                    prompt=plan.inventory_prompt,
                    output_schema=inventory_output_schema(),
                    profile=profile,
                    cwd=boundary.cwd,
                    client_user_message_id=inventory_tag,
                ),
            )
            _verify_receipt(inventory_receipt, fork_thread_id, profile)
            self.capture_service.attach_stage_turn(
                operation_id, "inventory", inventory_receipt.turn_id
            )
            self.capture_service.complete_inventory(
                operation_id,
                inventory_receipt.structured_output,
                raw_output_sha256=inventory_receipt.output_sha256,
            )
            record = _record(self.capture_service.get(operation_id))

        if record.status in {"inventory_completed", "extraction_running"}:
            extraction_tag = f"zdecision/{operation_id}/extraction"
            extraction_receipt = self.native_calls.resolve_structured_turn(
                request_id=source.request_id,
                operation_key=source.source_key,
                stage="extraction",
                stable_tag=extraction_tag,
                read=lambda tag: self.gateway.read_structured_turn_by_client_id(
                    fork_thread_id, tag, profile
                ),
                create=lambda: self.gateway.run_structured_turn(
                    thread_id=fork_thread_id,
                    prompt=plan.extraction_prompt,
                    output_schema=extraction_output_schema(product),
                    profile=profile,
                    cwd=boundary.cwd,
                    client_user_message_id=extraction_tag,
                ),
            )
            _verify_receipt(extraction_receipt, fork_thread_id, profile)
            self.capture_service.attach_stage_turn(
                operation_id, "extraction", extraction_receipt.turn_id
            )
            self.capture_service.complete_extraction(
                operation_id,
                extraction_receipt.structured_output,
                raw_output_sha256=extraction_receipt.output_sha256,
            )
            record = _record(self.capture_service.get(operation_id))

        if record.status != "completed":
            raise RequestedCaptureFailed(
                "The requested Capture did not reach a completed state"
            )
        return self._result(source, record, profile)

    def _result(
        self,
        source: FrozenSessionSource,
        record: CaptureRecord,
        profile: FeasibilityModelProfile,
    ) -> SessionCaptureResult:
        if (
            record.status != "completed"
            or record.inventory_turn_id is None
            or record.extraction_turn_id is None
            or record.inventory_sha256 is None
            or record.extraction_sha256 is None
        ):
            raise RequestedCaptureFailed(
                "Completed Capture evidence is incomplete"
            )
        observations = self.capture_service.get_candidates(
            record.operation_id
        )
        evidence_digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "candidate_ids": [
                        candidate.candidate_id
                        for candidate in observations
                    ],
                    "capture_operation_id": record.operation_id,
                    "extraction": {
                        "output_sha256": record.extraction_sha256,
                        "turn_id": record.extraction_turn_id,
                    },
                    "inventory": {
                        "output_sha256": record.inventory_sha256,
                        "turn_id": record.inventory_turn_id,
                    },
                    "model_profile_id": profile.profile_id,
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
            capture_operation_id=record.operation_id,
            inventory_turn_id=record.inventory_turn_id,
            extraction_turn_id=record.extraction_turn_id,
            observations=observations,
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


def _record(value: object) -> CaptureRecord:
    if not isinstance(value, CaptureRecord):
        raise RequestedCaptureFailed(
            "Legacy Capture records cannot serve a page request"
        )
    return value


def _nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value
