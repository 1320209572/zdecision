"""Strict, privacy-bounded values for local lifecycle observations."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from zdecision.jsonio import canonical_json_bytes


HookEventName = Literal[
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "Stop",
    "SessionEnd",
]
EventState = Literal[
    "recorded",
    "processing",
    "consumed",
    "deferred",
    "failed_retryable",
    "failed_terminal",
]

HOOK_EVENT_NAMES = frozenset(
    ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd")
)
TURN_SCOPED_EVENTS = frozenset(("UserPromptSubmit", "PostToolUse", "Stop"))
EVENT_STATES = frozenset(
    (
        "recorded",
        "processing",
        "consumed",
        "deferred",
        "failed_retryable",
        "failed_terminal",
    )
)
WORK_STATES = frozenset(
    (
        "exploring",
        "implementing",
        "awaiting_user",
        "validation_failed",
        "milestone_complete",
    )
)
VALIDATION_STATES = frozenset(("passed", "failed", "not_applicable", "unknown"))

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:-]{0,255}$")
_VALIDATION_COMMAND = re.compile(
    r"(?:\bpytest\b|\bunittest\b|\bcargo\s+test\b|\bgo\s+test\b|"
    r"\bnpm\s+(?:run\s+)?test\b|\bpnpm\s+(?:run\s+)?test\b|"
    r"\byarn\s+(?:run\s+)?test\b|\bmake\s+test\b)",
    re.IGNORECASE,
)
_GIT_COMMIT_COMMAND = re.compile(r"(?:^|[;&|]\s*)git\s+commit(?:\s|$)", re.IGNORECASE)
_GIT_COMMAND = re.compile(r"(?:^|[;&|]\s*)git(?:\s|$)", re.IGNORECASE)


class InvalidHookInvocation(ValueError):
    """Raised when a host Hook value cannot be normalized safely."""


@dataclass(frozen=True)
class RepositorySnapshot:
    repository_id: str
    worktree_root: str
    branch: str | None
    head_commit: str


@dataclass(frozen=True)
class TestRepositoryMapping:
    repository_id: str
    product_id: str
    product_name: str
    enabled: bool


@dataclass(frozen=True)
class HookInvocation:
    event_name: HookEventName
    session_id: str
    turn_id: str | None
    cwd: str
    occurred_at: str
    repository_id: str | None
    worktree_root: str | None
    branch: str | None
    head_commit: str | None
    source: str | None
    tool_name: str | None
    safe_fact: Mapping[str, object]
    input_digest: str

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, object],
        *,
        occurred_at: str,
        repository: RepositorySnapshot | None = None,
    ) -> "HookInvocation":
        if not isinstance(value, Mapping):
            raise InvalidHookInvocation("Hook input must be an object")
        event_name_value = value.get("hook_event_name")
        if event_name_value not in HOOK_EVENT_NAMES:
            raise InvalidHookInvocation("Hook event name is invalid")
        event_name = cast(HookEventName, event_name_value)
        session_id = _safe_identifier(value.get("session_id"), "session_id")
        cwd = _safe_cwd(value.get("cwd"))
        turn_id: str | None = None
        if event_name in TURN_SCOPED_EVENTS:
            turn_id = _safe_identifier(value.get("turn_id"), "turn_id")

        source: str | None = None
        tool_name: str | None = None
        safe_fact: dict[str, object] = {}
        hidden_discriminator: str | None = None
        if event_name == "SessionStart":
            source_value = value.get("source")
            if source_value not in ("startup", "resume", "clear", "compact"):
                raise InvalidHookInvocation("Session start source is invalid")
            source = cast(str, source_value)
            safe_fact = {"source": source}
        elif event_name == "SessionEnd":
            reason = value.get("reason")
            if reason != "other":
                raise InvalidHookInvocation("Session end reason is invalid")
            safe_fact = {"reason": "other"}
        elif event_name == "PostToolUse":
            tool_name = _safe_tool_name(value.get("tool_name"))
            tool_use_id = _safe_identifier(value.get("tool_use_id"), "tool_use_id")
            hidden_discriminator = hashlib.sha256(tool_use_id.encode("utf-8")).hexdigest()
            classification = _classify_tool_input(tool_name, value.get("tool_input"))
            if classification is not None:
                safe_fact["classification"] = classification
            exit_status = _extract_exit_status(value.get("tool_response"))
            if exit_status is not None:
                safe_fact["exit_status"] = exit_status

        return _build_invocation(
            event_name=event_name,
            session_id=session_id,
            turn_id=turn_id,
            cwd=cwd,
            occurred_at=_safe_occurrence_time(occurred_at),
            repository=repository,
            source=source,
            tool_name=tool_name,
            safe_fact=safe_fact,
            hidden_discriminator=hidden_discriminator,
        )


@dataclass(frozen=True)
class HookResponse:
    event_id: str
    output: Mapping[str, object]


@dataclass(frozen=True)
class AgentEvent:
    event_id: str
    invocation: HookInvocation
    state: EventState
    failure_code: str | None


def event_id_for(invocation: HookInvocation) -> str:
    """Return the replay-stable identity for one normalized observation."""

    return f"evt_{invocation.input_digest[:32]}"


def local_fact_invocation(
    *,
    session_id: str,
    turn_id: str,
    cwd: str,
    occurred_at: str,
    repository: RepositorySnapshot | None,
    fact_kind: Literal["work_state", "manual_submit"],
    status: str | None = None,
    validation: str | None = None,
    unresolved_blocker_count: int = 0,
) -> HookInvocation:
    """Build a bounded local-tool fact without retaining arbitrary tool text."""

    normalized_session = _safe_identifier(session_id, "session_id")
    normalized_turn = _safe_identifier(turn_id, "turn_id")
    normalized_cwd = _safe_cwd(cwd)
    if fact_kind == "work_state":
        if status not in WORK_STATES or validation not in VALIDATION_STATES:
            raise InvalidHookInvocation("Work state report is invalid")
        if not isinstance(unresolved_blocker_count, int) or isinstance(
            unresolved_blocker_count, bool
        ):
            raise InvalidHookInvocation("Blocker count is invalid")
        if not 0 <= unresolved_blocker_count <= 100:
            raise InvalidHookInvocation("Blocker count is invalid")
        safe_fact: dict[str, object] = {
            "report_kind": "work_state",
            "status": status,
            "validation": validation,
            "unresolved_blocker_count": unresolved_blocker_count,
        }
        tool_name = "zdecision.report_work_state"
    else:
        safe_fact = {"report_kind": "manual_submit"}
        tool_name = "zdecision.submit_current_boundary"
    discriminator = hashlib.sha256(
        canonical_json_bytes(
            {
                "fact_kind": fact_kind,
                "safe_fact": safe_fact,
                "session_id": normalized_session,
                "turn_id": normalized_turn,
            }
        )
    ).hexdigest()
    return _build_invocation(
        event_name="PostToolUse",
        session_id=normalized_session,
        turn_id=normalized_turn,
        cwd=normalized_cwd,
        occurred_at=_safe_occurrence_time(occurred_at),
        repository=repository,
        source=None,
        tool_name=tool_name,
        safe_fact=safe_fact,
        hidden_discriminator=discriminator,
    )


def _build_invocation(
    *,
    event_name: HookEventName,
    session_id: str,
    turn_id: str | None,
    cwd: str,
    occurred_at: str,
    repository: RepositorySnapshot | None,
    source: str | None,
    tool_name: str | None,
    safe_fact: Mapping[str, object],
    hidden_discriminator: str | None,
) -> HookInvocation:
    repository_fields: dict[str, object] = {
        "repository_id": None,
        "worktree_root": None,
        "branch": None,
        "head_commit": None,
    }
    if repository is not None:
        repository_fields = {
            "repository_id": repository.repository_id,
            "worktree_root": repository.worktree_root,
            "branch": repository.branch,
            "head_commit": repository.head_commit,
        }
    normalized_safe_fact = _canonical_safe_fact(safe_fact)
    fingerprint: dict[str, object] = {
        "event_name": event_name,
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": cwd,
        "source": source,
        "tool_name": tool_name,
        "safe_fact": normalized_safe_fact,
        **repository_fields,
    }
    if hidden_discriminator is not None:
        fingerprint["discriminator_sha256"] = hidden_discriminator
    input_digest = hashlib.sha256(canonical_json_bytes(fingerprint)).hexdigest()
    return HookInvocation(
        event_name=event_name,
        session_id=session_id,
        turn_id=turn_id,
        cwd=cwd,
        occurred_at=occurred_at,
        repository_id=cast(str | None, repository_fields["repository_id"]),
        worktree_root=cast(str | None, repository_fields["worktree_root"]),
        branch=cast(str | None, repository_fields["branch"]),
        head_commit=cast(str | None, repository_fields["head_commit"]),
        source=source,
        tool_name=tool_name,
        safe_fact=normalized_safe_fact,
        input_digest=input_digest,
    )


def _canonical_safe_fact(value: Mapping[str, object]) -> dict[str, object]:
    try:
        encoded = canonical_json_bytes(dict(value))
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvalidHookInvocation("Safe fact is not canonical JSON") from error
    if not isinstance(decoded, dict) or len(encoded) > 1024:
        raise InvalidHookInvocation("Safe fact exceeds its bound")
    return decoded


def _safe_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise InvalidHookInvocation(f"{field_name} is invalid")
    return value


def _safe_tool_name(value: object) -> str:
    if not isinstance(value, str) or _SAFE_TOOL_NAME.fullmatch(value) is None:
        raise InvalidHookInvocation("tool_name is invalid")
    return value


def _safe_cwd(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise InvalidHookInvocation("cwd is invalid")
    if "\x00" in value or not os.path.isabs(value):
        raise InvalidHookInvocation("cwd is invalid")
    return os.path.normpath(value)


def _safe_occurrence_time(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise InvalidHookInvocation("occurred_at is invalid")
    if any(ord(character) < 32 or ord(character) > 126 for character in value):
        raise InvalidHookInvocation("occurred_at is invalid")
    return value


def _classify_tool_input(tool_name: str, value: object) -> str | None:
    if tool_name != "Bash" or not isinstance(value, Mapping):
        return None
    command = value.get("command")
    if not isinstance(command, str):
        return None
    bounded = command[:4096]
    if _GIT_COMMIT_COMMAND.search(bounded):
        return "git_commit"
    if _VALIDATION_COMMAND.search(bounded):
        return "validation"
    if _GIT_COMMAND.search(bounded):
        return "git"
    return None


def _extract_exit_status(value: object) -> int | None:
    if not isinstance(value, Mapping):
        return None
    for key in ("exit_code", "exitCode"):
        candidate = value.get(key)
        if (
            isinstance(candidate, int)
            and not isinstance(candidate, bool)
            and -255 <= candidate <= 255
        ):
            return candidate
    return None
