"""Strict values returned by the Codex app-server gateway."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from zdecision.jsonio import canonical_json_bytes


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_ID = re.compile(r"^fmp_[0-9a-f]{32}$")


def _nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class FeasibilityModelProfile:
    profile_id: str
    model_id: str
    reasoning_effort: str
    discovery_digest: str
    discovered_at: str

    def __post_init__(self) -> None:
        if _PROFILE_ID.fullmatch(self.profile_id) is None:
            raise ValueError("profile_id is invalid")
        _nonempty(self.model_id, "model_id")
        _nonempty(self.reasoning_effort, "reasoning_effort")
        if _DIGEST.fullmatch(self.discovery_digest) is None:
            raise ValueError("discovery_digest is invalid")
        _nonempty(self.discovered_at, "discovered_at")

    @classmethod
    def create(
        cls,
        *,
        model_id: str,
        reasoning_effort: str,
        discovery_digest: str,
        discovered_at: str,
    ) -> "FeasibilityModelProfile":
        model = _nonempty(model_id, "model_id")
        effort = _nonempty(reasoning_effort, "reasoning_effort")
        if _DIGEST.fullmatch(discovery_digest) is None:
            raise ValueError("discovery_digest is invalid")
        identity = hashlib.sha256(
            canonical_json_bytes(
                {
                    "discovery_digest": discovery_digest,
                    "model_id": model,
                    "reasoning_effort": effort,
                }
            )
        ).hexdigest()[:32]
        return cls(
            profile_id=f"fmp_{identity}",
            model_id=model,
            reasoning_effort=effort,
            discovery_digest=discovery_digest,
            discovered_at=_nonempty(discovered_at, "discovered_at"),
        )


@dataclass(frozen=True)
class SourceBoundary:
    thread_id: str
    turn_id: str
    cwd: str
    status: Literal["completed"]
    model_id: str | None
    reasoning_effort: str | None

    def __post_init__(self) -> None:
        _nonempty(self.thread_id, "thread_id")
        _nonempty(self.turn_id, "turn_id")
        if not isinstance(self.cwd, str) or not Path(self.cwd).is_absolute():
            raise ValueError("cwd must be an absolute path")
        if self.status != "completed":
            raise ValueError("Source boundary must be completed")
        if self.model_id is not None:
            _nonempty(self.model_id, "model_id")
        if self.reasoning_effort is not None:
            _nonempty(self.reasoning_effort, "reasoning_effort")


@dataclass(frozen=True)
class ThreadIdentity:
    thread_id: str
    session_tree_id: str
    forked_from_id: str | None
    cwd: str
    ephemeral: bool

    def __post_init__(self) -> None:
        thread_id = _bounded_identifier(self.thread_id, "thread_id")
        session_tree_id = _bounded_identifier(
            self.session_tree_id, "session_tree_id"
        )
        if self.forked_from_id is None:
            if session_tree_id != thread_id:
                raise ValueError("root session_tree_id must equal thread_id")
        else:
            parent = _bounded_identifier(
                self.forked_from_id, "forked_from_id"
            )
            if parent == thread_id:
                raise ValueError("forked_from_id must not equal thread_id")
            if session_tree_id == thread_id:
                raise ValueError("child session_tree_id must differ from thread_id")
        if not isinstance(self.cwd, str) or not Path(self.cwd).is_absolute():
            raise ValueError("cwd must be an absolute path")
        if not isinstance(self.ephemeral, bool):
            raise ValueError("ephemeral must be a boolean")


@dataclass(frozen=True)
class SelectedSkill:
    selection_type: Literal["skill", "mention"]
    name: str
    path: str

    def __post_init__(self) -> None:
        if self.selection_type not in ("skill", "mention"):
            raise ValueError("selection_type is invalid")
        _bounded_identifier(self.name, "name")
        if (
            not isinstance(self.path, str)
            or len(self.path) > 4096
            or "\x00" in self.path
            or not Path(self.path).is_absolute()
        ):
            raise ValueError("path must be a bounded absolute path")


@dataclass(frozen=True)
class TurnItemEvidence:
    item_type: Literal[
        "hookPrompt",
        "mcpToolCall",
        "agentMessage",
        "commandExecution",
        "fileChange",
        "contextCompaction",
    ]
    item_id: str
    tool_name: str | None = None
    operation_id: str | None = None
    receipt_id: str | None = None
    probe_id: str | None = None

    def __post_init__(self) -> None:
        if self.item_type not in (
            "hookPrompt",
            "mcpToolCall",
            "agentMessage",
            "commandExecution",
            "fileChange",
            "contextCompaction",
        ):
            raise ValueError("item_type is invalid")
        _bounded_identifier(self.item_id, "item_id")
        if self.tool_name is not None:
            _bounded_identifier(self.tool_name, "tool_name")
            if self.item_type != "mcpToolCall":
                raise ValueError("tool_name is only valid for mcpToolCall")
        if self.operation_id is not None:
            _bounded_identifier(self.operation_id, "operation_id")
            if self.item_type != "mcpToolCall":
                raise ValueError("operation_id is only valid for mcpToolCall")
        for field_name, value in (
            ("receipt_id", self.receipt_id),
            ("probe_id", self.probe_id),
        ):
            if value is not None:
                _bounded_identifier(value, field_name)
        if self.receipt_id is not None and self.item_type not in (
            "hookPrompt",
            "mcpToolCall",
        ):
            raise ValueError("receipt_id has an invalid item type")
        if self.probe_id is not None and self.item_type != "mcpToolCall":
            raise ValueError("probe_id is only valid for mcpToolCall")


@dataclass(frozen=True)
class ActiveTurnEvidence:
    thread: ThreadIdentity
    turn_id: str
    selected_skills: tuple[SelectedSkill, ...]
    ordered_items: tuple[TurnItemEvidence, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.thread, ThreadIdentity):
            raise TypeError("thread must be a ThreadIdentity")
        _bounded_identifier(self.turn_id, "turn_id")
        if not isinstance(self.selected_skills, tuple) or len(
            self.selected_skills
        ) > 256:
            raise ValueError("selected_skills is invalid")
        if not all(isinstance(value, SelectedSkill) for value in self.selected_skills):
            raise TypeError("selected_skills contains an invalid value")
        if not isinstance(self.ordered_items, tuple) or len(
            self.ordered_items
        ) > 4096:
            raise ValueError("ordered_items is invalid")
        if not all(
            isinstance(value, TurnItemEvidence) for value in self.ordered_items
        ):
            raise TypeError("ordered_items contains an invalid value")


@dataclass(frozen=True)
class AppServerTurnReceipt:
    thread_id: str
    turn_id: str
    status: Literal["completed"]
    structured_output: Mapping[str, object]
    output_sha256: str
    model_profile_id: str

    def __post_init__(self) -> None:
        _nonempty(self.thread_id, "thread_id")
        _nonempty(self.turn_id, "turn_id")
        if self.status != "completed":
            raise ValueError("App-server Turn receipt must be completed")
        if not isinstance(self.structured_output, Mapping):
            raise ValueError("structured_output must be an object")
        if _DIGEST.fullmatch(self.output_sha256) is None:
            raise ValueError("output_sha256 is invalid")
        if _PROFILE_ID.fullmatch(self.model_profile_id) is None:
            raise ValueError("model_profile_id is invalid")

    @classmethod
    def create(
        cls,
        *,
        thread_id: str,
        turn_id: str,
        structured_output: Mapping[str, object],
        model_profile_id: str,
    ) -> "AppServerTurnReceipt":
        normalized_output = dict(structured_output)
        return cls(
            thread_id=thread_id,
            turn_id=turn_id,
            status="completed",
            structured_output=normalized_output,
            output_sha256=hashlib.sha256(
                canonical_json_bytes(normalized_output)
            ).hexdigest(),
            model_profile_id=model_profile_id,
        )


def inventory_output_schema() -> dict[str, object]:
    """Return the strict Stage 1 structured-output schema."""

    return {
        "type": "object",
        "properties": {
            "signals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "rule": {"type": "string"},
                        "future_effect": {"type": "string"},
                        "scope": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": [
                                "current_confirmed",
                                "unresolved",
                                "superseded",
                            ],
                        },
                        "confirmation_basis": {
                            "type": "string",
                            "enum": [
                                "explicit_user_confirmation",
                                "explicit_user_direction",
                                "adopted_decision_contract",
                                "uncertain",
                            ],
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                    },
                    "required": [
                        "topic",
                        "rule",
                        "future_effect",
                        "scope",
                        "status",
                        "confirmation_basis",
                        "confidence",
                    ],
                    "additionalProperties": False,
                },
            },
            "coverage": {
                "type": "object",
                "properties": {
                    "reviewed_retained_context": {
                        "type": "string",
                        "enum": ["earliest_to_latest"],
                    },
                    "known_gaps": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "reviewed_retained_context",
                    "known_gaps",
                ],
                "additionalProperties": False,
            },
        },
        "required": ["signals", "coverage"],
        "additionalProperties": False,
    }


def extraction_output_schema(product: str) -> dict[str, object]:
    """Return the strict Stage 2 schema pinned to one product name."""

    product_name = _nonempty(product, "product")
    return {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "product": {
                            "type": "string",
                            "enum": [product_name],
                        },
                        "claim": {"type": "string"},
                        "future_action": {"type": "string"},
                        "scope": {
                            "type": "object",
                            "properties": {
                                "summary": {"type": "string"},
                                "repositories": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "paths": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": [
                                "summary",
                                "repositories",
                                "paths",
                            ],
                            "additionalProperties": False,
                        },
                        "invalidation_conditions": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": [
                        "product",
                        "claim",
                        "future_action",
                        "scope",
                        "invalidation_conditions",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["candidates"],
        "additionalProperties": False,
    }


def _bounded_identifier(value: object, field_name: str) -> str:
    normalized = _nonempty(value, field_name)
    if len(normalized) > 256 or "\x00" in normalized:
        raise ValueError(f"{field_name} is invalid")
    return normalized
