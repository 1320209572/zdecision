"""Deterministic two-stage Capture state transitions and validation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Literal

from zdecision.capture.inventory import (
    InventoryResult,
    InventoryValidationError,
    validate_inventory,
)
from zdecision.capture.models import (
    Candidate,
    CandidateContent,
    CandidateSet,
    CapturePlan,
    CaptureRecord,
    ExtractionManifest,
    LegacyCaptureRecord,
    SourceCheckpoint,
    StageFailure,
    StageName,
    stage_failure_message,
)
from zdecision.capture.templates import TemplateCatalog
from zdecision.ids import capture_candidate_id, capture_operation_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.private_store.filesystem import (
    FilePrivateStore,
    PrivateStateConflict,
    PrivateStateCorrupt,
)


_MAX_CANDIDATES = 20
_MAX_CANDIDATE_BYTES = 16 * 1024
_RESULT_FIELDS = frozenset(("candidates",))
_CANDIDATE_FIELDS = frozenset(
    ("product", "claim", "future_action", "scope", "invalidation_conditions")
)
_SCOPE_FIELDS = frozenset(("summary", "repositories", "paths"))
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXPLICIT_FAILURE_CODES = frozenset(
    (
        "model_refusal",
        "model_timeout",
        "native_unavailable",
        "model_contract_violation",
    )
)


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


class CaptureTurnConflict(CaptureError):
    """Raised when a different native Turn is attached to a stage."""


class ExtractionValidationError(CaptureError):
    """Raised when Stage 2 output does not match the strict contract."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _invalid_extraction() -> ExtractionValidationError:
    return ExtractionValidationError(
        "invalid_extraction",
        "Extraction output does not match the required schema",
    )


def _require_nonempty_string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid_extraction()
    return value


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise _invalid_extraction()
    return tuple(value)


def _require_exact_fields(
    value: Mapping[str, object], expected: frozenset[str]
) -> None:
    if frozenset(value) != expected:
        raise _invalid_extraction()


def _output_digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _output_digest_with_fallback(
    value: object,
    raw_output_sha256: str | None,
) -> str:
    try:
        return _output_digest(value)
    except (TypeError, ValueError):
        if (
            not isinstance(raw_output_sha256, str)
            or _SHA256.fullmatch(raw_output_sha256) is None
        ):
            raise CaptureStateError(
                "Capture output cannot be assigned a valid digest"
            ) from None
        return raw_output_sha256


def _persisted_extraction_candidate(candidate: Candidate) -> dict[str, object]:
    return {
        "product": candidate.content.product,
        "claim": candidate.content.claim,
        "future_action": candidate.content.future_action,
        "scope": {
            "summary": candidate.content.scope_summary,
            "repositories": list(candidate.content.repositories),
            "paths": list(candidate.content.paths),
        },
        "invalidation_conditions": list(candidate.content.invalidation_conditions),
    }


