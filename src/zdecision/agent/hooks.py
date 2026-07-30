"""Fast, fail-open lifecycle Hook ingestion."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import HookInvocation, HookResponse, InvalidHookInvocation
from zdecision.agent.repository import RepositoryResolver


INVALID_HOOK_OUTPUT = {"systemMessage": "ZDecision ignored an invalid hook event."}
UNAVAILABLE_HOOK_OUTPUT = {
    "systemMessage": "ZDecision could not record this lifecycle event."
}


def handle_hook(
    raw: object,
    *,
    database: AgentDatabase,
    clock: Callable[[], datetime | str],
    repository_resolver: RepositoryResolver | None = None,
) -> HookResponse:
    """Validate and record one Hook without retaining discarded host fields."""

    try:
        value = _decode_hook(raw)
        occurred_at = _format_time(clock())
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
        return HookResponse(event_id=event.event_id, output={})
    except (InvalidHookInvocation, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return HookResponse(event_id="", output=INVALID_HOOK_OUTPUT)
    except Exception:
        return HookResponse(event_id="", output=UNAVAILABLE_HOOK_OUTPUT)


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


def _format_time(value: datetime | str) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, datetime):
        raise TypeError("clock must return datetime or string")
    if value.tzinfo is None:
        raise ValueError("clock datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
