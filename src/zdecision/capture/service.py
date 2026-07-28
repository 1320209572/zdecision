"""Deterministic Capture state transitions and extraction validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone

from zdecision.capture.models import (
    Candidate,
    CandidateContent,
    CandidateSet,
    CapturePlan,
    CaptureRecord,
    SourceCheckpoint,
)
from zdecision.capture.prompts import build_extraction_prompt
from zdecision.ids import capture_operation_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.private_store.filesystem import FilePrivateStore


_MAX_CANDIDATES = 20
_MAX_CANDIDATE_BYTES = 16 * 1024
_RESULT_FIELDS = frozenset(("candidates",))
_CANDIDATE_FIELDS = frozenset(
    ("product", "claim", "future_action", "scope", "invalidation_conditions")
)
_SCOPE_FIELDS = frozenset(("summary", "repositories", "paths"))


class CaptureError(Exception):
    """Base class for Capture domain errors."""


class CaptureNotFound(CaptureError):
    """Raised when an operation id has no private Capture record."""


class CaptureStateError(CaptureError):
    """Raised when an operation is not in the required state."""


class CaptureForkAmbiguous(CaptureError):
    """Raised when a fork may exist but has not been attached."""

    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        super().__init__(
            f"Capture {operation_id} is prepared but its fork is not attached; "
            "reconcile the native fork result before retrying."
        )


class CaptureForkConflict(CaptureError):
    """Raised when a different fork is attached to an operation."""


class ExtractionValidationError(CaptureError):
    """Raised when extractor output does not match the strict contract."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExtractionValidationError(f"{field} must be a non-empty string")
    return value


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise ExtractionValidationError(f"{field} must be a list of strings")
    return tuple(value)


def _require_exact_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    name: str,
) -> None:
    actual = frozenset(value)
    if actual != expected:
        unknown = sorted(repr(field) for field in actual - expected)
        missing = sorted(expected - actual)
        raise ExtractionValidationError(
            f"Invalid {name} fields: unknown={unknown}, missing={missing}"
        )


