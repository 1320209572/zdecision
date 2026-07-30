"""Strict local facts and model output for Capture eligibility assessment."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from zdecision.jsonio import canonical_json_bytes


ELIGIBILITY_PROMPT_VERSION = "capture-eligibility/v1"
_PHASES = frozenset(
    (
        "exploring",
        "implementing",
        "awaiting_user",
        "validation_failed",
        "milestone_complete",
    )
)
_VALIDATIONS = frozenset(("passed", "failed", "not_applicable", "unknown"))
_WORK_KINDS = frozenset(("code", "product", "design"))
_ASSESSMENT_FIELDS = frozenset(
    (
        "phase",
        "has_durable_decision_signal",
        "validation",
        "unresolved_blockers",
    )
)
_HEAD_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MAX_BLOCKERS = 20
_MAX_BLOCKER_LENGTH = 256


Phase = Literal[
    "exploring",
    "implementing",
    "awaiting_user",
    "validation_failed",
    "milestone_complete",
]
Validation = Literal["passed", "failed", "not_applicable", "unknown"]
WorkKind = Literal["code", "product", "design"]


def _nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _blockers(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_BLOCKERS:
        raise ValueError("unresolved_blockers is invalid")
    blockers: list[str] = []
    for blocker in value:
        if (
            not isinstance(blocker, str)
            or not blocker.strip()
            or len(blocker) > _MAX_BLOCKER_LENGTH
        ):
            raise ValueError("unresolved_blockers is invalid")
        blockers.append(blocker)
    if len(set(blockers)) != len(blockers):
        raise ValueError("unresolved_blockers must be unique")
    return tuple(blockers)


@dataclass(frozen=True)
class BoundaryAssessment:
    phase: Phase
    has_durable_decision_signal: bool
    validation: Validation
    unresolved_blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.phase not in _PHASES:
            raise ValueError("BoundaryAssessment phase is invalid")
        if not isinstance(self.has_durable_decision_signal, bool):
            raise ValueError("has_durable_decision_signal must be a boolean")
        if self.validation not in _VALIDATIONS:
            raise ValueError("BoundaryAssessment validation is invalid")
        if _blockers(self.unresolved_blockers) != self.unresolved_blockers:
            raise ValueError("BoundaryAssessment blockers are invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "has_durable_decision_signal": self.has_durable_decision_signal,
            "validation": self.validation,
            "unresolved_blockers": list(self.unresolved_blockers),
        }


@dataclass(frozen=True)
class SourceBoundaryFacts:
    source_thread_id: str
    source_turn_id: str
    repository_id: str
    head_commit: str | None
    work_kind: WorkKind
    source_turn_completed: bool
    source_turn_assessed: bool
    capture_active: bool
    repository_mapping_valid: bool
    local_runtime_valid: bool
    reported_work_state: Phase | None
    validation: Validation
    unresolved_blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        _nonempty(self.source_thread_id, "source_thread_id")
        _nonempty(self.source_turn_id, "source_turn_id")
        _nonempty(self.repository_id, "repository_id")
        if self.head_commit is not None and _HEAD_COMMIT.fullmatch(
            self.head_commit
        ) is None:
            raise ValueError("head_commit is invalid")
        if self.work_kind not in _WORK_KINDS:
            raise ValueError("work_kind is invalid")
        for field_name in (
            "source_turn_completed",
            "source_turn_assessed",
            "capture_active",
            "repository_mapping_valid",
            "local_runtime_valid",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} must be a boolean")
        if self.reported_work_state is not None and (
            self.reported_work_state not in _PHASES
        ):
            raise ValueError("reported_work_state is invalid")
        if self.validation not in _VALIDATIONS:
            raise ValueError("validation is invalid")
        if _blockers(self.unresolved_blockers) != self.unresolved_blockers:
            raise ValueError("SourceBoundaryFacts blockers are invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_thread_id": self.source_thread_id,
            "source_turn_id": self.source_turn_id,
            "repository_id": self.repository_id,
            "head_commit": self.head_commit,
            "work_kind": self.work_kind,
            "source_turn_completed": self.source_turn_completed,
            "source_turn_assessed": self.source_turn_assessed,
            "capture_active": self.capture_active,
            "repository_mapping_valid": self.repository_mapping_valid,
            "local_runtime_valid": self.local_runtime_valid,
            "reported_work_state": self.reported_work_state,
            "validation": self.validation,
            "unresolved_blockers": list(self.unresolved_blockers),
        }


def eligibility_prompt(boundary: SourceBoundaryFacts) -> str:
    if not isinstance(boundary, SourceBoundaryFacts):
        raise TypeError("boundary must be SourceBoundaryFacts")
    policy_path = (
        Path(__file__).resolve().parent
        / "prompt_contracts"
        / "capture-eligibility-v1.md"
    )
    policy = policy_path.read_text("utf-8").strip()
    facts_json = canonical_json_bytes(boundary.to_dict()).decode("utf-8").rstrip()
    return f"{policy}\n\n<boundary_facts>\n{facts_json}\n</boundary_facts>\n"


def eligibility_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "phase": {"type": "string", "enum": sorted(_PHASES)},
            "has_durable_decision_signal": {"type": "boolean"},
            "validation": {"type": "string", "enum": sorted(_VALIDATIONS)},
            "unresolved_blockers": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": sorted(_ASSESSMENT_FIELDS),
        "additionalProperties": False,
    }


def validate_boundary_assessment(value: object) -> BoundaryAssessment:
    if not isinstance(value, Mapping) or frozenset(value) != _ASSESSMENT_FIELDS:
        raise ValueError("BoundaryAssessment fields are invalid")
    phase = value["phase"]
    validation = value["validation"]
    durable = value["has_durable_decision_signal"]
    if not isinstance(phase, str) or phase not in _PHASES:
        raise ValueError("BoundaryAssessment phase is invalid")
    if not isinstance(validation, str) or validation not in _VALIDATIONS:
        raise ValueError("BoundaryAssessment validation is invalid")
    if not isinstance(durable, bool):
        raise ValueError("has_durable_decision_signal must be a boolean")
    return BoundaryAssessment(
        phase=cast(Phase, phase),
        has_durable_decision_signal=durable,
        validation=cast(Validation, validation),
        unresolved_blockers=_blockers(value["unresolved_blockers"]),
    )


def capture_eligible(
    assessment: BoundaryAssessment, facts: SourceBoundaryFacts
) -> bool:
    if not isinstance(assessment, BoundaryAssessment) or not isinstance(
        facts, SourceBoundaryFacts
    ):
        raise TypeError("capture_eligible requires typed inputs")
    validation_ok = assessment.validation == "passed" or (
        assessment.validation == "not_applicable"
        and facts.work_kind in ("product", "design")
    )
    return (
        assessment.phase == "milestone_complete"
        and assessment.has_durable_decision_signal
        and validation_ok
        and not assessment.unresolved_blockers
        and facts.source_turn_completed
        and not facts.source_turn_assessed
        and not facts.capture_active
        and facts.repository_mapping_valid
        and facts.local_runtime_valid
    )
