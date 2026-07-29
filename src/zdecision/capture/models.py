"""Typed records persisted by the Capture slice."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Mapping

from zdecision.capture.templates import TemplateSnapshot
from zdecision.ids import capture_candidate_id, capture_operation_id


def _require_keys(
    value: Mapping[str, object],
    expected: frozenset[str],
    record_name: str,
) -> None:
    actual = frozenset(value)
    if actual != expected:
        unknown = sorted(actual - expected)
        missing = sorted(expected - actual)
        raise ValueError(
            f"Invalid {record_name} fields: unknown={unknown}, missing={missing}"
        )


def _strings(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return tuple(value)


@dataclass(frozen=True)
class SourceCheckpoint:
    thread_id: str
    turn_id: str

    def to_dict(self) -> dict[str, object]:
        return {"thread_id": self.thread_id, "turn_id": self.turn_id}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SourceCheckpoint:
        _require_keys(
            value,
            frozenset(("thread_id", "turn_id")),
            "SourceCheckpoint",
        )
        thread_id = value["thread_id"]
        turn_id = value["turn_id"]
        if (
            not isinstance(thread_id, str)
            or not thread_id.strip()
            or not isinstance(turn_id, str)
            or not turn_id.strip()
        ):
            raise ValueError("SourceCheckpoint ids must be non-empty strings")
        return cls(thread_id=thread_id, turn_id=turn_id)


@dataclass(frozen=True)
class CandidateContent:
    product: str
    claim: str
    future_action: str
    scope_summary: str
    repositories: tuple[str, ...]
    paths: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "product": self.product,
            "claim": self.claim,
            "future_action": self.future_action,
            "scope_summary": self.scope_summary,
            "repositories": list(self.repositories),
            "paths": list(self.paths),
            "invalidation_conditions": list(self.invalidation_conditions),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CandidateContent:
        _require_keys(
            value,
            frozenset(
                (
                    "product",
                    "claim",
                    "future_action",
                    "scope_summary",
                    "repositories",
                    "paths",
                    "invalidation_conditions",
                )
            ),
            "CandidateContent",
        )
        scalar_fields = (
            "product",
            "claim",
            "future_action",
            "scope_summary",
        )
        if any(
            not isinstance(value[field], str) or not value[field].strip()
            for field in scalar_fields
        ):
            raise ValueError(
                "CandidateContent scalar fields must be non-empty strings"
            )
        return cls(
            product=value["product"],
            claim=value["claim"],
            future_action=value["future_action"],
            scope_summary=value["scope_summary"],
            repositories=_strings(value["repositories"], "repositories"),
            paths=_strings(value["paths"], "paths"),
            invalidation_conditions=_strings(
                value["invalidation_conditions"],
                "invalidation_conditions",
            ),
        )


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    capture_id: str
    ordinal: int
    content: CandidateContent
    source: SourceCheckpoint

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "capture_id": self.capture_id,
            "ordinal": self.ordinal,
            "content": self.content.to_dict(),
            "source": self.source.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Candidate:
        _require_keys(
            value,
            frozenset(("candidate_id", "capture_id", "ordinal", "content", "source")),
            "Candidate",
        )
        candidate_id = value["candidate_id"]
        capture_id = value["capture_id"]
        ordinal = value["ordinal"]
        content = value["content"]
        source = value["source"]
        if (
            not isinstance(candidate_id, str)
            or not candidate_id.strip()
            or not isinstance(capture_id, str)
            or not capture_id.strip()
        ):
            raise ValueError("Candidate ids must be non-empty strings")
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal < 1
        ):
            raise ValueError("Candidate ordinal must be a positive integer")
        if not isinstance(content, Mapping) or not isinstance(source, Mapping):
            raise ValueError("Candidate content and source must be objects")
        return cls(
            candidate_id=candidate_id,
            capture_id=capture_id,
            ordinal=ordinal,
            content=CandidateContent.from_dict(content),
            source=SourceCheckpoint.from_dict(source),
        )


CaptureStatus = Literal[
    "prepared",
    "fork_attached",
    "inventory_running",
    "inventory_completed",
    "extraction_running",
    "completed",
    "failed",
]
StageName = Literal["inventory", "extraction"]

_SHARED_FAILURE_MESSAGES = {
    "invalid_json": "Stage output was not valid JSON",
    "model_refusal": "Stage model refused the request",
    "model_timeout": "Stage model did not complete before the timeout",
    "native_unavailable": "Required native task capability was unavailable",
    "model_contract_violation": "Stage model violated the output contract",
}
_STAGE_FAILURE_MESSAGES = {
    "inventory": {
        **_SHARED_FAILURE_MESSAGES,
        "invalid_inventory": "Inventory output does not match the required schema",
        "inventory_signal_limit_exceeded": (
            "Inventory contains more than 100 signals"
        ),
        "inventory_output_too_large": "Inventory output exceeds 256 KiB",
    },
    "extraction": {
        **_SHARED_FAILURE_MESSAGES,
        "invalid_extraction": "Extraction output does not match the required schema",
        "candidate_limit_exceeded": (
            "Extraction contains more than 20 Candidates"
        ),
        "candidate_item_too_large": "A Candidate exceeds 16 KiB",
    },
}


def stage_failure_message(stage: StageName, code: str) -> str | None:
    """Return the one sanitized persisted message for a stage/code pair."""

    stage_messages = _STAGE_FAILURE_MESSAGES.get(stage)
    return None if stage_messages is None else stage_messages.get(code)

_CAPTURE_FIELDS = frozenset(
    (
        "record_version",
        "operation_id",
        "source",
        "product",
        "template",
        "status",
        "fork_thread_id",
        "inventory_turn_id",
        "extraction_turn_id",
        "inventory_sha256",
        "extraction_sha256",
        "failure",
        "candidate_ids",
        "created_at",
        "updated_at",
    )
)
LEGACY_CAPTURE_FIELDS = frozenset(
    (
        "operation_id",
        "source",
        "product",
        "status",
        "fork_thread_id",
        "candidate_ids",
        "created_at",
        "updated_at",
    )
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PAYLOAD_FAILURE_CODES = frozenset(
    (
        "invalid_json",
        "invalid_inventory",
        "inventory_signal_limit_exceeded",
        "inventory_output_too_large",
        "invalid_extraction",
        "candidate_limit_exceeded",
        "candidate_item_too_large",
    )
)


def _optional_string(value: object, field_name: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(f"{field_name} must be a non-empty string or null")
    return value


def _optional_digest(value: object, field_name: str) -> str | None:
    if value is not None and (
        not isinstance(value, str) or _SHA256.fullmatch(value) is None
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest or null")
    return value


@dataclass(frozen=True)
class StageFailure:
    stage: StageName
    code: str
    message: str
    output_sha256: str | None

    def __post_init__(self) -> None:
        if self.stage not in ("inventory", "extraction"):
            raise ValueError("StageFailure stage is invalid")
        if not isinstance(self.code, str) or not isinstance(self.message, str):
            raise ValueError("StageFailure code and message must be strings")
        expected_message = stage_failure_message(self.stage, self.code)
        if expected_message is None or self.message != expected_message:
            raise ValueError("StageFailure code or message is invalid")
        digest = _optional_digest(
            self.output_sha256, "StageFailure output_sha256"
        )
        if self.code in _PAYLOAD_FAILURE_CODES and digest is None:
            raise ValueError(
                "StageFailure payload validation requires an output digest"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "code": self.code,
            "message": self.message,
            "output_sha256": self.output_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> StageFailure:
        _require_keys(
            value,
            frozenset(("stage", "code", "message", "output_sha256")),
            "StageFailure",
        )
        stage = value["stage"]
        code = value["code"]
        message = value["message"]
        return cls(
            stage=stage,
            code=code,
            message=message,
            output_sha256=value["output_sha256"],
        )


@dataclass(frozen=True)
class ExtractionManifest:
    """Immutable ownership claim for one complete Stage 2 payload."""

    manifest_version: Literal[1]
    operation_id: str
    extraction_turn_id: str
    extraction_sha256: str
    candidate_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.manifest_version != 1 or isinstance(self.manifest_version, bool):
            raise ValueError("ExtractionManifest manifest_version must be 1")
        if (
            not isinstance(self.operation_id, str)
            or not self.operation_id
            or not isinstance(self.extraction_turn_id, str)
            or not self.extraction_turn_id
        ):
            raise ValueError("ExtractionManifest ids must be non-empty strings")
        if _optional_digest(
            self.extraction_sha256, "ExtractionManifest extraction_sha256"
        ) is None:
            raise ValueError("ExtractionManifest requires an extraction digest")
        if (
            not isinstance(self.candidate_ids, tuple)
            or len(self.candidate_ids) > 20
            or any(
                not isinstance(candidate_id, str) or not candidate_id
                for candidate_id in self.candidate_ids
            )
        ):
            raise ValueError("ExtractionManifest Candidate references are invalid")
        expected_ids = tuple(
            capture_candidate_id(self.operation_id, ordinal)
            for ordinal in range(1, len(self.candidate_ids) + 1)
        )
        if self.candidate_ids != expected_ids:
            raise ValueError("ExtractionManifest Candidate references are invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest_version": self.manifest_version,
            "operation_id": self.operation_id,
            "extraction_turn_id": self.extraction_turn_id,
            "extraction_sha256": self.extraction_sha256,
            "candidate_ids": list(self.candidate_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ExtractionManifest:
        _require_keys(
            value,
            frozenset(
                (
                    "manifest_version",
                    "operation_id",
                    "extraction_turn_id",
                    "extraction_sha256",
                    "candidate_ids",
                )
            ),
            "ExtractionManifest",
        )
        return cls(
            manifest_version=value["manifest_version"],
            operation_id=value["operation_id"],
            extraction_turn_id=value["extraction_turn_id"],
            extraction_sha256=value["extraction_sha256"],
            candidate_ids=_strings(value["candidate_ids"], "candidate_ids"),
        )


@dataclass(frozen=True)
class CaptureRecord:
    record_version: Literal[2]
    operation_id: str
    source: SourceCheckpoint
    product: str
    template: TemplateSnapshot
    status: CaptureStatus
    fork_thread_id: str | None
    inventory_turn_id: str | None
    extraction_turn_id: str | None
    inventory_sha256: str | None
    extraction_sha256: str | None
    failure: StageFailure | None
    candidate_ids: tuple[str, ...]
    created_at: str
    updated_at: str

    @classmethod
    def started(
        cls,
        operation_id: str,
        source: SourceCheckpoint,
        product: str,
        template: TemplateSnapshot,
    ) -> CaptureRecord:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return cls(
            record_version=2,
            operation_id=operation_id,
            source=source,
            product=product,
            template=template,
            status="prepared",
            fork_thread_id=None,
            inventory_turn_id=None,
            extraction_turn_id=None,
            inventory_sha256=None,
            extraction_sha256=None,
            failure=None,
            candidate_ids=(),
            created_at=now,
            updated_at=now,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "record_version": self.record_version,
            "operation_id": self.operation_id,
            "source": self.source.to_dict(),
            "product": self.product,
            "template": self.template.to_dict(),
            "status": self.status,
            "fork_thread_id": self.fork_thread_id,
            "inventory_turn_id": self.inventory_turn_id,
            "extraction_turn_id": self.extraction_turn_id,
            "inventory_sha256": self.inventory_sha256,
            "extraction_sha256": self.extraction_sha256,
            "failure": None if self.failure is None else self.failure.to_dict(),
            "candidate_ids": list(self.candidate_ids),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def public_dict(self) -> dict[str, object]:
        return {
            "record_version": self.record_version,
            "operation_id": self.operation_id,
            "source": self.source.to_dict(),
            "product": self.product,
            "status": self.status,
            "fork_thread_id": self.fork_thread_id,
            "inventory_turn_id": self.inventory_turn_id,
            "extraction_turn_id": self.extraction_turn_id,
            "inventory_sha256": self.inventory_sha256,
            "extraction_sha256": self.extraction_sha256,
            "failure": None if self.failure is None else self.failure.to_dict(),
            "candidate_ids": list(self.candidate_ids),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CaptureRecord:
        _require_keys(value, _CAPTURE_FIELDS, "CaptureRecord")
        if value["record_version"] != 2 or isinstance(value["record_version"], bool):
            raise ValueError("CaptureRecord record_version must be 2")
        source = value["source"]
        template = value["template"]
        failure = value["failure"]
        if not isinstance(source, Mapping) or not isinstance(template, Mapping):
            raise ValueError("CaptureRecord source and template must be objects")
        if failure is not None and not isinstance(failure, Mapping):
            raise ValueError("CaptureRecord failure must be an object or null")
        status = value["status"]
        if status not in (
            "prepared",
            "fork_attached",
            "inventory_running",
            "inventory_completed",
            "extraction_running",
            "completed",
            "failed",
        ):
            raise ValueError("CaptureRecord status is invalid")
        string_fields = ("operation_id", "product", "created_at", "updated_at")
        if any(
            not isinstance(value[field], str) or not value[field]
            for field in string_fields
        ):
            raise ValueError("CaptureRecord scalar fields must be non-empty strings")
        record = cls(
            record_version=2,
            operation_id=value["operation_id"],
            source=SourceCheckpoint.from_dict(source),
            product=value["product"],
            template=TemplateSnapshot.from_dict(template),
            status=status,
            fork_thread_id=_optional_string(
                value["fork_thread_id"], "fork_thread_id"
            ),
            inventory_turn_id=_optional_string(
                value["inventory_turn_id"], "inventory_turn_id"
            ),
            extraction_turn_id=_optional_string(
                value["extraction_turn_id"], "extraction_turn_id"
            ),
            inventory_sha256=_optional_digest(
                value["inventory_sha256"], "inventory_sha256"
            ),
            extraction_sha256=_optional_digest(
                value["extraction_sha256"], "extraction_sha256"
            ),
            failure=None if failure is None else StageFailure.from_dict(failure),
            candidate_ids=_strings(value["candidate_ids"], "candidate_ids"),
            created_at=value["created_at"],
            updated_at=value["updated_at"],
        )
        expected_operation_id = capture_operation_id(
            record.source.thread_id,
            record.source.turn_id,
            record.product,
            record.template,
        )
        if record.operation_id != expected_operation_id:
            raise ValueError("CaptureRecord operation identity mismatch")
        record._validate_state_shape()
        return record

    def _validate_state_shape(self) -> None:
        fork = self.fork_thread_id is not None
        inventory_turn = self.inventory_turn_id is not None
        extraction_turn = self.extraction_turn_id is not None
        inventory_digest = self.inventory_sha256 is not None
        extraction_digest = self.extraction_sha256 is not None
        failed = self.failure is not None
        candidates = bool(self.candidate_ids)

        if self.status == "completed":
            if len(self.candidate_ids) > 20:
                raise ValueError("CaptureRecord has too many Candidate references")
            expected_candidate_ids = tuple(
                capture_candidate_id(self.operation_id, ordinal)
                for ordinal in range(1, len(self.candidate_ids) + 1)
            )
            if self.candidate_ids != expected_candidate_ids:
                raise ValueError("CaptureRecord Candidate references are invalid")

        valid = False
        if self.status == "prepared":
            valid = not any(
                (
                    fork,
                    inventory_turn,
                    extraction_turn,
                    inventory_digest,
                    extraction_digest,
                    failed,
                    candidates,
                )
            )
        elif self.status == "fork_attached":
            valid = fork and not any(
                (
                    inventory_turn,
                    extraction_turn,
                    inventory_digest,
                    extraction_digest,
                    failed,
                    candidates,
                )
            )
        elif self.status == "inventory_running":
            valid = fork and inventory_turn and not any(
                (extraction_turn, inventory_digest, extraction_digest, failed, candidates)
            )
        elif self.status == "inventory_completed":
            valid = fork and inventory_turn and inventory_digest and not any(
                (extraction_turn, extraction_digest, failed, candidates)
            )
        elif self.status == "extraction_running":
            valid = (
                fork
                and inventory_turn
                and inventory_digest
                and extraction_turn
                and not any((extraction_digest, failed, candidates))
            )
        elif self.status == "completed":
            valid = (
                fork
                and inventory_turn
                and inventory_digest
                and extraction_turn
                and extraction_digest
                and not failed
            )
        elif self.status == "failed" and self.failure is not None:
            if self.failure.stage == "inventory":
                stage_turn_is_legal = (
                    inventory_turn or self.failure.code == "native_unavailable"
                )
                valid = fork and stage_turn_is_legal and not any(
                    (extraction_turn, inventory_digest, extraction_digest, candidates)
                )
            else:
                stage_turn_is_legal = (
                    extraction_turn or self.failure.code == "native_unavailable"
                )
                valid = (
                    fork
                    and inventory_turn
                    and inventory_digest
                    and stage_turn_is_legal
                    and not extraction_digest
                    and not candidates
                )
        if not valid:
            raise ValueError("CaptureRecord fields do not match its status")


@dataclass(frozen=True)
class LegacyCaptureRecord:
    operation_id: str
    source: SourceCheckpoint
    product: str
    status: Literal["prepared", "fork_attached", "completed", "failed"]
    fork_thread_id: str | None
    candidate_ids: tuple[str, ...]
    created_at: str
    updated_at: str
    record_version: Literal[1] = field(default=1, init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "source": self.source.to_dict(),
            "product": self.product,
            "status": self.status,
            "fork_thread_id": self.fork_thread_id,
            "candidate_ids": list(self.candidate_ids),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> LegacyCaptureRecord:
        _require_keys(value, LEGACY_CAPTURE_FIELDS, "LegacyCaptureRecord")
        source = value["source"]
        if not isinstance(source, Mapping):
            raise ValueError("LegacyCaptureRecord source must be an object")
        status = value["status"]
        if status not in ("prepared", "fork_attached", "completed", "failed"):
            raise ValueError("LegacyCaptureRecord status is invalid")
        string_fields = ("operation_id", "product", "created_at", "updated_at")
        if any(not isinstance(value[field], str) for field in string_fields):
            raise ValueError("LegacyCaptureRecord scalar fields must be strings")
        return cls(
            operation_id=value["operation_id"],
            source=SourceCheckpoint.from_dict(source),
            product=value["product"],
            status=status,
            fork_thread_id=_optional_string(
                value["fork_thread_id"], "fork_thread_id"
            ),
            candidate_ids=_strings(value["candidate_ids"], "candidate_ids"),
            created_at=value["created_at"],
            updated_at=value["updated_at"],
        )


@dataclass(frozen=True)
class CandidateSet:
    operation_id: str
    status: Literal["completed"]
    candidate_ids: tuple[str, ...]
    extraction_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "status": self.status,
            "candidate_ids": list(self.candidate_ids),
            "extraction_sha256": self.extraction_sha256,
        }


@dataclass(frozen=True)
class CapturePlan:
    record: CaptureRecord
    inventory_prompt: str
    extraction_prompt: str
    replayed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "record": self.record.to_dict(),
            "inventory_prompt": self.inventory_prompt,
            "extraction_prompt": self.extraction_prompt,
            "replayed": self.replayed,
        }