class CaptureService:
    """Own the private prepare, fork attachment, and completion boundary."""

    def __init__(self, store: FilePrivateStore) -> None:
        self.store = store

    def prepare(
        self,
        source_thread_id: str,
        source_turn_id: str,
        product: str,
    ) -> CapturePlan:
        operation_id = capture_operation_id(
            source_thread_id,
            source_turn_id,
            product,
        )
        existing = self.store.get_capture(operation_id)
        if existing is None:
            record = CaptureRecord.started(
                operation_id=operation_id,
                source=SourceCheckpoint(source_thread_id, source_turn_id),
                product=product,
            )
            self.store.put_capture(record)
            return CapturePlan(
                record=record,
                extraction_prompt=build_extraction_prompt(product),
                replayed=False,
            )
        if existing.status == "prepared":
            raise CaptureForkAmbiguous(operation_id)
        if existing.status in ("fork_attached", "completed"):
            return CapturePlan(
                record=existing,
                extraction_prompt=build_extraction_prompt(existing.product),
                replayed=True,
            )
        raise CaptureStateError(
            f"Capture {operation_id} cannot be prepared from state {existing.status!r}"
        )

    def attach_fork(
        self,
        operation_id: str,
        fork_thread_id: str,
    ) -> CaptureRecord:
        record = self._required_capture(operation_id)
        if record.status == "prepared":
            attached = replace(
                record,
                status="fork_attached",
                fork_thread_id=fork_thread_id,
                updated_at=_now(),
            )
            self.store.put_capture(attached)
            return attached
        if record.status in ("fork_attached", "completed"):
            if record.fork_thread_id == fork_thread_id:
                return record
            raise CaptureForkConflict(
                f"Capture {operation_id} is already attached to a different fork"
            )
        raise CaptureStateError(
            f"Capture {operation_id} cannot attach a fork from state {record.status!r}"
        )

    def complete(
        self,
        operation_id: str,
        extraction: Mapping[str, object],
    ) -> CandidateSet:
        record = self._required_capture(operation_id)
        if record.status == "completed":
            return CandidateSet(
                operation_id=record.operation_id,
                status="completed",
                candidate_ids=record.candidate_ids,
            )
        if record.status != "fork_attached":
            raise CaptureStateError(
                f"Capture {operation_id} requires an attached fork before completion"
            )

        candidates = self._validated_candidates(record, extraction)
        for candidate in candidates:
            self.store.put_candidate(candidate)

        candidate_ids = tuple(candidate.candidate_id for candidate in candidates)
        completed = replace(
            record,
            status="completed",
            candidate_ids=candidate_ids,
            updated_at=_now(),
        )
        self.store.put_capture(completed)
        return CandidateSet(
            operation_id=operation_id,
            status="completed",
            candidate_ids=candidate_ids,
        )

    def get(self, operation_id: str) -> CaptureRecord:
        return self._required_capture(operation_id)

    def _required_capture(self, operation_id: str) -> CaptureRecord:
        record = self.store.get_capture(operation_id)
        if record is None:
            raise CaptureNotFound(f"Capture {operation_id!r} does not exist")
        return record

    def _validated_candidates(
        self,
        record: CaptureRecord,
        extraction: Mapping[str, object],
    ) -> tuple[Candidate, ...]:
        if not isinstance(extraction, Mapping):
            raise ExtractionValidationError("Extraction result must be an object")
        _require_exact_fields(extraction, _RESULT_FIELDS, "extraction result")
        raw_candidates = extraction["candidates"]
        if not isinstance(raw_candidates, list):
            raise ExtractionValidationError("candidates must be a list")
        if len(raw_candidates) > _MAX_CANDIDATES:
            raise ExtractionValidationError(
                f"candidates must contain at most {_MAX_CANDIDATES} items"
            )

        validated: list[Candidate] = []
        for ordinal, raw_candidate in enumerate(raw_candidates, start=1):
            if not isinstance(raw_candidate, Mapping):
                raise ExtractionValidationError(
                    f"candidate {ordinal} must be an object"
                )
            _require_exact_fields(
                raw_candidate,
                _CANDIDATE_FIELDS,
                f"candidate {ordinal}",
            )
            scope = raw_candidate["scope"]
            if not isinstance(scope, Mapping):
                raise ExtractionValidationError(
                    f"candidate {ordinal}.scope must be an object"
                )
            _require_exact_fields(scope, _SCOPE_FIELDS, f"candidate {ordinal}.scope")

            product = _require_nonempty_string(
                raw_candidate["product"],
                f"candidate {ordinal}.product",
            )
            if product != record.product:
                raise ExtractionValidationError(
                    f"candidate {ordinal}.product must match the Capture product"
                )
            claim = _require_nonempty_string(
                raw_candidate["claim"],
                f"candidate {ordinal}.claim",
            )
            future_action = _require_nonempty_string(
                raw_candidate["future_action"],
                f"candidate {ordinal}.future_action",
            )
            scope_summary = _require_nonempty_string(
                scope["summary"],
                f"candidate {ordinal}.scope.summary",
            )
            repositories = _string_list(
                scope["repositories"],
                f"candidate {ordinal}.scope.repositories",
            )
            paths = _string_list(
                scope["paths"],
                f"candidate {ordinal}.scope.paths",
            )
            invalidation_conditions = _string_list(
                raw_candidate["invalidation_conditions"],
                f"candidate {ordinal}.invalidation_conditions",
            )
            encoded_candidate = {
                "product": product,
                "claim": claim,
                "future_action": future_action,
                "scope": {
                    "summary": scope_summary,
                    "repositories": list(repositories),
                    "paths": list(paths),
                },
                "invalidation_conditions": list(invalidation_conditions),
            }
            if (
                len(canonical_json_bytes(encoded_candidate))
                > _MAX_CANDIDATE_BYTES
            ):
                raise ExtractionValidationError(
                    f"candidate {ordinal} exceeds {_MAX_CANDIDATE_BYTES} encoded bytes"
                )

            candidate_id = f"cand_{operation_id_suffix(record.operation_id)}_{ordinal:02d}"
            validated.append(
                Candidate(
                    candidate_id=candidate_id,
                    capture_id=record.operation_id,
                    ordinal=ordinal,
                    content=CandidateContent(
                        product=product,
                        claim=claim,
                        future_action=future_action,
                        scope_summary=scope_summary,
                        repositories=repositories,
                        paths=paths,
                        invalidation_conditions=invalidation_conditions,
                    ),
                    source=record.source,
                )
            )
        return tuple(validated)


def operation_id_suffix(operation_id: str) -> str:
    """Return the digest portion used by deterministic Candidate ids."""

    return operation_id.removeprefix("cap_")
