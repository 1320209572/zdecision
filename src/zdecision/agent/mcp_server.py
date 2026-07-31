"""Local MCP tools exposed by the ZDecision Plugin."""

from __future__ import annotations

import os
from pathlib import Path

from zdecision.agent.db import AgentDatabase
from zdecision.agent.repository import RepositoryResolver


UPDATE_PROBE_URI = "ui://zdecision/update-probe-v1.html"
UPDATE_PROBE_MIME_TYPE = "text/html;profile=mcp-app"
UPDATE_PROBE_PATH = (
    Path(__file__).resolve().parent / "static" / "update-probe-v1.html"
)


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


def create_mcp_server(tools: LocalMcpTools):
    """Create the local server, including the isolated UI capability probe."""

    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP("zdecision-local")
    read_only = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @server.resource(
        UPDATE_PROBE_URI,
        name="zdecision-update-probe",
        title="ZDecision update control probe",
        description="One-button MCP Apps host capability probe.",
        mime_type=UPDATE_PROBE_MIME_TYPE,
        meta={
            "ui": {
                "prefersBorder": True,
                "csp": {
                    "connectDomains": [],
                    "resourceDomains": [],
                },
            },
            "openai/widgetDescription": (
                "A one-button ZDecision UI capability probe with no domain "
                "side effects."
            ),
        },
    )
    def update_probe_resource() -> str:
        return UPDATE_PROBE_PATH.read_text("utf-8")

    @server.tool(annotations=read_only)
    def zdecision_status() -> dict[str, object]:
        """Return local registration, event, and Session-binding status."""

        return tools.zdecision_status()

    @server.tool(
        title="Show ZDecision update control",
        description=(
            "Use this when the user asks to display or test the ZDecision "
            "Update Candidates control inside the conversation."
        ),
        annotations=read_only,
        meta={
            "ui": {
                "resourceUri": UPDATE_PROBE_URI,
                "visibility": ["model", "app"],
            },
            "openai/outputTemplate": UPDATE_PROBE_URI,
            "openai/toolInvocation/invoking": "Opening ZDecision control…",
            "openai/toolInvocation/invoked": "ZDecision control ready",
        },
    )
    def show_zdecision_update() -> dict[str, object]:
        return {
            "button_label": "更新候选决策",
            "probe_only": True,
            "state": "ready",
        }

    @server.tool(
        title="Acknowledge ZDecision update probe",
        description=(
            "Use only from the ZDecision UI capability probe to verify that "
            "the host can call an MCP tool from the widget."
        ),
        annotations=read_only,
        meta={"ui": {"visibility": ["app"]}},
    )
    def acknowledge_zdecision_update(
        action_id: str,
    ) -> dict[str, object]:
        return {
            "action_id": action_id,
            "probe_acknowledged": True,
            "side_effects": "none",
        }

    return server


def run_mcp(
    *,
    database_path: Path,
    cwd: str,
) -> None:
    """Start the MCP SDK only for the explicit `mcp` subcommand."""

    database = AgentDatabase.open(database_path)
    tools = LocalMcpTools(
        database=database,
        cwd=cwd,
    )
    server = create_mcp_server(tools)

    try:
        server.run(transport="stdio")
    finally:
        database.close()
