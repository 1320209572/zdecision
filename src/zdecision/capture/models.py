"""Typed records persisted by the Capture slice."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Mapping


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
        if not isinstance(thread_id, str) or not isinstance(turn_id, str):
            raise ValueError("SourceCheckpoint ids must be strings")
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
        if any(not isinstance(value[field], str) for field in scalar_fields):
            raise ValueError("CandidateContent scalar fields must be strings")
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
        if not isinstance(candidate_id, str) or not isinstance(capture_id, str):
            raise ValueError("Candidate ids must be strings")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise ValueError("Candidate ordinal must be an integer")
        if not isinstance(content, Mapping) or not isinstance(source, Mapping):
            raise ValueError("Candidate content and source must be objects")
        return cls(
            candidate_id=candidate_id,
            capture_id=capture_id,
            ordinal=ordinal,
            content=CandidateContent.from_dict(content),
            source=SourceCheckpoint.from_dict(source),
        )


CaptureStatus = Literal["prepared", "fork_attached", "completed", "failed"]


@dataclass(frozen=True)
class CaptureRecord:
    operation_id: str
    source: SourceCheckpoint
    product: str
    status: CaptureStatus
    fork_thread_id: str | None
    candidate_ids: tuple[str, ...]
    created_at: str
    updated_at: str

    @classmethod
    def started(
        cls,
        operation_id: str,
        source: SourceCheckpoint,
        product: str,
    ) -> CaptureRecord:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return cls(
            operation_id=operation_id,
            source=source,
            product=product,
            status="prepared",
            fork_thread_id=None,
            candidate_ids=(),
            created_at=now,
            updated_at=now,
        )

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
    def from_dict(cls, value: Mapping[str, object]) -> CaptureRecord:
        _require_keys(
            value,
            frozenset(
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
            ),
            "CaptureRecord",
        )
        source = value["source"]
        if not isinstance(source, Mapping):
            raise ValueError("CaptureRecord source must be an object")
        status = value["status"]
        if status not in ("prepared", "fork_attached", "completed", "failed"):
            raise ValueError("CaptureRecord status is invalid")
        fork_thread_id = value["fork_thread_id"]
        if fork_thread_id is not None and not isinstance(fork_thread_id, str):
            raise ValueError("fork_thread_id must be a string or null")
        string_fields = ("operation_id", "product", "created_at", "updated_at")
        if any(not isinstance(value[field], str) for field in string_fields):
            raise ValueError("CaptureRecord scalar fields must be strings")
        return cls(
            operation_id=value["operation_id"],
            source=SourceCheckpoint.from_dict(source),
            product=value["product"],
            status=status,
            fork_thread_id=fork_thread_id,
            candidate_ids=_strings(value["candidate_ids"], "candidate_ids"),
            created_at=value["created_at"],
            updated_at=value["updated_at"],
        )


@dataclass(frozen=True)
class CandidateSet:
    operation_id: str
    status: Literal["completed"]
    candidate_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "status": self.status,
            "candidate_ids": list(self.candidate_ids),
        }


@dataclass(frozen=True)
class CapturePlan:
    record: CaptureRecord
    extraction_prompt: str
    replayed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "record": self.record.to_dict(),
            "extraction_prompt": self.extraction_prompt,
            "replayed": self.replayed,
        }
