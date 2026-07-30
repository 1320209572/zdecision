"""Local MCP tools exposed by the ZDecision Plugin."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import (
    RepositorySnapshot,
    VALIDATION_STATES,
    WORK_STATES,
    local_fact_invocation,
)
from zdecision.agent.repository import RepositoryResolver


class LocalMcpTools:
    """Testable domain methods behind the stdio MCP adapter."""

    def __init__(
        self,
        *,
        database: AgentDatabase,
        cwd: str,
        clock: Callable[[], datetime | str],
    ) -> None:
        self.database = database
        self.cwd = os.path.normpath(cwd)
        self.clock = clock

    def report_work_state(
        self,
        *,
        status: str,
        validation: str,
        unresolved_blockers: list[str],
    ) -> dict[str, object]:
        if status not in WORK_STATES or validation not in VALIDATION_STATES:
            return {"ok": False, "error": "invalid_work_state"}
        if (
            not isinstance(unresolved_blockers, list)
            or len(unresolved_blockers) > 100
            or not all(isinstance(value, str) for value in unresolved_blockers)
        ):
            return {"ok": False, "error": "invalid_work_state"}
        binding = self._binding()
        if binding is None:
            return {"ok": False, "error": "session_binding_ambiguous"}
        session_id, turn_id, repository = binding
        invocation = local_fact_invocation(
            session_id=session_id,
            turn_id=turn_id,
            cwd=self.cwd,
            occurred_at=_format_time(self.clock()),
            repository=repository,
            fact_kind="work_state",
            status=status,
            validation=validation,
            unresolved_blocker_count=len(unresolved_blockers),
        )
        self.database.record_hook(invocation)
        return {"ok": True, "session_id": session_id, "turn_id": turn_id}

    def zdecision_status(self) -> dict[str, object]:
        snapshot = RepositoryResolver().resolve(self.cwd)
        mapping = (
            None
            if snapshot is None
            else self.database.get_repository_mapping(snapshot.repository_id)
        )
        boundary = self.database.latest_open_boundary(self.cwd)
        return {
            "repository_registered": mapping is not None,
            "repository_enabled": bool(mapping is not None and mapping.enabled),
            "event_count": self.database.count_events(cwd=self.cwd),
            "active_session_bound": boundary is not None,
        }

    def submit_current_boundary(self) -> dict[str, object]:
        binding = self._binding()
        if binding is None:
            return {"ok": False, "error": "session_binding_ambiguous"}
        session_id, turn_id, repository = binding
        invocation = local_fact_invocation(
            session_id=session_id,
            turn_id=turn_id,
            cwd=self.cwd,
            occurred_at=_format_time(self.clock()),
            repository=repository,
            fact_kind="manual_submit",
        )
        self.database.record_hook(invocation)
        return {"ok": True, "session_id": session_id, "turn_id": turn_id}

    def _binding(self) -> tuple[str, str, RepositorySnapshot | None] | None:
        event = self.database.latest_turn_event(self.cwd)
        if event is None or event.invocation.turn_id is None:
            return None
        invocation = event.invocation
        repository = None
        if (
            invocation.repository_id is not None
            and invocation.worktree_root is not None
            and invocation.head_commit is not None
        ):
            repository = RepositorySnapshot(
                repository_id=invocation.repository_id,
                worktree_root=invocation.worktree_root,
                branch=invocation.branch,
                head_commit=invocation.head_commit,
            )
        return invocation.session_id, invocation.turn_id, repository


def run_mcp(
    *,
    database_path: Path,
    cwd: str,
) -> None:
    """Start the MCP SDK only for the explicit `mcp` subcommand."""

    from mcp.server.fastmcp import FastMCP

    database = AgentDatabase.open(database_path)
    tools = LocalMcpTools(
        database=database,
        cwd=cwd,
        clock=lambda: datetime.now(UTC),
    )
    server = FastMCP("zdecision-local")

    @server.tool()
    def report_work_state(
        status: Literal[
            "exploring",
            "implementing",
            "awaiting_user",
            "validation_failed",
            "milestone_complete",
        ],
        validation: Literal["passed", "failed", "not_applicable", "unknown"],
        unresolved_blockers: list[str],
    ) -> dict[str, object]:
        """Record a bounded work-state fact for the current local Turn."""

        return tools.report_work_state(
            status=status,
            validation=validation,
            unresolved_blockers=unresolved_blockers,
        )

    @server.tool()
    def zdecision_status() -> dict[str, object]:
        """Return local registration, event, and Session-binding status."""

        return tools.zdecision_status()

    @server.tool()
    def submit_current_boundary() -> dict[str, object]:
        """Record the explicit manual assessment trigger for this Turn."""

        return tools.submit_current_boundary()

    try:
        server.run(transport="stdio")
    finally:
        database.close()


def _format_time(value: datetime | str) -> str:
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        raise ValueError("clock datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
