"""Persistent page-request processor for the on-demand Capture pipeline."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime

from zdecision.agent.central_client import CentralClientError
from zdecision.agent.capture_operation_store import (
    CaptureOperationStoreError,
)
from zdecision.agent.db import AgentDatabase
from zdecision.agent.request_state import (
    BatchConflict,
    RequestStateStore,
)
from zdecision.agent.service import (
    RetryableCaptureRequestError,
    TerminalCaptureRequestError,
)
from zdecision.agent.session_index import SessionIndex
from zdecision.app_server.reconciliation_runner import (
    ReconciliationRunnerError,
)
from zdecision.app_server.requested_capture import (
    CaptureAttemptRetryable,
    RequestedCaptureFailed,
    SessionCaptureResult,
    SourceBoundaryUnavailable,
    SourceNotInteractive,
)
from zdecision.capture.reconciliation import (
    CandidateFamilyRevision,
    ReconciliationResult,
)
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import (
    CandidateBatchUpload,
    CandidateRevisionUpload,
    ClaimedCaptureRequest,
    UploadReceipt,
)


_TRANSIENT_CENTRAL_CODES = frozenset(
    (
        "central_connection_unavailable",
        "central_temporarily_unavailable",
    )
)


class OnDemandCaptureProcessor:
    """Deliver one frozen Capture Request in crash-safe order."""

    def __init__(
        self,
        *,
        database: AgentDatabase,
        session_index: SessionIndex,
        capture_runner,
        reconciliation_runner,
        request_state: RequestStateStore,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(database, AgentDatabase):
            raise TypeError("database must be an AgentDatabase")
        if not isinstance(session_index, SessionIndex):
            raise TypeError("session_index must be a SessionIndex")
        if not isinstance(request_state, RequestStateStore):
            raise TypeError(
                "request_state must be a RequestStateStore"
            )
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.database = database
        self.session_index = session_index
        self.capture_runner = capture_runner
        self.reconciliation_runner = reconciliation_runner
        self.request_state = request_state
        self.clock = clock

    def process(
        self,
        request: ClaimedCaptureRequest,
        client,
    ) -> None:
        if not isinstance(request, ClaimedCaptureRequest):
            raise TypeError(
                "request must be a ClaimedCaptureRequest"
            )
        try:
            self._process(request, client)
        except (
            RetryableCaptureRequestError,
            TerminalCaptureRequestError,
        ):
            raise
        except CentralClientError as error:
            if error.code in _TRANSIENT_CENTRAL_CODES:
                raise RetryableCaptureRequestError(
                    error.code
                ) from error
            raise TerminalCaptureRequestError(
                error.code
            ) from error
        except (ConnectionError, TimeoutError) as error:
            raise RetryableCaptureRequestError(
                "central_connection_unavailable"
            ) from error
        except BatchConflict as error:
            raise TerminalCaptureRequestError(
                "local_delivery_conflict"
            ) from error
        except CaptureAttemptRetryable as error:
            raise RetryableCaptureRequestError(
                "capture_attempt_retryable"
            ) from error
        except SourceBoundaryUnavailable as error:
            raise TerminalCaptureRequestError(
                "source_boundary_unavailable"
            ) from error
        except CaptureOperationStoreError as error:
            raise TerminalCaptureRequestError(
                "local_capture_state_invalid"
            ) from error
        except RequestedCaptureFailed as error:
            raise TerminalCaptureRequestError(
                "capture_result_failed"
            ) from error
        except ReconciliationRunnerError as error:
            raise TerminalCaptureRequestError(
                "reconciliation_result_failed"
            ) from error
        except OSError as error:
            raise RetryableCaptureRequestError(
                "local_state_unavailable"
            ) from error

    def _process(
        self,
        request: ClaimedCaptureRequest,
        client,
    ) -> None:
        self.capture_runner.sweep_archives()
        client.start(request.request_id, request.lease_token)
        sources = self.session_index.freeze_sources(
            request.request_id,
            request.repository_id,
            self._now(),
        )
        self._require_matching_local_mapping(request)

        staged = self.request_state.staged_batch(
            request.request_id
        )
        if staged is not None:
            self._deliver(request, client, staged)
            return

        result = self.request_state.get_reconciliation(
            request.request_id
        )
        if result is None:
            captures = self._capture_sources(
                request, client, sources
            )
            observations = tuple(
                observation
                for _, capture in captures
                for observation in capture.observations
            )
            if observations:
                profiles = {
                    capture.model_profile.profile_id
                    for _, capture in captures
                }
                if len(profiles) != 1:
                    raise TerminalCaptureRequestError(
                        "model_profile_mismatch"
                    )
                client.progress(
                    request.request_id,
                    request.lease_token,
                    "reconciling_candidates",
                )
                client.heartbeat(
                    request.request_id, request.lease_token
                )
                result = self.reconciliation_runner.run(
                    request_id=request.request_id,
                    repository_id=request.repository_id,
                    cwd=min(
                        source.cwd for source, _ in captures
                    ),
                    observations=observations,
                    current=self.request_state.current_families(
                        request.repository_id
                    ),
                    profile=captures[0][1].model_profile,
                    heartbeat=lambda: client.heartbeat(
                        request.request_id,
                        request.lease_token,
                    ),
                )
                client.heartbeat(
                    request.request_id, request.lease_token
                )
            else:
                result = ReconciliationResult.empty(
                    request.repository_id
                )
                self.request_state.save_reconciliation(
                    request.request_id, result
                )
        if result.repository_id != request.repository_id:
            raise TerminalCaptureRequestError(
                "reconciliation_repository_mismatch"
            )
        batch = _candidate_batch(
            request.request_id,
            request.repository_id,
            result.uploadable_revisions,
        )
        self.request_state.stage_batch(
            request.request_id,
            result.uploadable_revisions,
            batch,
        )
        client.progress(
            request.request_id,
            request.lease_token,
            "uploading_candidates",
        )
        self._deliver(request, client, batch)

    def _capture_sources(
        self,
        request: ClaimedCaptureRequest,
        client,
        sources,
    ) -> tuple[tuple[object, SessionCaptureResult], ...]:
        if sources:
            client.progress(
                request.request_id,
                request.lease_token,
                "capturing_sessions",
            )
        captures: list[
            tuple[object, SessionCaptureResult]
        ] = []
        for source in sources:
            client.heartbeat(
                request.request_id, request.lease_token
            )
            try:
                capture = self.capture_runner.run(
                    source,
                    product_name=request.product_name,
                    template_id=request.template_id,
                    heartbeat=lambda: client.heartbeat(
                        request.request_id,
                        request.lease_token,
                    ),
                )
            except SourceNotInteractive:
                self.session_index.mark_excluded(
                    request.request_id,
                    source.source_key,
                    "subagent_session",
                )
                client.heartbeat(
                    request.request_id, request.lease_token
                )
                continue
            if not isinstance(capture, SessionCaptureResult):
                raise TerminalCaptureRequestError(
                    "capture_result_invalid"
                )
            if capture.source_key != source.source_key:
                raise TerminalCaptureRequestError(
                    "capture_source_mismatch"
                )
            captures.append((source, capture))
            client.heartbeat(
                request.request_id, request.lease_token
            )
        return tuple(captures)

    def _require_matching_local_mapping(
        self, request: ClaimedCaptureRequest
    ) -> None:
        mapping = self.database.get_repository_mapping(
            request.repository_id
        )
        if (
            mapping is None
            or not mapping.enabled
            or mapping.product_id != request.product_id
            or mapping.product_name != request.product_name
        ):
            raise TerminalCaptureRequestError(
                "repository_mapping_mismatch"
            )

    def _deliver(
        self,
        request: ClaimedCaptureRequest,
        client,
        batch: CandidateBatchUpload,
    ) -> None:
        receipt = self.request_state.upload_receipt(
            request.request_id
        )
        if receipt is None:
            receipt = client.upload_candidates(
                request.lease_token, batch
            )
            if not isinstance(receipt, UploadReceipt):
                raise TerminalCaptureRequestError(
                    "upload_receipt_invalid"
                )
            self.request_state.mark_uploaded(receipt)
        if (
            receipt.request_id != request.request_id
            or receipt.batch_digest != batch.batch_digest
        ):
            raise TerminalCaptureRequestError(
                "upload_receipt_conflict"
            )
        self.session_index.acknowledge(
            request.request_id,
            receipt.batch_digest,
            _parse_timestamp(receipt.acknowledged_at),
        )
        client.complete(
            request.request_id,
            request.lease_token,
            receipt.batch_digest,
        )

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError(
                "clock must return an aware datetime"
            )
        return value


def _candidate_batch(
    request_id: str,
    repository_id: str,
    revisions: tuple[CandidateFamilyRevision, ...],
) -> CandidateBatchUpload:
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
    return CandidateBatchUpload(
        request_id=request_id,
        repository_id=repository_id,
        items=items,
        batch_digest=hashlib.sha256(
            canonical_json_bytes(
                {"items": [item.to_dict() for item in items]}
            )
        ).hexdigest(),
    )


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except (AttributeError, ValueError) as error:
        raise TerminalCaptureRequestError(
            "upload_receipt_timestamp_invalid"
        ) from error
    if parsed.tzinfo is None:
        raise TerminalCaptureRequestError(
            "upload_receipt_timestamp_invalid"
        )
    return parsed.astimezone(UTC)
