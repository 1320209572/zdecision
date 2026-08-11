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
_RECEIPT_ID = re.compile(r"^rcpt_[0-9a-f]{64}$")


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


def inventory_output_schema(
    evidence_receipt_ids: tuple[str, ...] | None = None,
) -> dict[str, object]:
    """Return the legacy schema or a receipt-bounded v5 schema."""

    if evidence_receipt_ids is not None:
        _receipt_enum(evidence_receipt_ids)

    return {
        "type": "object",
        "properties": {
            "signals": {
                "type": "array",
                **(
                    {} if evidence_receipt_ids is None else {"maxItems": 100}
                ),
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
                        **(
                            {}
                            if evidence_receipt_ids is None
                            else {
                                "signal_ordinal": {"type": "integer"},
                                "evidence_receipt_ids": {
                                    "type": "array",
                                    "items": {
                                        "type": "string",
                                        "enum": list(evidence_receipt_ids),
                                    },
                                    "maxItems": len(evidence_receipt_ids),
                                    "uniqueItems": True,
                                },
                            }
                        ),
                    },
                    "required": [
                        "topic",
                        "rule",
                        "future_effect",
                        "scope",
                        "status",
                        "confirmation_basis",
                        "confidence",
                        *(
                            []
                            if evidence_receipt_ids is None
                            else ["signal_ordinal", "evidence_receipt_ids"]
                        ),
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


def extraction_output_schema(
    product: str,
    eligible_signal_ordinals: tuple[int, ...] | None = None,
) -> dict[str, object]:
    """Return the legacy schema or a signal-bounded v5 schema."""

    product_name = _nonempty(product, "product")
    if eligible_signal_ordinals is not None:
        _signal_ordinal_enum(eligible_signal_ordinals)
    return {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                **(
                    {}
                    if eligible_signal_ordinals is None
                    else {
                        "maxItems": 20 if eligible_signal_ordinals else 0
                    }
                ),
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
                        **(
                            {}
                            if not eligible_signal_ordinals
                            else {
                                "source_signal_ordinal": {
                                    "type": "integer",
                                    "enum": list(eligible_signal_ordinals),
                                }
                            }
                        ),
                    },
                    "required": [
                        "product",
                        "claim",
                        "future_action",
                        "scope",
                        "invalidation_conditions",
                        *(
                            []
                            if not eligible_signal_ordinals
                            else ["source_signal_ordinal"]
                        ),
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


def _receipt_enum(value: tuple[str, ...]) -> None:
    if (
        not isinstance(value, tuple)
        or not 1 <= len(value) <= 100
        or len(set(value)) != len(value)
        or any(_RECEIPT_ID.fullmatch(item) is None for item in value)
    ):
        raise ValueError("evidence_receipt_ids are invalid")


def _signal_ordinal_enum(value: tuple[int, ...]) -> None:
    if (
        not isinstance(value, tuple)
        or len(value) > 100
        or len(set(value)) != len(value)
        or any(
            not isinstance(item, int)
            or isinstance(item, bool)
            or not 1 <= item <= 100
            for item in value
        )
    ):
        raise ValueError("eligible_signal_ordinals are invalid")
