"""Validated domain gateway over the Codex app-server protocol."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from zdecision.agent.db import (
    AgentDatabase,
    FeasibilityModelProfileConflict,
)
from zdecision.app_server.jsonl import (
    AppServerTransport,
    JsonlAppServerClient,
    ProcessJsonlTransport,
)
from zdecision.app_server.models import (
    AppServerTurnReceipt,
    FeasibilityModelProfile,
    SourceBoundary,
)
from zdecision.jsonio import canonical_json_bytes


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
        if forked_from is not None and forked_from != source_thread_id:
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

    def start_ephemeral_thread(
        self,
        cwd: str,
        profile: FeasibilityModelProfile,
        thread_source: str,
    ) -> str:
        """Compatibility shim until the reconciliation caller migrates."""

        _nonempty(thread_source, "thread_source")
        return self.start_disposable_thread(cwd, profile)

    def fork_ephemeral(
        self,
        thread_id: str,
        last_turn_id: str,
        *,
        thread_source: str | None = None,
    ) -> str:
        """Compatibility shim until the Capture caller migrates."""

        if thread_source is not None:
            _nonempty(thread_source, "thread_source")
        return self.fork_disposable_thread(thread_id, last_turn_id)

    def find_thread_by_source(
        self,
        thread_source: str,
        *,
        cwd: str | None = None,
    ) -> str | None:
        source = _nonempty(thread_source, "thread_source")
        resolved_cwd = None if cwd is None else _resolved_cwd(cwd)
        seen_ids: set[str] = set()
        matches: list[str] = []
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
                    "limit": 100,
                    "sourceKinds": ["appServer"],
                    "archived": archived,
                }
                if resolved_cwd is not None:
                    params["cwd"] = [resolved_cwd]
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
                    returned_source = thread.get("threadSource")
                    if returned_source is not None and not isinstance(
                        returned_source, str
                    ):
                        raise InvalidAppServerResponse(
                            "thread/list Thread source is invalid"
                        )
                    if returned_source == source:
                        matches.append(thread_id)
                cursor = _next_cursor(result, seen_cursors)
                if cursor is None:
                    break
        if len(matches) > 1:
            raise InvalidAppServerResponse(
                "thread/list returned multiple exact Thread source matches"
            )
        return None if not matches else matches[0]

    def run_structured_turn(
        self,
        thread_id: str,
        prompt: str,
        output_schema: Mapping[str, object],
        profile: FeasibilityModelProfile,
        cwd: str,
        *,
        client_user_message_id: str | None = None,
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
        # Kept temporarily in the Python signature for caller migration only.
        # It is deliberately neither sent nor used for result validation.
        if client_user_message_id is not None:
            _nonempty(client_user_message_id, "client_user_message_id")
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

    def read_structured_turn_by_client_id(
        self,
        thread_id: str,
        client_user_message_id: str,
        profile: FeasibilityModelProfile,
    ) -> AppServerTurnReceipt | None:
        target_thread_id = _nonempty(thread_id, "thread_id")
        client_id = _nonempty(
            client_user_message_id, "client_user_message_id"
        )
        if not isinstance(profile, FeasibilityModelProfile):
            raise TypeError("profile must be a FeasibilityModelProfile")
        result = _mapping(
            self.client.request(
                "thread/read",
                {"threadId": target_thread_id, "includeTurns": True},
            ),
            "thread/read result",
        )
        thread = _mapping(result.get("thread"), "thread/read thread")
        if thread.get("id") != target_thread_id:
            raise InvalidAppServerResponse(
                "thread/read returned the wrong Thread"
            )
        turns = thread.get("turns")
        if not isinstance(turns, list):
            raise InvalidAppServerResponse(
                "thread/read did not include Turns"
            )
        matches: list[Mapping[str, object]] = []
        for value in turns:
            turn = _mapping(value, "thread/read Turn")
            items = turn.get("items")
            if not isinstance(items, list):
                raise InvalidAppServerResponse(
                    "thread/read Turn items are invalid"
                )
            count = sum(
                1
                for item in items
                if isinstance(item, Mapping)
                and item.get("type") == "userMessage"
                and item.get("clientId") == client_id
            )
            if count > 1:
                raise InvalidAppServerResponse(
                    "thread/read repeated a client user message id"
                )
            if count == 1:
                matches.append(turn)
        if not matches:
            return None
        if len(matches) != 1:
            raise InvalidAppServerResponse(
                "thread/read repeated a client user message id"
            )
        matched = matches[0]
        if matched.get("status") != "completed":
            return None
        turn_id = _nonempty_response(
            matched.get("id"), "generated Turn id"
        )
        return AppServerTurnReceipt.create(
            thread_id=target_thread_id,
            turn_id=turn_id,
            structured_output=_structured_output(matched),
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
    defaults = [value for value in catalog if value["isDefault"] is True]
    if len(defaults) != 1:
        raise InvalidAppServerResponse(
            "model/list must return exactly one default model"
        )
    return defaults[0]["id"], defaults[0]["defaultReasoningEffort"]


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
