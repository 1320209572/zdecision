"""Route an empty-Git Capture source from its frozen Session context."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from zdecision.agent.capture_routing import SessionRouteDecision
from zdecision.agent.recall_host_state import RecallGateConflict, RecallHostStore
from zdecision.agent.repository_routes import RepositoryRouteSnapshot
from zdecision.agent.session_index import FrozenSessionSource
from zdecision.app_server.gateway import (
    AppServerGatewayError,
    IncompleteSourceTurn,
    InvalidAppServerResponse,
    StructuredTurnOutputInvalid,
    UnknownSourceTurn,
)
from zdecision.app_server.jsonl import AppServerError
from zdecision.app_server.models import FeasibilityModelProfile
from zdecision.jsonio import canonical_json_bytes


class SessionProductRoutingError(Exception):
    """Base class for bounded Session product-routing failures."""


class SessionProductRoutingRetryable(SessionProductRoutingError):
    """The disposable structured model turn can be retried."""


class SessionProductRoutingInvalid(SessionProductRoutingError):
    """The frozen source or structured routing result is invalid."""


class SessionProductRouter:
    """Select one registered route from one exact completed Session."""

    def __init__(
        self,
        *,
        gateway,
        recall_host_store: RecallHostStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(recall_host_store, RecallHostStore):
            raise TypeError("recall_host_store must be a RecallHostStore")
        self.gateway = gateway
        self.recall_host_store = recall_host_store
        self.clock = clock or (lambda: datetime.now(UTC))

    def route(
        self,
        source: FrozenSessionSource,
        snapshot: RepositoryRouteSnapshot,
        profile: FeasibilityModelProfile,
        heartbeat: Callable[[], None] | None = None,
    ) -> SessionRouteDecision:
        if not isinstance(source, FrozenSessionSource):
            raise TypeError("source must be a FrozenSessionSource")
        if not isinstance(snapshot, RepositoryRouteSnapshot):
            raise TypeError("snapshot must be a RepositoryRouteSnapshot")
        if not isinstance(profile, FeasibilityModelProfile):
            raise TypeError("profile must be a FeasibilityModelProfile")
        if source.repository_id != snapshot.repository_id:
            raise SessionProductRoutingInvalid("repository_identity_mismatch")
        routes = tuple(
            sorted(
                (route for route in snapshot.routes if route.enabled),
                key=lambda route: route.route_id,
            )
        )
        if not routes:
            raise SessionProductRoutingInvalid("session_route_invalid")

        try:
            interactive = self.gateway.list_interactive_thread_ids(source.cwd)
            if source.session_id not in interactive:
                raise SessionProductRoutingInvalid("source_not_interactive")
            boundary = self.gateway.read_completed_boundary(
                source.session_id, source.upper_turn_id
            )
        except SessionProductRoutingInvalid:
            raise
        except (
            AppServerError,
            AppServerGatewayError,
            UnknownSourceTurn,
            IncompleteSourceTurn,
            InvalidAppServerResponse,
        ) as error:
            raise SessionProductRoutingInvalid(
                "source_boundary_unavailable"
            ) from error
        if boundary.cwd != source.cwd:
            raise SessionProductRoutingInvalid("source_boundary_unavailable")

        try:
            fork_id = self.gateway.fork_disposable_thread(
                source.session_id, source.upper_turn_id
            )
        except (AppServerError, AppServerGatewayError) as error:
            raise SessionProductRoutingRetryable(
                "session_route_retryable"
            ) from error
        try:
            self.recall_host_store.bind_internal_thread(
                thread_id=fork_id,
                parent_thread_id=source.session_id,
                purpose="capture",
                operation_id=(
                    f"session-routing:{source.request_id}:"
                    f"{source.source_key}:{fork_id}"
                ),
                now=self.clock(),
            )
        except (RecallGateConflict, sqlite3.Error, ValueError) as error:
            self._archive(fork_id)
            raise SessionProductRoutingInvalid(
                "session_route_binding_invalid"
            ) from error

        route_ids = [route.route_id for route in routes]
        schema: dict[str, object] = {
            "type": "object",
            "properties": {
                "route_id": {"type": "string", "enum": route_ids}
            },
            "required": ["route_id"],
            "additionalProperties": False,
        }
        prompt = _routing_prompt(routes)
        try:
            if heartbeat is not None:
                heartbeat()
            receipt = self.gateway.run_structured_turn(
                thread_id=fork_id,
                prompt=prompt,
                output_schema=schema,
                profile=profile,
                cwd=source.cwd,
            )
            if heartbeat is not None:
                heartbeat()
        except (
            InvalidAppServerResponse,
            StructuredTurnOutputInvalid,
        ) as error:
            self._archive(fork_id)
            raise SessionProductRoutingInvalid(
                "session_route_invalid"
            ) from error
        except (AppServerError, AppServerGatewayError) as error:
            self._archive(fork_id)
            raise SessionProductRoutingRetryable(
                "session_route_retryable"
            ) from error

        try:
            output = receipt.structured_output
            if (
                not isinstance(output, Mapping)
                or set(output) != {"route_id"}
                or output.get("route_id") not in route_ids
                or receipt.thread_id != fork_id
                or receipt.model_profile_id != profile.profile_id
                or hashlib.sha256(
                    canonical_json_bytes(dict(output))
                ).hexdigest()
                != receipt.output_sha256
            ):
                raise ValueError("invalid routing receipt")
            return SessionRouteDecision(
                source_key=source.source_key,
                route_id=output["route_id"],
                output_digest=receipt.output_sha256,
            )
        except (TypeError, ValueError) as error:
            raise SessionProductRoutingInvalid("session_route_invalid") from error
        finally:
            self._archive(fork_id)

    def _archive(self, thread_id: str) -> None:
        try:
            self.gateway.archive_thread(thread_id)
        except (AppServerError, AppServerGatewayError):
            pass


def _routing_prompt(routes: tuple[object, ...]) -> str:
    route_records = [
        {
            "route_id": route.route_id,
            "path_prefixes": list(route.path_prefixes),
        }
        for route in routes
    ]
    return (
        "Classify the inherited completed development Session into exactly "
        "one registered leaf route. Judge only from the inherited Session "
        "conversation. Do not call tools, read files, inspect Git, use the "
        "network, or invent a route. Return only the required structured "
        "route_id. Registered routes: "
        + canonical_json_bytes({"routes": route_records}).decode("utf-8")
    )
