"""Bounded values passed through the host-side recall gate."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from zdecision.jsonio import canonical_json_bytes


RecallSessionState = Literal[
    "activating", "active", "blocked", "bypassed", "dormant", "closed"
]
GateDisposition = Literal[
    "reuse", "retrieve", "clarify_product", "refresh_required", "blocked"
]

_INTENT_FIELDS = frozenset(
    (
        "target_decision_space_ids",
        "explicit_multi_space",
        "feature_goal",
        "domain_objects",
        "repository_relative_paths",
        "constraints",
        "exclusions",
    )
)
_MAX_FEATURE_GOAL_CHARACTERS = 2_000
_MAX_LIST_MEMBERS = 32
_MAX_MEMBER_CHARACTERS = 512
_MAX_INTENT_BYTES = 10 * 1024
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != _INTENT_FIELDS:
        raise ValueError("RecallIntent fields are invalid")
    return value


def _member(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_MEMBER_CHARACTERS:
        raise ValueError(f"{field_name} is invalid")
    return normalized


def _members(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_LIST_MEMBERS:
        raise ValueError(f"{field_name} is invalid")
    return tuple(_member(item, field_name) for item in value)


def _relative_path(value: object) -> str:
    normalized = _member(value, "repository_relative_paths")
    if "\\" in normalized or _WINDOWS_DRIVE.match(normalized):
        raise ValueError("repository_relative_paths is invalid")
    path = PurePosixPath(normalized)
    if path.is_absolute() or path == PurePosixPath(".") or ".." in path.parts:
        raise ValueError("repository_relative_paths is invalid")
    return path.as_posix()


def _relative_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_LIST_MEMBERS:
        raise ValueError("repository_relative_paths is invalid")
    return tuple(_relative_path(item) for item in value)


@dataclass(frozen=True)
class RecallIntent:
    target_decision_space_ids: tuple[str, ...]
    explicit_multi_space: bool
    feature_goal: str
    domain_objects: tuple[str, ...]
    repository_relative_paths: tuple[str, ...]
    constraints: tuple[str, ...]
    exclusions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.explicit_multi_space, bool):
            raise ValueError("explicit_multi_space is invalid")
        if (
            not isinstance(self.feature_goal, str)
            or not self.feature_goal.strip()
            or len(self.feature_goal) > _MAX_FEATURE_GOAL_CHARACTERS
        ):
            raise ValueError("feature_goal is invalid")
        for field_name in (
            "target_decision_space_ids",
            "domain_objects",
            "constraints",
            "exclusions",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, tuple) or _members(value, field_name) != value:
                raise ValueError(f"{field_name} is invalid")
        if (
            not isinstance(self.repository_relative_paths, tuple)
            or _relative_paths(self.repository_relative_paths)
            != self.repository_relative_paths
        ):
            raise ValueError("repository_relative_paths is invalid")
        if len(set(self.target_decision_space_ids)) != len(self.target_decision_space_ids):
            raise ValueError("target_decision_space_ids contains duplicates")
        if self.explicit_multi_space:
            if not 1 <= len(self.target_decision_space_ids) <= 8:
                raise ValueError("target_decision_space_ids is invalid")
        elif len(self.target_decision_space_ids) != 1:
            raise ValueError("target_decision_space_ids is invalid")
        if len(canonical_json_bytes(self.to_dict())) > _MAX_INTENT_BYTES:
            raise ValueError("RecallIntent is too large")

    @classmethod
    def from_dict(cls, value: object) -> "RecallIntent":
        item = _mapping(value)
        return cls(
            target_decision_space_ids=_members(
                item["target_decision_space_ids"], "target_decision_space_ids"
            ),
            explicit_multi_space=item["explicit_multi_space"],
            feature_goal=item["feature_goal"],
            domain_objects=_members(item["domain_objects"], "domain_objects"),
            repository_relative_paths=_relative_paths(
                item["repository_relative_paths"]
            ),
            constraints=_members(item["constraints"], "constraints"),
            exclusions=_members(item["exclusions"], "exclusions"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "target_decision_space_ids": list(self.target_decision_space_ids),
            "explicit_multi_space": self.explicit_multi_space,
            "feature_goal": self.feature_goal,
            "domain_objects": list(self.domain_objects),
            "repository_relative_paths": list(self.repository_relative_paths),
            "constraints": list(self.constraints),
            "exclusions": list(self.exclusions),
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_dict())).hexdigest()


@dataclass(frozen=True)
class HostProbeEnvelope:
    probe_id: str
    marker: Literal["host_gate_fixture_not_formal"]
    instruction: str

    def __post_init__(self) -> None:
        if self.marker != "host_gate_fixture_not_formal":
            raise ValueError("marker is invalid")


@dataclass(frozen=True)
class TurnGateResult:
    disposition: GateDisposition
    intent_digest: str
    context_epoch: int
    intent_epoch: int
    probe: HostProbeEnvelope | None
