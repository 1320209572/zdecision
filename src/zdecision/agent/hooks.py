"""Fast, fail-open lifecycle Hook ingestion."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zdecision.agent.control_bindings import ControlBindingStore
from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import HookInvocation, HookResponse, InvalidHookInvocation
from zdecision.agent.recall_host_state import (
    RecallGateConflict,
    RecallHostStore,
    installed_recall_skill_path,
)
from zdecision.agent.repository import RepositoryResolver
from zdecision.ids import product_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.recall.handoff import (
    RecallApplicationSubmission,
    RecallPreflightClarification,
    RecallPreflightReady,
    RecallPreflightUnavailable,
)
from zdecision.recall.provider import RecallProvider, UnavailableRecallProvider
from zdecision.recall.session import RecallIntent


INVALID_HOOK_OUTPUT = {"systemMessage": "ZDecision ignored an invalid hook event."}
UNAVAILABLE_HOOK_OUTPUT = {
    "systemMessage": "ZDecision could not record this lifecycle event."
}
CONTROL_BINDING_TOOL = "mcp__zdecision_local__show_zdecision_update"
SHOW_RECALL_CONFIRMATION_TOOL = "mcp__zdecision_local__show_zdecision_recall_confirmation"
TURN_GATE_TOOL = "mcp__zdecision_local__gate_zdecision_turn"
APPLY_RECALL_DELIVERY_TOOL = (
    "mcp__zdecision_local__apply_zdecision_recall_delivery"
)
RECALL_MUTATION_MATCHER = "Bash|apply_patch|Edit|Write|Agent|mcp__.*"
_ACTIVATING_APP_TOOLS = frozenset(
    (
        "mcp__zdecision_local__decide_zdecision_recall",
        "mcp__zdecision_local__get_zdecision_recall_handoff",
        "mcp__zdecision_local__ack_zdecision_recall_delivery",
    )
)
_CONTROL_ID = re.compile(r"^ctl_[0-9a-f]{32}$")
_SAFE_HOST_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_HOOK_STORE_TIMEOUT_SECONDS = 0.05
_RECALL_BINDING_STORE_TIMEOUT_SECONDS = 1.0
_CONFIRMATION_LIFETIME = timedelta(minutes=15)
_RECALL_BINDING_DENIED_REASON = (
    "ZDecision could not create a trusted Recall binding. "
    "Do not retry or guess identifiers."
)
_RECALL_PREFLIGHT_UNAVAILABLE_REASON = (
    "ZDecision Recall is unavailable for this task. "
    "Do not retry or guess identifiers."
)
_RECALL_GATE_DENIED_REASON = (
    "ZDecision Recall has not completed the required gate for this action."
)
_ACTIVE_GATE_INSTRUCTION = (
    "ZDecision recall is active. Call gate_zdecision_turn before substantive "
    "output or development tools in this Turn."
)
_APPLICATION_INSTRUCTION = (
    "ZDecision decisions were delivered for this task. Classify every delivered "
    "item and call apply_zdecision_recall_delivery before development tools."
)
_RESUME_INSTRUCTION = (
    "ZDecision recall revalidation is required before development continues."
)


def handle_hook(
    raw: object,
    *,
    database: AgentDatabase,
    clock: Callable[[], datetime | str],
    repository_resolver: RepositoryResolver | None = None,
    worker_waker: Callable[[Path], None] | None = None,
    control_store: ControlBindingStore | None = None,
    control_id_factory: Callable[[], str] | None = None,
    recall_store: RecallHostStore | None = None,
    recall_provider: RecallProvider | None = None,
    activation_attempt_id_factory: Callable[[], str] | None = None,
    activation_binding_id_factory: Callable[[], str] | None = None,
    turn_gate_id_factory: Callable[..., str] | None = None,
    session_lease_seconds: float = 120.0,
) -> HookResponse:
    """Validate and record one Hook without retaining discarded host fields."""

    try:
        value = _decode_hook(raw)
        if value.get("hook_event_name") == "PreToolUse":
            return handle_pre_tool_hook(
                value,
                database=database,
                clock=clock,
                repository_resolver=repository_resolver,
                control_store=control_store,
                control_id_factory=control_id_factory,
                recall_store=recall_store,
                recall_provider=recall_provider,
                activation_attempt_id_factory=(
                    activation_attempt_id_factory or activation_binding_id_factory
                ),
                turn_gate_id_factory=turn_gate_id_factory,
            )
        observed_at = clock()
        occurred_at = _format_time(observed_at)
        lease_time = _parse_time(occurred_at)
        unbound = HookInvocation.from_dict(value, occurred_at=occurred_at)
        resolver = repository_resolver or RepositoryResolver()
        repository = resolver.resolve(unbound.cwd)
        if repository is None:
            return HookResponse(event_id="", output={})
        enabled = database.get_enabled_repository(repository.repository_id)
        if enabled is None or not enabled.enabled:
            return HookResponse(event_id="", output={})
        invocation = HookInvocation.from_dict(
            value,
            occurred_at=occurred_at,
            repository=repository,
        )
        event = database.record_hook(invocation)
        expires_at = lease_time + timedelta(seconds=session_lease_seconds)
        if invocation.event_name == "SessionStart":
            database.renew_session(
                invocation.session_id,
                invocation.cwd,
                renewed_at=lease_time,
                expires_at=expires_at,
                create=True,
            )
        elif invocation.event_name == "SessionEnd":
            database.end_session(invocation.session_id, ended_at=lease_time)
        else:
            database.renew_session(
                invocation.session_id,
                invocation.cwd,
                renewed_at=lease_time,
                expires_at=expires_at,
                create=False,
            )
        recall_output = _handle_recall_lifecycle(
            value,
            invocation=invocation,
            database=database,
            recall_store=recall_store,
            now=lease_time,
            turn_gate_id_factory=turn_gate_id_factory,
        )
        if worker_waker is None:
            from zdecision.agent.worker import wake_worker

            worker_waker = wake_worker
        worker_waker(database.path)
        return HookResponse(event_id=event.event_id, output=recall_output)
    except (InvalidHookInvocation, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return HookResponse(event_id="", output=INVALID_HOOK_OUTPUT)
    except Exception:
        return HookResponse(event_id="", output=UNAVAILABLE_HOOK_OUTPUT)


def handle_pre_tool_hook(
    value: Mapping[str, object],
    *,
    database: AgentDatabase,
    clock: Callable[[], datetime | str],
    repository_resolver: RepositoryResolver | None = None,
    control_store: ControlBindingStore | None = None,
    control_id_factory: Callable[[], str] | None = None,
    recall_store: RecallHostStore | None = None,
    recall_provider: RecallProvider | None = None,
    activation_attempt_id_factory: Callable[[], str] | None = None,
    activation_binding_id_factory: Callable[[], str] | None = None,
    turn_gate_id_factory: Callable[..., str] | None = None,
) -> HookResponse:
    """Dispatch trusted PreToolUse controls before the active-Turn guard."""

    tool_name = value.get("tool_name")
    if tool_name == CONTROL_BINDING_TOOL:
        return handle_control_binding_hook(
            value,
            database=database,
            clock=clock,
            repository_resolver=repository_resolver,
            control_store=control_store,
            control_id_factory=control_id_factory,
        )
    recall_binding = tool_name in (
        SHOW_RECALL_CONFIRMATION_TOOL,
        TURN_GATE_TOOL,
        APPLY_RECALL_DELIVERY_TOOL,
    )
    owned_store: RecallHostStore | None = None
    try:
        store = recall_store
        if store is None:
            owned_store = RecallHostStore.open(
                database.path,
                timeout_seconds=(
                    _RECALL_BINDING_STORE_TIMEOUT_SECONDS
                    if recall_binding
                    else _HOOK_STORE_TIMEOUT_SECONDS
                ),
            )
            store = owned_store
        if recall_binding:
            return bind_recall_tool_call(
                value,
                database=database,
                clock=clock,
                repository_resolver=repository_resolver,
                recall_store=store,
                recall_provider=recall_provider,
                activation_attempt_id_factory=(
                    activation_attempt_id_factory or activation_binding_id_factory
                ),
                turn_gate_id_factory=turn_gate_id_factory,
            )
        return guard_active_turn_tool(value, database=database, recall_store=store)
    except Exception:
        if recall_binding:
            return _pre_tool_response(
                "deny", reason=_RECALL_BINDING_DENIED_REASON
            )
        return HookResponse(event_id="", output={})
    finally:
        if owned_store is not None:
            try:
                owned_store.close()
            except Exception:
                pass


def bind_recall_tool_call(
    value: Mapping[str, object],
    *,
    database: AgentDatabase,
    clock: Callable[[], datetime | str],
    repository_resolver: RepositoryResolver | None,
    recall_store: RecallHostStore,
    activation_attempt_id_factory: Callable[[], str] | None,
    turn_gate_id_factory: Callable[..., str] | None,
    recall_provider: RecallProvider | None = None,
) -> HookResponse:
    """Replace model-authored recall coordinates with one trusted binding."""

    try:
        session_id, turn_id, cwd = _trusted_recall_coordinates(
            value,
            database=database,
            repository_resolver=repository_resolver,
        )
        plugin_root = _trusted_plugin_root()
        tool_name = value.get("tool_name")
        if tool_name == SHOW_RECALL_CONFIRMATION_TOOL:
            tool_input = value.get("tool_input")
            if (
                not isinstance(tool_input, Mapping)
                or frozenset(tool_input) not in (
                    frozenset(("intent",)),
                    frozenset(("activation_attempt_id", "intent")),
                )
            ):
                raise ValueError("recall confirmation input is invalid")
            intent = RecallIntent.from_dict(tool_input["intent"])
            repository = (repository_resolver or RepositoryResolver()).resolve(cwd)
            if repository is None:
                raise ValueError("repository is unresolved")
            now = _parse_time(_format_time(clock()))
            provider = recall_provider or UnavailableRecallProvider()
            preflight = provider.preflight(
                repository_id=repository.repository_id,
                repository_display_name=Path(repository.worktree_root).name,
                intent=intent,
                now=now,
            )
            if isinstance(preflight, RecallPreflightClarification):
                return _pre_tool_response(
                    "deny",
                    reason="Clarify the Recall target: "
                    + ", ".join(preflight.candidate_display_names),
                )
            if isinstance(preflight, RecallPreflightUnavailable):
                return _pre_tool_response(
                    "deny", reason=_RECALL_PREFLIGHT_UNAVAILABLE_REASON
                )
            if not isinstance(preflight, RecallPreflightReady):
                raise ValueError("recall preflight result is invalid")
            attempt_id = (
                activation_attempt_id_factory()
                if activation_attempt_id_factory is not None
                else _opaque_id("activation", session_id, turn_id, cwd)
            )
            attempt_id = _safe_generated_identifier(attempt_id)
            recall_store.create_activation_attempt(
                session_id=session_id,
                turn_id=turn_id,
                cwd=cwd,
                repository_id=repository.repository_id,
                repository_display_name=preflight.repository_display_name,
                attempt_id=attempt_id,
                now=now,
                expires_at=now + _CONFIRMATION_LIFETIME,
                plugin_root=plugin_root,
                intent=intent,
                preflight=preflight,
            )
            return _pre_tool_response(
                "allow",
                updated_input={
                    "activation_attempt_id": attempt_id,
                    "intent": intent.to_dict(),
                },
            )
        if tool_name == APPLY_RECALL_DELIVERY_TOOL:
            tool_input = value.get("tool_input")
            if (
                not isinstance(tool_input, Mapping)
                or frozenset(tool_input)
                != frozenset(("turn_gate_id", "delivery_id", "items"))
            ):
                raise ValueError("recall application input is invalid")
            session = recall_store.get_session(session_id)
            if (
                session is None
                or session.state != "activating"
                or session.cwd != cwd
            ):
                raise RecallGateConflict("session is not awaiting application")
            delivery = recall_store.eligible_delivery_for_session(session_id)
            if delivery is None:
                raise RecallGateConflict("eligible delivery was not found")
            gate = recall_store.get_turn_gate(session_id, turn_id)
            if (
                gate is None
                or gate.state != "pending"
                or gate.active_generation != delivery.preflight.generation
            ):
                raise RecallGateConflict("application gate is not pending")
            submission = RecallApplicationSubmission.from_dict(
                {"delivery_id": delivery.delivery_id, "items": tool_input["items"]}
            )
            return _pre_tool_response(
                "allow",
                updated_input={
                    "turn_gate_id": gate.gate_id,
                    "delivery_id": delivery.delivery_id,
                    "items": [item.to_dict() for item in submission.items],
                },
            )
        if tool_name != TURN_GATE_TOOL:
            raise ValueError("recall tool is invalid")
        tool_input = value.get("tool_input")
        if not isinstance(tool_input, Mapping) or "intent" not in tool_input:
            raise ValueError("recall intent is missing")
        intent = tool_input["intent"]
        session = recall_store.get_session(session_id)
        if session is None or session.state != "active" or session.cwd != cwd:
            raise RecallGateConflict("session is not active for this CWD")
        gate_id = _turn_gate_id(
            session_id,
            turn_id,
            session.context_epoch,
            session.intent_epoch,
            None,
            turn_gate_id_factory,
        )
        recall_store.begin_turn_gate(
            session_id=session_id,
            turn_id=turn_id,
            context_epoch=session.context_epoch,
            intent_epoch=session.intent_epoch,
            active_generation=None,
            gate_id=gate_id,
            plugin_root=plugin_root,
        )
        return _pre_tool_response(
            "allow",
            updated_input={"turn_gate_id": gate_id, "intent": intent},
        )
    except Exception:
        return _pre_tool_response("deny", reason=_RECALL_BINDING_DENIED_REASON)


def guard_active_turn_tool(
    value: Mapping[str, object],
    *,
    database: AgentDatabase,
    recall_store: RecallHostStore,
) -> HookResponse:
    """Deny only selected Sessions whose exact current Turn is not committed."""

    try:
        session_id = _safe_host_identifier(value.get("session_id"))
    except ValueError:
        return HookResponse(event_id="", output={})
    session = recall_store.get_session(session_id)
    if session is None or session.state not in ("active", "activating", "blocked"):
        return HookResponse(event_id="", output={})
    if value.get("tool_name") in _ACTIVATING_APP_TOOLS:
        return HookResponse(event_id="", output={})
    if session.state != "active":
        return _pre_tool_response("deny", reason=_RECALL_GATE_DENIED_REASON)
    try:
        turn_id = _safe_host_identifier(value.get("turn_id"))
        cwd = _safe_absolute_cwd(value.get("cwd"))
        if session.cwd != cwd:
            raise RecallGateConflict("active Session CWD changed")
        if not database.has_open_observed_turn(session_id, turn_id, cwd):
            raise RecallGateConflict("host Turn is not current")
        recall_store.require_committed_gate(session_id, turn_id)
    except Exception:
        return _pre_tool_response("deny", reason=_RECALL_GATE_DENIED_REASON)
    return HookResponse(event_id="", output={})


def _handle_recall_lifecycle(
    value: Mapping[str, object],
    *,
    invocation: HookInvocation,
    database: AgentDatabase,
    recall_store: RecallHostStore | None,
    now: datetime,
    turn_gate_id_factory: Callable[..., str] | None,
) -> Mapping[str, object]:
    if invocation.event_name not in (
        "UserPromptSubmit",
        "SessionStart",
        "SessionEnd",
    ):
        return {}
    owned_store: RecallHostStore | None = None
    try:
        store = recall_store
        if store is None:
            owned_store = RecallHostStore.open(
                database.path, timeout_seconds=_HOOK_STORE_TIMEOUT_SECONDS
            )
            store = owned_store
        session = store.get_session(invocation.session_id)
        if invocation.event_name == "SessionEnd":
            store.retire_activation_attempts(invocation.session_id, now=now)
            if session is not None:
                store.mark_dormant(invocation.session_id, ended_at=now)
            return {}
        if invocation.event_name == "UserPromptSubmit":
            if session is None or session.state not in ("active", "activating"):
                return {}
            if invocation.turn_id is None:
                return _additional_context(
                    "UserPromptSubmit", _blocked_envelope("invalid_native_turn")
                )
            try:
                plugin_root = _trusted_plugin_root()
            except ValueError:
                return _additional_context(
                    "UserPromptSubmit",
                    _blocked_envelope("plugin_runtime_unavailable"),
                )
            delivery = (
                store.eligible_delivery_for_session(invocation.session_id)
                if session.state == "activating"
                else None
            )
            if session.state == "activating" and delivery is None:
                return _additional_context(
                    "UserPromptSubmit", _blocked_envelope("delivery_unavailable")
                )
            active_generation = (
                delivery.preflight.generation if delivery is not None else None
            )
            gate_id = _turn_gate_id(
                invocation.session_id,
                invocation.turn_id,
                session.context_epoch,
                session.intent_epoch,
                active_generation,
                turn_gate_id_factory,
            )
            try:
                store.begin_turn_gate(
                    session_id=invocation.session_id,
                    turn_id=invocation.turn_id,
                    context_epoch=session.context_epoch,
                    intent_epoch=session.intent_epoch,
                    active_generation=active_generation,
                    gate_id=gate_id,
                    plugin_root=plugin_root,
                )
            except RecallGateConflict:
                return _additional_context(
                    "UserPromptSubmit", _blocked_envelope("invalid_turn_gate")
                )
            instruction = (
                _APPLICATION_INSTRUCTION
                if session.state == "activating"
                else _ACTIVE_GATE_INSTRUCTION
            )
            return _additional_context("UserPromptSubmit", instruction)
        if session is None:
            return {}
        if invocation.source == "resume":
            if session.state == "dormant":
                store.begin_resume(invocation.session_id, invocation.cwd, now)
                return _additional_context("SessionStart", _RESUME_INSTRUCTION)
            return {}
        if invocation.source not in ("compact", "clear"):
            return {}
        if (
            session.state != "active"
            or session.last_gate_turn_id is None
            or session.active_set_digest is None
        ):
            return _additional_context(
                "SessionStart", _blocked_envelope("restoration_unavailable")
            )
        if invocation.source == "compact":
            matched_compaction = _matched_compaction_key(
                value,
                invocation=invocation,
                database=database,
            )
            if matched_compaction is None:
                return _additional_context(
                    "SessionStart", _blocked_envelope("unmatched_compaction")
                )
            compaction_key, compaction_turn_id = matched_compaction
            if not database.has_open_observed_turn(
                invocation.session_id,
                compaction_turn_id,
                invocation.cwd,
            ):
                return _additional_context(
                    "SessionStart", _blocked_envelope("unmatched_compaction")
                )
            compact_gate = store.get_turn_gate(
                invocation.session_id, compaction_turn_id
            )
            if compact_gate is not None and compact_gate.state == "blocked":
                return _additional_context(
                    "SessionStart", _blocked_envelope("restoration_conflict")
                )
            if compact_gate is not None and compact_gate.state == "pending":
                pending_turn_id = compaction_turn_id
                pending_gate_id = _turn_gate_id(
                    invocation.session_id,
                    compaction_turn_id,
                    session.context_epoch,
                    session.intent_epoch,
                    compact_gate.active_generation,
                    turn_gate_id_factory,
                )
                rebased_gate_id = _turn_gate_id(
                    invocation.session_id,
                    compaction_turn_id,
                    session.context_epoch + 1,
                    session.intent_epoch,
                    compact_gate.active_generation,
                    turn_gate_id_factory,
                )
            else:
                pending_turn_id = None
                pending_gate_id = None
                rebased_gate_id = None
        else:
            compaction_key = _opaque_id(
                "clear",
                invocation.session_id,
                invocation.source,
                session.last_gate_turn_id,
                session.active_set_digest,
            )
            pending_turn_id = None
            pending_gate_id = None
            rebased_gate_id = None
        try:
            restoration = store.begin_context_epoch(
                session_id=invocation.session_id,
                source=invocation.source,
                latest_observed_turn_id=session.last_gate_turn_id,
                active_set_digest=session.active_set_digest,
                compaction_key=compaction_key,
                pending_turn_id=pending_turn_id,
                pending_gate_id=pending_gate_id,
                rebased_gate_id=rebased_gate_id,
            )
        except RecallGateConflict:
            return _additional_context(
                "SessionStart", _blocked_envelope("restoration_conflict")
            )
        receipt_id = _opaque_id(
            "restoration", restoration.compaction_key, restoration.context_epoch
        )
        return _additional_context(
            "SessionStart",
            _json_context(
                {
                    "marker": "ZDECISION_RECALL_RESTORATION",
                    "receipt_id": receipt_id,
                    "context_epoch": restoration.context_epoch,
                    "active_set_digest": restoration.active_set_digest,
                }
            ),
        )
    finally:
        if owned_store is not None:
            owned_store.close()


def _matched_compaction_key(
    value: Mapping[str, object],
    *,
    invocation: HookInvocation,
    database: AgentDatabase,
) -> tuple[str, str] | None:
    try:
        native_turn_id = _safe_host_identifier(value.get("turn_id"))
    except ValueError:
        return None
    relevant = tuple(
        event.invocation
        for event in database.list_events(invocation.session_id)
        if event.invocation.event_name in (
            "PreCompact",
            "PostCompact",
            "SessionStart",
        )
    )
    if len(relevant) < 3:
        return None
    pre_compact, post_compact, session_start = relevant[-3:]
    if (
        pre_compact.event_name != "PreCompact"
        or post_compact.event_name != "PostCompact"
        or session_start.event_name != "SessionStart"
        or session_start.source != "compact"
        or pre_compact.turn_id != native_turn_id
        or post_compact.turn_id != native_turn_id
        or pre_compact.cwd != invocation.cwd
        or post_compact.cwd != invocation.cwd
        or pre_compact.safe_fact.get("trigger")
        != post_compact.safe_fact.get("trigger")
    ):
        return None
    return (
        _opaque_id(
            "compact",
            invocation.session_id,
            native_turn_id,
            pre_compact.safe_fact["trigger"],
        ),
        native_turn_id,
    )


def _trusted_recall_coordinates(
    value: Mapping[str, object],
    *,
    database: AgentDatabase,
    repository_resolver: RepositoryResolver | None,
) -> tuple[str, str, str]:
    if value.get("hook_event_name") != "PreToolUse" or "agent_id" in value:
        raise ValueError("untrusted recall binding envelope")
    session_id = _safe_host_identifier(value.get("session_id"))
    turn_id = _safe_host_identifier(value.get("turn_id"))
    cwd = _safe_absolute_cwd(value.get("cwd"))
    repository = (repository_resolver or RepositoryResolver()).resolve(cwd)
    if repository is None:
        raise ValueError("repository is unresolved")
    enabled = database.get_enabled_repository(repository.repository_id)
    if enabled is None or not enabled.enabled:
        raise ValueError("repository is not enabled")
    if not database.has_open_observed_turn(session_id, turn_id, cwd):
        raise ValueError("host Turn was not observed")
    return session_id, turn_id, cwd


def _trusted_plugin_root() -> str:
    skill_path = installed_recall_skill_path(os.environ.get("PLUGIN_ROOT"))
    if skill_path is None:
        raise ValueError("trusted plugin root is unavailable")
    return str(skill_path.parents[2])


def _turn_gate_id(
    session_id: str,
    turn_id: str,
    context_epoch: int,
    intent_epoch: int,
    active_generation: int | None,
    factory: Callable[..., str] | None,
) -> str:
    value = (
        factory(
            session_id,
            turn_id,
            context_epoch,
            intent_epoch,
            active_generation,
        )
        if factory is not None
        else _opaque_id(
            "gate",
            session_id,
            turn_id,
            context_epoch,
            intent_epoch,
            active_generation,
        )
    )
    return _safe_generated_identifier(value)


def _opaque_id(prefix: str, *coordinates: object) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes(
            {"domain": f"zdecision-recall-{prefix}-v1", "coordinates": coordinates}
        )
    ).hexdigest()
    return f"{prefix}_{digest[:32]}"


def _safe_generated_identifier(value: object) -> str:
    if not isinstance(value, str) or _SAFE_HOST_IDENTIFIER.fullmatch(value) is None:
        raise ValueError("generated binding ID is invalid")
    return value


def _safe_absolute_cwd(value: object) -> str:
    if not isinstance(value, str) or not Path(value).is_absolute() or "\x00" in value:
        raise ValueError("cwd is invalid")
    return os.path.normpath(value)


def _pre_tool_response(
    decision: str,
    *,
    updated_input: Mapping[str, object] | None = None,
    reason: str | None = None,
) -> HookResponse:
    output: dict[str, object] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
    }
    if updated_input is not None:
        output["updatedInput"] = dict(updated_input)
    if decision == "deny":
        if not isinstance(reason, str) or not reason:
            raise ValueError("denied PreToolUse response requires a reason")
        output["permissionDecisionReason"] = reason
    return HookResponse(event_id="", output={"hookSpecificOutput": output})


def _additional_context(event_name: str, context: str) -> dict[str, object]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context,
        }
    }


def _blocked_envelope(reason: str) -> str:
    return _json_context({"marker": "ZDECISION_RECALL_BLOCKED", "reason": reason})


def _json_context(value: Mapping[str, object]) -> str:
    return canonical_json_bytes(dict(value)).decode("utf-8")


def handle_control_binding_hook(
    value: Mapping[str, object],
    *,
    database: AgentDatabase,
    clock: Callable[[], datetime | str],
    repository_resolver: RepositoryResolver | None = None,
    control_store: ControlBindingStore | None = None,
    control_id_factory: Callable[[], str] | None = None,
) -> HookResponse:
    """Replace render input with one trusted, device-local control binding."""

    updated_input: dict[str, str] = {}
    allowed = False
    owned_store: ControlBindingStore | None = None
    try:
        session_id = _safe_host_identifier(value.get("session_id"))
        turn_id = _safe_host_identifier(value.get("turn_id"))
        cwd_value = value.get("cwd")
        if (
            value.get("hook_event_name") != "PreToolUse"
            or value.get("tool_name") != CONTROL_BINDING_TOOL
            or "agent_id" in value
            or not isinstance(cwd_value, str)
            or not Path(cwd_value).is_absolute()
        ):
            raise ValueError("untrusted control binding envelope")
        repository = (repository_resolver or RepositoryResolver()).resolve(cwd_value)
        if repository is None:
            raise ValueError("repository is unresolved")
        enabled = database.get_enabled_repository(repository.repository_id)
        if enabled is None or not enabled.enabled:
            raise ValueError("repository is not enabled")
        mapping = database.get_repository_mapping(repository.repository_id)
        if not database.has_open_observed_turn(session_id, turn_id, cwd_value):
            raise ValueError("host turn was not observed")
        created_at = _parse_time(_format_time(clock()))
        control_id = (
            control_id_factory or (lambda: f"ctl_{secrets.token_hex(16)}")
        )()
        if not isinstance(control_id, str) or _CONTROL_ID.fullmatch(control_id) is None:
            raise ValueError("generated control ID is invalid")
        store = control_store
        if store is None:
            owned_store = ControlBindingStore.open(database.path)
            store = owned_store
        store.create_binding(
            session_id=session_id,
            render_turn_id=turn_id,
            cwd=cwd_value,
            repository_id=repository.repository_id,
            # Kept only for the archive-compatible binding schema. Active
            # refresh authorization is repository-scoped.
            product_id=(
                mapping.product_id
                if mapping is not None
                else product_id(f"Repository {repository.repository_id}")
            ),
            created_at=created_at,
            expires_at=created_at + timedelta(minutes=15),
            control_id=control_id,
        )
        updated_input = {"control_id": control_id}
        allowed = True
    except Exception:
        updated_input = {}
        allowed = False
    finally:
        if owned_store is not None:
            try:
                owned_store.close()
            except Exception:
                updated_input = {}
                allowed = False
    hook_output: dict[str, object] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow" if allowed else "deny",
    }
    if allowed:
        hook_output["updatedInput"] = updated_input
    return HookResponse(
        event_id="",
        output={"hookSpecificOutput": hook_output},
    )


def _decode_hook(raw: object) -> Mapping[str, object]:
    if isinstance(raw, bytes):
        decoded: object = json.loads(raw.decode("utf-8"))
    elif isinstance(raw, str):
        decoded = json.loads(raw)
    else:
        decoded = raw
    if not isinstance(decoded, Mapping):
        raise InvalidHookInvocation("Hook input must be an object")
    return decoded


def _safe_host_identifier(value: object) -> str:
    if not isinstance(value, str) or _SAFE_HOST_IDENTIFIER.fullmatch(value) is None:
        raise ValueError("host identifier is invalid")
    return value


def _format_time(value: datetime | str) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, datetime):
        raise TypeError("clock must return datetime or string")
    if value.tzinfo is None:
        raise ValueError("clock datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("clock string must be an ISO-8601 datetime") from None
    if parsed.tzinfo is None:
        raise ValueError("clock string must be timezone-aware")
    return parsed.astimezone(UTC)
