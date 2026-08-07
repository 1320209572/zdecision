"""Validated domain gateway over the Codex app-server protocol."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from zdecision.agent.db import (
    AgentDatabase,
    FeasibilityModelProfileConflict,
    StoredFeasibilityModelProfile,
)
from zdecision.app_server.jsonl import (
    AppServerTransport,
    JsonlAppServerClient,
    ProcessJsonlTransport,
)
from zdecision.app_server.models import (
    ActiveTurnEvidence,
    AppServerTurnReceipt,
    FeasibilityModelProfile,
    SelectedSkill,
    SourceBoundary,
    ThreadIdentity,
    TurnItemEvidence,
)
from zdecision.jsonio import canonical_json_bytes


_EVIDENCE_ITEM_TYPES = frozenset(
    (
        "hookPrompt",
        "mcpToolCall",
        "agentMessage",
        "commandExecution",
        "fileChange",
        "contextCompaction",
    )
)
_ZDECISION_RECALL_TOOLS = frozenset(
    ("activate_zdecision_recall", "gate_zdecision_turn")
)


class AppServerGatewayError(Exception):
    """Base class for validated Gateway failures."""


class InvalidAppServerResponse(AppServerGatewayError):
    """A successful protocol response did not satisfy the domain contract."""


class UnknownSourceTurn(AppServerGatewayError):
    """The requested source Turn was not present in the source Thread."""


class IncompleteSourceTurn(AppServerGatewayError):
    """The requested source Turn is not a completed boundary."""


class ModelDiscoveryConflict(AppServerGatewayError):
    """The model catalog changed after the Gate 3 profile was frozen."""


class ActiveModelProfileResolutionConflict(AppServerGatewayError):
    """A concurrent active profile cannot run against the observed catalog."""


class FrozenModelProfileUnavailable(AppServerGatewayError):
    """A profile frozen by a Capture operation is no longer supported."""


class StructuredTurnFailed(AppServerGatewayError):
    """A structured app-server Turn failed or returned invalid output."""


class AppServerUnavailable(AppServerGatewayError):
    """Neither the explicit host route nor controlled fallback is available."""


class AppServerGateway:
    def __init__(
        self,
        *,
        client: JsonlAppServerClient,
        database: AgentDatabase,
        clock: Callable[[], datetime] | None = None,
        turn_timeout_seconds: float = 300.0,
        route: str = "injected",
    ) -> None:
        if turn_timeout_seconds <= 0:
            raise ValueError("turn_timeout_seconds must be positive")
        self.client = client
        self.database = database
        self.clock = clock or (lambda: datetime.now(UTC))
        self.turn_timeout_seconds = turn_timeout_seconds
        self.route = route

    @classmethod
    def connect(
        cls,
        *,
        database: AgentDatabase,
        host_transport: AppServerTransport | None = None,
        process_factory: Callable[[], AppServerTransport] | None = None,
        clock: Callable[[], datetime] | None = None,
        request_timeout_seconds: float = 30.0,
        turn_timeout_seconds: float = 300.0,
    ) -> "AppServerGateway":
        current_time = clock or (lambda: datetime.now(UTC))
        if host_transport is not None:
            try:
                host_client = JsonlAppServerClient(
                    host_transport,
                    default_timeout_seconds=request_timeout_seconds,
                )
                host_client.initialize()
            except Exception:
                try:
                    host_transport.close()
                except Exception:
                    pass
                database.record_app_server_route(
                    route="host",
                    failure_code="host_transport_failed",
                    recorded_at=current_time(),
                )
            else:
                database.record_app_server_route(
                    route="host",
                    failure_code=None,
                    recorded_at=current_time(),
                )
                return cls(
                    client=host_client,
                    database=database,
                    clock=current_time,
                    turn_timeout_seconds=turn_timeout_seconds,
                    route="host",
                )
        else:
            database.record_app_server_route(
                route="host",
                failure_code="host_transport_unavailable",
                recorded_at=current_time(),
            )

        factory = process_factory or ProcessJsonlTransport.launch
        process_transport: AppServerTransport | None = None
        try:
            process_transport = factory()
            process_client = JsonlAppServerClient(
                process_transport,
                default_timeout_seconds=request_timeout_seconds,
            )
            process_client.initialize()
        except Exception:
            if process_transport is not None:
                try:
                    process_transport.close()
                except Exception:
                    pass
            database.record_app_server_route(
                route="controlled_process",
                failure_code="controlled_process_unavailable",
                recorded_at=current_time(),
            )
            raise AppServerUnavailable(
                "Codex app-server automation is unavailable"
            ) from None
        database.record_app_server_route(
            route="controlled_process",
            failure_code=None,
            recorded_at=current_time(),
        )
        return cls(
            client=process_client,
            database=database,
            clock=current_time,
            turn_timeout_seconds=turn_timeout_seconds,
            route="controlled_process",
        )

    def close(self) -> None:
        self.client.close()

    def read_thread_identity(self, thread_id: str) -> ThreadIdentity:
        requested_id = _nonempty(thread_id, "thread_id")
        result = _mapping(
            self.client.request(
                "thread/read",
                {"threadId": requested_id, "includeTurns": False},
            ),
            "thread/read result",
        )
        thread = _mapping(result.get("thread"), "thread/read thread")
        return _thread_identity(thread, requested_id)

    def read_active_turn_evidence(
        self, thread_id: str, turn_id: str
    ) -> ActiveTurnEvidence:
        requested_thread_id = _nonempty(thread_id, "thread_id")
        requested_turn_id = _nonempty(turn_id, "turn_id")
        result = _mapping(
            self.client.request(
                "thread/read",
                {"threadId": requested_thread_id, "includeTurns": True},
            ),
            "thread/read result",
        )
        thread = _mapping(result.get("thread"), "thread/read thread")
        identity = _thread_identity(thread, requested_thread_id)
        turns = thread.get("turns")
        if not isinstance(turns, list) or len(turns) > 4096:
            raise InvalidAppServerResponse("thread/read did not include bounded Turns")
        matches = [
            value
            for value in turns
            if isinstance(value, Mapping) and value.get("id") == requested_turn_id
        ]
        if not matches:
            raise UnknownSourceTurn("The active Turn is not present")
        if len(matches) != 1:
            raise InvalidAppServerResponse("thread/read repeated the active Turn")
        turn = matches[0]
        if turn.get("status") != "inProgress":
            raise IncompleteSourceTurn("The requested Turn is not active")
        items = turn.get("items")
        if not isinstance(items, list) or len(items) > 4096:
            raise InvalidAppServerResponse("active Turn items are invalid")
        selected: list[SelectedSkill] = []
        ordered: list[TurnItemEvidence] = []
        for value in items:
            if not isinstance(value, Mapping):
                raise InvalidAppServerResponse("active Turn item is invalid")
            item_type = value.get("type")
            if item_type == "userMessage":
                selected.extend(_selected_skills(value))
            elif item_type in _EVIDENCE_ITEM_TYPES:
                try:
                    ordered.append(_turn_item_evidence(value, item_type))
                except (TypeError, ValueError) as error:
                    raise InvalidAppServerResponse(
                        "Turn item evidence is invalid"
                    ) from error
        try:
            return ActiveTurnEvidence(
                thread=identity,
                turn_id=requested_turn_id,
                selected_skills=tuple(selected),
                ordered_items=tuple(ordered),
            )
        except (TypeError, ValueError) as error:
            raise InvalidAppServerResponse(
                "active Turn evidence is invalid"
            ) from error

    def read_completed_boundary(
        self, thread_id: str, turn_id: str
    ) -> SourceBoundary:
        source_thread_id = _nonempty(thread_id, "thread_id")
        source_turn_id = _nonempty(turn_id, "turn_id")
        result = _mapping(
            self.client.request(
                "thread/read",
                {"threadId": source_thread_id, "includeTurns": True},
            ),
            "thread/read result",
        )
        thread = _mapping(result.get("thread"), "thread/read thread")
        if thread.get("id") != source_thread_id:
            raise InvalidAppServerResponse("thread/read returned the wrong Thread")
        cwd = thread.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise InvalidAppServerResponse("thread/read returned an invalid cwd")
        turns = thread.get("turns")
        if not isinstance(turns, list):
            raise InvalidAppServerResponse("thread/read did not include Turns")
        matches = [
            value
            for value in turns
            if isinstance(value, Mapping) and value.get("id") == source_turn_id
        ]
        if not matches:
            raise UnknownSourceTurn("The source Turn is not present")
        if len(matches) != 1:
            raise InvalidAppServerResponse("thread/read repeated the source Turn")
        status = matches[0].get("status")
        if status != "completed":
            raise IncompleteSourceTurn("The source Turn is not completed")
        model_id = _optional_string(
            thread.get("model", result.get("model")), "source model"
        )
        reasoning_effort = _optional_string(
            thread.get("reasoningEffort", result.get("reasoningEffort")),
            "source reasoning effort",
        )
        return SourceBoundary(
            thread_id=source_thread_id,
            turn_id=source_turn_id,
            cwd=cwd,
            status="completed",
            model_id=model_id,
            reasoning_effort=reasoning_effort,
        )

    def discover_and_freeze_profile(
        self, boundary: SourceBoundary
    ) -> FeasibilityModelProfile:
        if not isinstance(boundary, SourceBoundary):
            raise TypeError("boundary must be a SourceBoundary")
        catalog = self._discover_models()
        discovery_digest = hashlib.sha256(
            canonical_json_bytes({"models": catalog})
        ).hexdigest()
        selected_model, selected_effort = _select_model(catalog, boundary)
        proposed = FeasibilityModelProfile.create(
            model_id=selected_model,
            reasoning_effort=selected_effort,
            discovery_digest=discovery_digest,
            discovered_at=_format_datetime(self.clock()),
        )
        try:
            stored = self.database.freeze_feasibility_model_profile(
                profile_id=proposed.profile_id,
                model_id=proposed.model_id,
                reasoning_effort=proposed.reasoning_effort,
                discovery_digest=proposed.discovery_digest,
                discovered_at=proposed.discovered_at,
            )
        except FeasibilityModelProfileConflict:
            raise ModelDiscoveryConflict(
                "Model discovery changed after the Gate 3 profile froze"
            ) from None
        return FeasibilityModelProfile(
            profile_id=stored.profile_id,
            model_id=stored.model_id,
            reasoning_effort=stored.reasoning_effort,
            discovery_digest=stored.discovery_digest,
            discovered_at=stored.discovered_at,
        )

    def resolve_active_profile(self) -> FeasibilityModelProfile:
        catalog = self._discover_models()
        stored = self.database.get_feasibility_model_profile()
        active = None if stored is None else _stored_profile(stored)
        if active is not None and _profile_supported(catalog, active):
            return active

        default_model, default_effort = _select_default_model(catalog)
        discovery_digest = hashlib.sha256(
            canonical_json_bytes({"models": catalog})
        ).hexdigest()
        proposed = FeasibilityModelProfile.create(
            model_id=default_model,
            reasoning_effort=default_effort,
            discovery_digest=discovery_digest,
            discovered_at=_format_datetime(self.clock()),
        )
        winner = self.database.activate_feasibility_model_profile(
            expected_profile_id=None if active is None else active.profile_id,
            profile_id=proposed.profile_id,
            model_id=proposed.model_id,
            reasoning_effort=proposed.reasoning_effort,
            discovery_digest=proposed.discovery_digest,
            discovered_at=proposed.discovered_at,
        )
        resolved = _stored_profile(winner)
        if not _profile_supported(catalog, resolved):
            raise ActiveModelProfileResolutionConflict(
                "The active model profile changed during discovery"
            )
        return resolved

    def require_supported_profile(
        self, profile: FeasibilityModelProfile
    ) -> FeasibilityModelProfile:
        if not isinstance(profile, FeasibilityModelProfile):
            raise TypeError("profile must be a FeasibilityModelProfile")
        catalog = self._discover_models()
        if not _profile_supported(catalog, profile):
            raise FrozenModelProfileUnavailable(
                "The frozen Capture model profile is unavailable"
            )
        return profile

    def list_interactive_thread_ids(self, cwd: str) -> frozenset[str]:
        resolved_cwd = _resolved_cwd(cwd)
        seen_ids: set[str] = set()
        page_count = 0
        for archived in (False, True):
            cursor: str | None = None
            seen_cursors: set[str] = set()
            while True:
                page_count += 1
                if page_count > 100:
                    raise InvalidAppServerResponse(
                        "thread/list exceeded the page limit"
                    )
                params: dict[str, object] = {
                    "cwd": [resolved_cwd],
                    "limit": 100,
                    "sourceKinds": ["cli", "vscode", "appServer"],
                    "archived": archived,
                }
                if cursor is not None:
                    params["cursor"] = cursor
                result = _mapping(
                    self.client.request("thread/list", params),
                    "thread/list result",
                )
                data = result.get("data")
                if not isinstance(data, list):
                    raise InvalidAppServerResponse(
                        "thread/list data is invalid"
                    )
                for value in data:
                    thread = _mapping(value, "thread/list entry")
                    thread_id = _nonempty_response(
                        thread.get("id"), "Thread id"
                    )
                    if thread_id in seen_ids:
                        raise InvalidAppServerResponse(
                            "thread/list repeated a Thread id"
                        )
                    seen_ids.add(thread_id)
                cursor = _next_cursor(result, seen_cursors)
                if cursor is None:
                    break
        return frozenset(seen_ids)

    def start_disposable_thread(
        self,
        cwd: str,
        profile: FeasibilityModelProfile,
    ) -> str:
        resolved_cwd = _resolved_cwd(cwd)
        if not isinstance(profile, FeasibilityModelProfile):
            raise TypeError("profile must be a FeasibilityModelProfile")
        result = _mapping(
            self.client.request(
                "thread/start",
                {
                    "cwd": resolved_cwd,
                    "model": profile.model_id,
                    "sandbox": "read-only",
                },
            ),
            "thread/start result",
        )
        thread = _mapping(result.get("thread"), "thread/start thread")
        thread_id = _nonempty_response(
            thread.get("id"), "started Thread id"
        )
        if thread.get("ephemeral") is True or result.get("ephemeral") is True:
            raise InvalidAppServerResponse(
                "thread/start returned an ephemeral Thread"
            )
        if (
            "cwd" in thread
            and thread.get("cwd") != resolved_cwd
        ) or (
            "cwd" in result
            and result.get("cwd") != resolved_cwd
        ):
            raise InvalidAppServerResponse(
                "thread/start returned the wrong cwd"
            )
        if (
            "model" in thread
            and thread.get("model") != profile.model_id
        ) or (
            "model" in result
            and result.get("model") != profile.model_id
        ):
            raise InvalidAppServerResponse(
                "thread/start returned the wrong model"
            )
        return thread_id

    def fork_disposable_thread(
        self,
        thread_id: str,
        last_turn_id: str,
    ) -> str:
        source_thread_id = _nonempty(thread_id, "thread_id")
        source_turn_id = _nonempty(last_turn_id, "last_turn_id")
        result = _mapping(
            self.client.request(
                "thread/fork",
                {
                    "threadId": source_thread_id,
                    "lastTurnId": source_turn_id,
                },
            ),
            "thread/fork result",
        )
        thread = _mapping(result.get("thread"), "thread/fork thread")
        fork_id = _nonempty_response(thread.get("id"), "fork Thread id")
        if fork_id == source_thread_id:
            raise InvalidAppServerResponse("thread/fork reused the source Thread id")
        if thread.get("ephemeral") is True or result.get("ephemeral") is True:
            raise InvalidAppServerResponse(
                "thread/fork returned an ephemeral Thread"
            )
        forked_from = thread.get("forkedFromId")
        if forked_from != source_thread_id:
            raise InvalidAppServerResponse("thread/fork returned the wrong source Thread")
        return fork_id

    def archive_thread(self, thread_id: str) -> None:
        target_thread_id = _nonempty(thread_id, "thread_id")
        _mapping(
            self.client.request(
                "thread/archive",
                {"threadId": target_thread_id},
            ),
            "thread/archive result",
        )

    def run_structured_turn(
        self,
        thread_id: str,
        prompt: str,
        output_schema: Mapping[str, object],
        profile: FeasibilityModelProfile,
        cwd: str,
    ) -> AppServerTurnReceipt:
        target_thread_id = _nonempty(thread_id, "thread_id")
        prompt_text = _nonempty(prompt, "prompt")
        if len(prompt_text) > 250_000:
            raise ValueError("prompt exceeds the bounded app-server input size")
        if not isinstance(output_schema, Mapping):
            raise TypeError("output_schema must be an object")
        if not isinstance(profile, FeasibilityModelProfile):
            raise TypeError("profile must be a FeasibilityModelProfile")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise ValueError("cwd must be an absolute path")
        params: dict[str, object] = {
            "threadId": target_thread_id,
            "input": [{"type": "text", "text": prompt_text}],
            "cwd": cwd,
            "approvalPolicy": "never",
            "sandboxPolicy": {
                "type": "readOnly",
                "access": {"type": "fullAccess"},
            },
            "model": profile.model_id,
            "effort": profile.reasoning_effort,
            "outputSchema": dict(output_schema),
        }
        result = _mapping(
            self.client.request("turn/start", params),
            "turn/start result",
        )
        started_turn = _mapping(result.get("turn"), "turn/start Turn")
        generated_turn_id = _nonempty_response(
            started_turn.get("id"), "generated Turn id"
        )
        if started_turn.get("status") not in {"inProgress", "completed"}:
            raise InvalidAppServerResponse("turn/start returned an invalid status")
        completed = self.client.wait_for_notification(
            "turn/completed",
            lambda params: _is_completed_turn_notification(
                params, target_thread_id, generated_turn_id
            ),
            timeout_seconds=self.turn_timeout_seconds,
        )
        completed_turn = _mapping(completed.get("turn"), "turn/completed Turn")
        if completed.get("threadId") != target_thread_id:
            raise InvalidAppServerResponse(
                "turn/completed returned the wrong Thread"
            )
        if completed_turn.get("id") != generated_turn_id:
            raise InvalidAppServerResponse("turn/completed returned the wrong Turn")
        if completed_turn.get("status") != "completed":
            raise StructuredTurnFailed("The structured Turn did not complete")
        structured_output = _structured_output(completed_turn)
        return AppServerTurnReceipt.create(
            thread_id=target_thread_id,
            turn_id=generated_turn_id,
            structured_output=structured_output,
            model_profile_id=profile.profile_id,
        )

    def _discover_models(self) -> list[dict[str, object]]:
        models: list[dict[str, object]] = []
        cursor: str | None = None
        for _ in range(10):
            params: dict[str, object] = {"limit": 100, "includeHidden": True}
            if cursor is not None:
                params["cursor"] = cursor
            result = _mapping(
                self.client.request("model/list", params), "model/list result"
            )
            data = result.get("data")
            if not isinstance(data, list):
                raise InvalidAppServerResponse("model/list data is invalid")
            for value in data:
                model = _mapping(value, "model/list entry")
                _validate_model(model)
                models.append(dict(model))
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor:
                raise InvalidAppServerResponse("model/list cursor is invalid")
            cursor = next_cursor
        else:
            raise InvalidAppServerResponse("model/list exceeded the page limit")
        if not models:
            raise InvalidAppServerResponse("model/list returned no models")
        ids = [value["id"] for value in models]
        if len(set(ids)) != len(ids):
            raise InvalidAppServerResponse("model/list repeated a model id")
        try:
            canonical_json_bytes(models)
        except (TypeError, ValueError):
            raise InvalidAppServerResponse("model/list is not canonical JSON") from None
        return sorted(models, key=lambda value: value["id"])


def connect(
    *,
    database: AgentDatabase,
    host_transport: AppServerTransport | None = None,
    process_factory: Callable[[], AppServerTransport] | None = None,
) -> AppServerGateway:
    """Prefer an explicit host route, otherwise use the one controlled fallback."""

    return AppServerGateway.connect(
        database=database,
        host_transport=host_transport,
        process_factory=process_factory,
    )


def _thread_identity(
    thread: Mapping[str, object], requested_id: str
) -> ThreadIdentity:
    if thread.get("id") != requested_id:
        raise InvalidAppServerResponse("thread/read returned the wrong Thread")
    if "forkedFromId" not in thread:
        raise InvalidAppServerResponse("thread/read omitted forkedFromId")
    try:
        session_tree_id = _bounded_response_string(
            thread.get("sessionId"), "Thread sessionId"
        )
        forked_from = thread.get("forkedFromId")
        if forked_from is not None:
            forked_from = _bounded_response_string(
                forked_from, "Thread forkedFromId"
            )
        cwd = thread.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise ValueError("Thread cwd is invalid")
        ephemeral = thread.get("ephemeral")
        if not isinstance(ephemeral, bool):
            raise ValueError("Thread ephemeral is invalid")
        return ThreadIdentity(
            thread_id=requested_id,
            session_tree_id=session_tree_id,
            forked_from_id=forked_from,
            cwd=os.path.normpath(cwd),
            ephemeral=ephemeral,
        )
    except ValueError as error:
        raise InvalidAppServerResponse(
            "thread/read returned invalid Thread identity"
        ) from error


def _selected_skills(item: Mapping[str, object]) -> tuple[SelectedSkill, ...]:
    content = item.get("content")
    if not isinstance(content, list) or len(content) > 256:
        raise InvalidAppServerResponse("userMessage content is invalid")
    selected: list[SelectedSkill] = []
    for value in content:
        if not isinstance(value, Mapping):
            raise InvalidAppServerResponse("userMessage content item is invalid")
        selection_type = value.get("type")
        if selection_type not in ("skill", "mention"):
            continue
        try:
            name = _bounded_response_string(value.get("name"), "selection name")
            raw_path = value.get("path")
            if (
                not isinstance(raw_path, str)
                or len(raw_path) > 4096
                or "\x00" in raw_path
                or not Path(raw_path).is_absolute()
            ):
                raise ValueError("selection path is invalid")
            selected.append(
                SelectedSkill(
                    selection_type=selection_type,
                    name=name,
                    path=str(Path(raw_path).resolve(strict=False)),
                )
            )
        except ValueError as error:
            raise InvalidAppServerResponse(
                "native selection evidence is invalid"
            ) from error
    return tuple(selected)


def _turn_item_evidence(
    item: Mapping[str, object], item_type: object
) -> TurnItemEvidence:
    item_id = _bounded_response_string(item.get("id"), "Turn item id")
    tool_name = None
    receipt_id = None
    probe_id = None
    if item_type == "hookPrompt":
        receipt_id = _hook_receipt_id(item)
    elif item_type == "mcpToolCall":
        tool_name = _bounded_response_string(item.get("tool"), "MCP tool name")
        receipt_id, probe_id = _mcp_zdecision_ids(item, tool_name)
    try:
        return TurnItemEvidence(
            item_type=item_type,
            item_id=item_id,
            tool_name=tool_name,
            receipt_id=receipt_id,
            probe_id=probe_id,
        )
    except (TypeError, ValueError) as error:
        raise InvalidAppServerResponse("Turn item evidence is invalid") from error


def _hook_receipt_id(item: Mapping[str, object]) -> str | None:
    fragments = item.get("fragments")
    if not isinstance(fragments, list) or len(fragments) > 64:
        raise InvalidAppServerResponse("hookPrompt fragments are invalid")
    receipts: set[str] = set()
    for fragment in fragments:
        if not isinstance(fragment, Mapping):
            raise InvalidAppServerResponse("hookPrompt fragment is invalid")
        text = fragment.get("text")
        if not isinstance(text, str):
            raise InvalidAppServerResponse("hookPrompt text is invalid")
        if len(text) > 2048:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, Mapping) or value.get("marker") not in (
            "ZDECISION_RECALL_RESTORATION",
            "ZDECISION_RECEIPT",
        ):
            continue
        receipts.add(
            _bounded_response_string(
                value.get("receipt_id"), "ZDecision receipt id"
            )
        )
    if len(receipts) > 1:
        raise InvalidAppServerResponse("hookPrompt repeated ZDecision receipts")
    return next(iter(receipts), None)


def _mcp_zdecision_ids(
    item: Mapping[str, object], tool_name: str
) -> tuple[str | None, str | None]:
    if (
        item.get("server") != "zdecision-local"
        or tool_name not in _ZDECISION_RECALL_TOOLS
    ):
        return None, None
    result = item.get("result")
    if not isinstance(result, Mapping):
        return None, None
    structured = result.get("structuredContent")
    if not isinstance(structured, Mapping):
        return None, None
    receipt_id = structured.get("receipt_id")
    if receipt_id is not None:
        receipt_id = _bounded_response_string(
            receipt_id, "ZDecision receipt id"
        )
    probe_id = None
    probe = structured.get("probe")
    if isinstance(probe, Mapping) and probe.get("marker") == (
        "host_gate_fixture_not_formal"
    ):
        probe_id = _bounded_response_string(
            probe.get("probe_id"), "ZDecision probe id"
        )
    return receipt_id, probe_id


def _bounded_response_string(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 256
        or "\x00" in value
    ):
        raise ValueError(f"{field_name} is invalid")
    return value


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidAppServerResponse(f"{field_name} must be an object")
    return value


def _nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _nonempty_response(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidAppServerResponse(f"{field_name} is invalid")
    return value


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise InvalidAppServerResponse(f"{field_name} is invalid")
    return value


def _resolved_cwd(value: object) -> str:
    cwd = _nonempty(value, "cwd")
    return str(Path(cwd).resolve())


def _next_cursor(
    result: Mapping[str, object], seen_cursors: set[str]
) -> str | None:
    next_cursor = result.get("nextCursor")
    if next_cursor is None:
        return None
    if not isinstance(next_cursor, str) or not next_cursor:
        raise InvalidAppServerResponse("thread/list cursor is invalid")
    if next_cursor in seen_cursors:
        raise InvalidAppServerResponse("thread/list repeated a cursor")
    seen_cursors.add(next_cursor)
    return next_cursor


def _validate_model(model: Mapping[str, object]) -> None:
    _nonempty_response(model.get("id"), "model id")
    if not isinstance(model.get("isDefault"), bool):
        raise InvalidAppServerResponse("model isDefault is invalid")
    _nonempty_response(
        model.get("defaultReasoningEffort"), "default reasoning effort"
    )
    efforts = model.get("supportedReasoningEfforts")
    if not isinstance(efforts, list):
        raise InvalidAppServerResponse("supported reasoning efforts are invalid")
    seen: set[str] = set()
    for value in efforts:
        option = _mapping(value, "reasoning effort option")
        effort = _nonempty_response(
            option.get("reasoningEffort"), "supported reasoning effort"
        )
        if effort in seen:
            raise InvalidAppServerResponse("reasoning effort is repeated")
        seen.add(effort)


def _stored_profile(
    stored: StoredFeasibilityModelProfile,
) -> FeasibilityModelProfile:
    return FeasibilityModelProfile(
        profile_id=stored.profile_id,
        model_id=stored.model_id,
        reasoning_effort=stored.reasoning_effort,
        discovery_digest=stored.discovery_digest,
        discovered_at=stored.discovered_at,
    )


def _profile_supported(
    catalog: list[dict[str, object]], profile: FeasibilityModelProfile
) -> bool:
    model = next(
        (value for value in catalog if value["id"] == profile.model_id),
        None,
    )
    if model is None:
        return False
    return profile.reasoning_effort in {
        value["reasoningEffort"]
        for value in model["supportedReasoningEfforts"]
    }


def _select_default_model(
    catalog: list[dict[str, object]],
) -> tuple[str, str]:
    defaults = [value for value in catalog if value["isDefault"] is True]
    if len(defaults) != 1:
        raise InvalidAppServerResponse(
            "model/list must return exactly one default model"
        )
    selected = defaults[0]
    default_effort = selected["defaultReasoningEffort"]
    supported = {
        value["reasoningEffort"]
        for value in selected["supportedReasoningEfforts"]
    }
    if default_effort not in supported:
        raise InvalidAppServerResponse(
            "default reasoning effort is not supported"
        )
    return selected["id"], default_effort


def _select_model(
    catalog: list[dict[str, object]], boundary: SourceBoundary
) -> tuple[str, str]:
    if boundary.model_id is not None and boundary.reasoning_effort is not None:
        source = next(
            (value for value in catalog if value["id"] == boundary.model_id), None
        )
        if source is not None:
            supported = {
                value["reasoningEffort"]
                for value in source["supportedReasoningEfforts"]
            }
            if boundary.reasoning_effort in supported:
                return boundary.model_id, boundary.reasoning_effort
    return _select_default_model(catalog)


def _is_completed_turn_notification(
    params: Mapping[str, object], thread_id: str, turn_id: str
) -> bool:
    if params.get("threadId") != thread_id:
        return False
    turn = params.get("turn")
    return isinstance(turn, Mapping) and turn.get("id") == turn_id


def _structured_output(turn: Mapping[str, object]) -> Mapping[str, object]:
    direct = turn.get("structuredOutput")
    if isinstance(direct, Mapping):
        return dict(direct)
    items = turn.get("items")
    if not isinstance(items, list):
        raise StructuredTurnFailed("The structured Turn returned no items")
    messages = [
        value
        for value in items
        if isinstance(value, Mapping)
        and value.get("type") == "agentMessage"
        and isinstance(value.get("text"), str)
    ]
    if not messages:
        raise StructuredTurnFailed(
            "The structured Turn returned no final agent message"
        )
    try:
        parsed = json.loads(messages[-1]["text"])
    except json.JSONDecodeError:
        raise StructuredTurnFailed(
            "The structured Turn output is not valid JSON"
        ) from None
    if not isinstance(parsed, Mapping):
        raise StructuredTurnFailed(
            "The structured Turn output must be a JSON object"
        )
    return dict(parsed)


def _format_datetime(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