class CaptureService:
    """Own the private prepare, native attachment, and completion boundary."""

    def __init__(self, store: FilePrivateStore, catalog: TemplateCatalog) -> None:
        self.store = store
        self.catalog = catalog

    def prepare(
        self,
        source_thread_id: str,
        source_turn_id: str,
        product: str,
        template_id: str = "business",
    ) -> CapturePlan:
        snapshot = self.catalog.render(template_id, product)
        operation_id = capture_operation_id(
            source_thread_id,
            source_turn_id,
            product,
            snapshot,
        )
        existing = self.store.get_capture(operation_id)
        if existing is None:
            record = CaptureRecord.started(
                operation_id,
                SourceCheckpoint(source_thread_id, source_turn_id),
                product,
                snapshot,
            )
            self.store.put_capture(record)
            return CapturePlan(
                record,
                snapshot.inventory_prompt,
                snapshot.extraction_prompt,
                False,
            )
        return self._replayed_plan(existing)

    def resume(self, operation_id: str) -> CapturePlan:
        record = self._required_v2_capture(operation_id)
        self._verified_replay_record(record)
        return CapturePlan(
            record,
            record.template.inventory_prompt,
            record.template.extraction_prompt,
            True,
        )

    def attach_fork(
        self,
        operation_id: str,
        fork_thread_id: str,
    ) -> CaptureRecord:
        record = self._required_v2_capture(operation_id)
        if record.fork_thread_id is not None:
            if record.fork_thread_id == fork_thread_id:
                return self._verified_replay_record(record)
            raise CaptureForkConflict(
                f"Capture {operation_id} is already attached to a different fork"
            )
        if record.status != "prepared":
            raise CaptureStateError(
                f"Capture {operation_id} cannot attach a fork from state "
                f"{record.status!r}"
            )
        attached = replace(
            record,
            status="fork_attached",
            fork_thread_id=fork_thread_id,
            updated_at=_now(),
        )
        self.store.put_capture(attached)
        return attached

    def attach_stage_turn(
        self,
        operation_id: str,
        stage: StageName,
        turn_id: str,
    ) -> CaptureRecord:
        record = self._required_v2_capture(operation_id)
        if stage not in ("inventory", "extraction"):
            raise CaptureStateError("Capture stage is invalid")
        return self._attach_stage_turn(record, stage, turn_id)

    def complete_inventory(
        self,
        operation_id: str,
        output: object,
        *,
        raw_output_sha256: str | None = None,
    ) -> CaptureRecord:
        return self._complete_inventory(
            self._required_v2_capture(operation_id),
            output,
            raw_output_sha256,
        )

    def complete_extraction(
        self,
        operation_id: str,
        output: object,
        *,
        raw_output_sha256: str | None = None,
    ) -> CandidateSet:
        return self._complete_extraction(
            self._required_v2_capture(operation_id),
            output,
            raw_output_sha256,
        )

    def record_invalid_json(
        self,
        operation_id: str,
        stage: StageName,
        output_sha256: str,
    ) -> CaptureRecord:
        message = stage_failure_message(stage, "invalid_json")
        if message is None:
            raise CaptureStateError("Capture stage is invalid")
        return self._fail(
            operation_id,
            stage,
            "invalid_json",
            message,
            output_sha256,
        )

    def record_stage_failure(
        self,
        operation_id: str,
        stage: StageName,
        code: Literal[
            "model_refusal",
            "model_timeout",
            "native_unavailable",
            "model_contract_violation",
        ],
        output_sha256: str | None = None,
    ) -> CaptureRecord:
        if code not in _EXPLICIT_FAILURE_CODES:
            raise CaptureStateError("Capture failure code is invalid")
        message = stage_failure_message(stage, code)
        if message is None:
            raise CaptureStateError("Capture stage is invalid")
        return self._fail(
            operation_id,
            stage,
            code,
            message,
            output_sha256,
        )

    def get(self, operation_id: str) -> CaptureRecord | LegacyCaptureRecord:
        return self._required_capture(operation_id)

    def get_inventory(self, operation_id: str) -> InventoryResult | None:
        record = self._required_v2_capture(operation_id)
        return self._verified_inventory(record)

    def get_candidates(self, operation_id: str) -> tuple[Candidate, ...]:
        record = self._required_v2_capture(operation_id)
        return self._verified_candidates(record)

    def _replayed_plan(
        self, record: CaptureRecord | LegacyCaptureRecord
    ) -> CapturePlan:
        if isinstance(record, LegacyCaptureRecord):
            raise CaptureStateError("Legacy Capture records are read-only")
        if record.status == "prepared":
            raise CaptureForkAmbiguous(record.operation_id)
        self._verified_replay_record(record)
        return CapturePlan(
            record,
            record.template.inventory_prompt,
            record.template.extraction_prompt,
            True,
        )

    def _attach_stage_turn(
        self,
        record: CaptureRecord,
        stage: StageName,
        turn_id: str,
    ) -> CaptureRecord:
        existing_turn_id = (
            record.inventory_turn_id
            if stage == "inventory"
            else record.extraction_turn_id
        )
        if existing_turn_id is not None:
            if existing_turn_id == turn_id:
                return self._verified_replay_record(record)
            raise CaptureTurnConflict(
                f"Capture {record.operation_id} stage {stage} is already attached "
                "to a different Turn"
            )

        required_status = (
            "fork_attached" if stage == "inventory" else "inventory_completed"
        )
        if record.status != required_status:
            raise CaptureStateError(
                f"Capture {record.operation_id} cannot attach a {stage} Turn from "
                f"state {record.status!r}"
            )
        if stage == "extraction":
            self._verified_inventory(record)
            updated = replace(
                record,
                status="extraction_running",
                extraction_turn_id=turn_id,
                updated_at=_now(),
            )
        else:
            updated = replace(
                record,
                status="inventory_running",
                inventory_turn_id=turn_id,
                updated_at=_now(),
            )
        self.store.put_capture(updated)
        return updated

    def _complete_inventory(
        self,
        record: CaptureRecord,
        output: object,
        raw_output_sha256: str | None,
    ) -> CaptureRecord:
        if record.inventory_sha256 is not None:
            return self._verified_replay_record(record)
        if record.status != "inventory_running":
            raise CaptureStateError(
                f"Capture {record.operation_id} requires a running inventory Turn"
            )

        output_sha256 = _output_digest_with_fallback(output, raw_output_sha256)
        try:
            inventory = validate_inventory(output)
        except InventoryValidationError as exc:
            self._fail_record(
                record,
                "inventory",
                exc.code,
                exc.message,
                output_sha256,
            )
            raise

        try:
            self.store.put_inventory(record.operation_id, inventory)
        except PrivateStateConflict as exc:
            raise CaptureStateError(
                f"Capture {record.operation_id} has conflicting inventory state"
            ) from exc
        completed = replace(
            record,
            status="inventory_completed",
            inventory_sha256=output_sha256,
            updated_at=_now(),
        )
        self.store.put_capture(completed)
        return completed

    def _complete_extraction(
        self,
        record: CaptureRecord,
        output: object,
        raw_output_sha256: str | None,
    ) -> CandidateSet:
        self._verified_replay_record(record)
        if record.status == "completed":
            assert record.extraction_sha256 is not None
            return CandidateSet(
                operation_id=record.operation_id,
                status="completed",
                candidate_ids=record.candidate_ids,
                extraction_sha256=record.extraction_sha256,
            )
        if record.status != "extraction_running":
            raise CaptureStateError(
                f"Capture {record.operation_id} requires a running extraction Turn"
            )

        output_sha256 = _output_digest_with_fallback(output, raw_output_sha256)
        try:
            candidates = self._validated_candidates(record, output)
        except ExtractionValidationError as exc:
            self._fail_record(
                record,
                "extraction",
                exc.code,
                exc.message,
                output_sha256,
            )
            raise

        candidate_ids = tuple(candidate.candidate_id for candidate in candidates)
        assert record.extraction_turn_id is not None
        manifest = ExtractionManifest(
            manifest_version=1,
            operation_id=record.operation_id,
            extraction_turn_id=record.extraction_turn_id,
            extraction_sha256=output_sha256,
            candidate_ids=candidate_ids,
        )
        try:
            self.store.put_extraction_manifest(manifest)
            for candidate in candidates:
                self.store.put_candidate(candidate)
        except PrivateStateConflict as exc:
            raise CaptureStateError(
                f"Capture {record.operation_id} has conflicting extraction state"
            ) from exc
        self._verified_extraction_artifacts(record, manifest)

        completed = replace(
            record,
            status="completed",
            extraction_sha256=output_sha256,
            candidate_ids=candidate_ids,
            updated_at=_now(),
        )
        self.store.put_capture(completed)
        return CandidateSet(
            operation_id=record.operation_id,
            status="completed",
            candidate_ids=candidate_ids,
            extraction_sha256=output_sha256,
        )

    def _fail(
        self,
        operation_id: str,
        stage: StageName,
        code: str,
        message: str,
        output_sha256: str | None,
    ) -> CaptureRecord:
        if stage not in ("inventory", "extraction"):
            raise CaptureStateError("Capture stage is invalid")
        if output_sha256 is not None and _SHA256.fullmatch(output_sha256) is None:
            raise CaptureStateError("Capture output digest is invalid")
        record = self._required_v2_capture(operation_id)
        requested_failure = StageFailure(stage, code, message, output_sha256)
        if record.status == "failed":
            if record.failure == requested_failure:
                return record
            raise CaptureStateError(
                f"Capture {operation_id} already has a different terminal failure"
            )

        if stage == "inventory":
            eligible = record.status in ("fork_attached", "inventory_running")
            running = (
                record.status == "inventory_running"
                and record.inventory_turn_id is not None
            )
        else:
            eligible = record.status in (
                "inventory_completed",
                "extraction_running",
            )
            running = (
                record.status == "extraction_running"
                and record.extraction_turn_id is not None
            )
        if not eligible or (code != "native_unavailable" and not running):
            raise CaptureStateError(
                f"Capture {operation_id} cannot fail stage {stage} from state "
                f"{record.status!r}"
            )
        return self._fail_record(
            record,
            stage,
            code,
            message,
            output_sha256,
        )

    def _fail_record(
        self,
        record: CaptureRecord,
        stage: StageName,
        code: str,
        message: str,
        output_sha256: str | None,
    ) -> CaptureRecord:
        failed = replace(
            record,
            status="failed",
            failure=StageFailure(stage, code, message, output_sha256),
            updated_at=_now(),
        )
        self.store.put_capture(failed)
        return failed

    def _required_capture(
        self, operation_id: str
    ) -> CaptureRecord | LegacyCaptureRecord:
        record = self.store.get_capture(operation_id)
        if record is None:
            raise CaptureNotFound(f"Capture {operation_id!r} does not exist")
        return record

    def _required_v2_capture(self, operation_id: str) -> CaptureRecord:
        record = self._required_capture(operation_id)
        if isinstance(record, LegacyCaptureRecord):
            raise CaptureStateError("Legacy Capture records are read-only")
        return record

    def _verified_inventory(self, record: CaptureRecord) -> InventoryResult | None:
        if record.inventory_sha256 is None:
            return None
        inventory = self.store.get_inventory(record.operation_id)
        if inventory is None:
            raise CaptureStateError(
                f"Capture {record.operation_id} inventory is missing from private state"
            )
        digest = _output_digest(inventory.to_dict())
        if digest != record.inventory_sha256:
            raise CaptureStateError(
                f"Capture {record.operation_id} inventory digest does not match"
            )
        return inventory

    def _verified_replay_record(self, record: CaptureRecord) -> CaptureRecord:
        if record.inventory_sha256 is not None:
            self._verified_inventory(record)
        if record.status == "completed":
            self._verified_candidates(record)
        return record

    def _verified_candidates(self, record: CaptureRecord) -> tuple[Candidate, ...]:
        if record.status != "completed":
            return ()
        if (
            record.extraction_turn_id is None
            or record.extraction_sha256 is None
        ):
            raise PrivateStateCorrupt("captures", record.operation_id)
        manifest = ExtractionManifest(
            manifest_version=1,
            operation_id=record.operation_id,
            extraction_turn_id=record.extraction_turn_id,
            extraction_sha256=record.extraction_sha256,
            candidate_ids=record.candidate_ids,
        )
        return self._verified_extraction_artifacts(record, manifest)

    def _verified_extraction_artifacts(
        self,
        record: CaptureRecord,
        expected_manifest: ExtractionManifest,
    ) -> tuple[Candidate, ...]:
        manifest = self.store.get_extraction_manifest(record.operation_id)
        if manifest is None or manifest != expected_manifest:
            raise PrivateStateCorrupt("extraction_manifests", record.operation_id)
        expected_ids = manifest.candidate_ids
        if self.store.candidate_ids_for_capture(record.operation_id) != expected_ids:
            raise PrivateStateCorrupt("candidates", record.operation_id)

        candidates: list[Candidate] = []
        for ordinal, candidate_id in enumerate(expected_ids, start=1):
            candidate = self.store.get_candidate(candidate_id)
            if candidate is None:
                raise PrivateStateCorrupt("candidates", candidate_id)
            if (
                candidate.capture_id != record.operation_id
                or candidate.ordinal != ordinal
                or candidate.source != record.source
                or candidate.content.product != record.product
            ):
                raise PrivateStateCorrupt("candidates", candidate_id)
            candidates.append(candidate)

        extraction = {
            "candidates": [
                _persisted_extraction_candidate(candidate)
                for candidate in candidates
            ]
        }
        if _output_digest(extraction) != manifest.extraction_sha256:
            raise PrivateStateCorrupt("extraction_manifests", record.operation_id)
        return tuple(candidates)

    def _validated_candidates(
        self,
        record: CaptureRecord,
        extraction: object,
    ) -> tuple[Candidate, ...]:
        if not isinstance(extraction, Mapping):
            raise _invalid_extraction()
        _require_exact_fields(extraction, _RESULT_FIELDS)
        raw_candidates = extraction["candidates"]
        if not isinstance(raw_candidates, list):
            raise _invalid_extraction()
        if len(raw_candidates) > _MAX_CANDIDATES:
            raise ExtractionValidationError(
                "candidate_limit_exceeded",
                "Extraction contains more than 20 Candidates",
            )

        validated: list[Candidate] = []
        for ordinal, raw_candidate in enumerate(raw_candidates, start=1):
            if not isinstance(raw_candidate, Mapping):
                raise _invalid_extraction()
            _require_exact_fields(raw_candidate, _CANDIDATE_FIELDS)
            scope = raw_candidate["scope"]
            if not isinstance(scope, Mapping):
                raise _invalid_extraction()
            _require_exact_fields(scope, _SCOPE_FIELDS)

            product = _require_nonempty_string(raw_candidate["product"])
            if product != record.product:
                raise _invalid_extraction()
            claim = _require_nonempty_string(raw_candidate["claim"])
            future_action = _require_nonempty_string(raw_candidate["future_action"])
            scope_summary = _require_nonempty_string(scope["summary"])
            repositories = _string_list(scope["repositories"])
            paths = _string_list(scope["paths"])
            invalidation_conditions = _string_list(
                raw_candidate["invalidation_conditions"]
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
            try:
                encoded_size = len(canonical_json_bytes(encoded_candidate))
            except (TypeError, ValueError):
                raise _invalid_extraction() from None
            if encoded_size > _MAX_CANDIDATE_BYTES:
                raise ExtractionValidationError(
                    "candidate_item_too_large",
                    "A Candidate exceeds 16 KiB",
                )

            candidate_id = capture_candidate_id(record.operation_id, ordinal)
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
