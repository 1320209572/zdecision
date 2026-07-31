"""Fast, fail-open lifecycle Hook ingestion."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zdecision.agent.control_bindings import ControlBindingStore
from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import HookInvocation, HookResponse, InvalidHookInvocation
from zdecision.agent.repository import RepositoryResolver
from zdecision.agent.worker import wake_worker


INVALID_HOOK_OUTPUT = {"systemMessage": "ZDecision ignored an invalid hook event."}
UNAVAILABLE_HOOK_OUTPUT = {
    "systemMessage": "ZDecision could not record this lifecycle event."
}
CONTROL_BINDING_TOOL = "mcp__zdecision_local__show_zdecision_update"
_CONTROL_ID = re.compile(r"^ctl_[0-9a-f]{32}$")
_SAFE_HOST_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def handle_hook(
    raw: object,
    *,
    database: AgentDatabase,
    clock: Callable[[], datetime | str],
    repository_resolver: RepositoryResolver | None = None,
    worker_waker: Callable[[Path], None] | None = None,
    control_store: ControlBindingStore | None = None,
    control_id_factory: Callable[[], str] | None = None,
    session_lease_seconds: float = 120.0,
) -> HookResponse:
    """Validate and record one Hook without retaining discarded host fields."""

    try:
        value = _decode_hook(raw)
        if value.get("hook_event_name") == "PreToolUse":
            return handle_control_binding_hook(
                value,
                database=database,
                clock=clock,
                repository_resolver=repository_resolver,
                control_store=control_store,
                control_id_factory=control_id_factory,
            )
        observed_at = clock()
        occurred_at = _format_time(observed_at)
        lease_time = _parse_time(occurred_at)
        unbound = HookInvocation.from_dict(value, occurred_at=occurred_at)
        resolver = repository_resolver or RepositoryResolver()
        repository = resolver.resolve(unbound.cwd)
        if repository is None:
            return HookResponse(event_id="", output={})
        mapping = database.get_repository_mapping(repository.repository_id)
        if mapping is None or not mapping.enabled:
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
        (worker_waker or wake_worker)(database.path)
        return HookResponse(event_id=event.event_id, output={})
    except (InvalidHookInvocation, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return HookResponse(event_id="", output=INVALID_HOOK_OUTPUT)
    except Exception:
        return HookResponse(event_id="", output=UNAVAILABLE_HOOK_OUTPUT)


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
        mapping = database.get_repository_mapping(repository.repository_id)
        if mapping is None or not mapping.enabled:
            raise ValueError("repository is not enabled")
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
            product_id=mapping.product_id,
            created_at=created_at,
            expires_at=created_at + timedelta(minutes=15),
            control_id=control_id,
        )
        updated_input = {"control_id": control_id}
    except Exception:
        updated_input = {}
    finally:
        if owned_store is not None:
            try:
                owned_store.close()
            except Exception:
                updated_input = {}
    return HookResponse(
        event_id="",
        output={
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": updated_input,
            }
        },
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
