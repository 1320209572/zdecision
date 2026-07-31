"""Local MCP tools exposed by the ZDecision Plugin."""

from __future__ import annotations

import os
from pathlib import Path

from zdecision.agent.db import AgentDatabase
from zdecision.agent.repository import RepositoryResolver


class LocalMcpTools:
    """Testable domain methods behind the stdio MCP adapter."""

    def __init__(
        self,
        *,
        database: AgentDatabase,
        cwd: str,
    ) -> None:
        self.database = database
        self.cwd = os.path.normpath(cwd)

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
    )
    server = FastMCP("zdecision-local")

    @server.tool()
    def zdecision_status() -> dict[str, object]:
        """Return local registration, event, and Session-binding status."""

        return tools.zdecision_status()

    try:
        server.run(transport="stdio")
    finally:
        database.close()
