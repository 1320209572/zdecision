"""Disposable MCP Apps server for one Codex Desktop capability probe."""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import ConfigDict

from zdecision.agent.host_capability_probe import (
    HostCapabilityProbe,
    HostCapabilityProbeStore,
)


HOST_PROBE_URI = "ui://zdecision/recall-host-capability-probe-v1.html"
HOST_PROBE_MIME_TYPE = "text/html;profile=mcp-app"
HOST_PROBE_PATH = (
    Path(__file__).resolve().parent
    / "static"
    / "recall-host-capability-probe-v1.html"
)


def create_host_probe_mcp_server(store: HostCapabilityProbeStore) -> FastMCP:
    """Create the isolated diagnostic server and no production tools."""

    server = FastMCP("zdecision-host-probe")
    render_annotations = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
    action_annotations = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    recovery_annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @server.resource(
        HOST_PROBE_URI,
        name="zdecision-recall-host-capability-probe",
        title="ZDecision Recall host capability probe",
        description="Disposable MCP Apps capability verification card.",
        mime_type=HOST_PROBE_MIME_TYPE,
        meta={
            "ui": {
                "prefersBorder": True,
                "csp": {
                    "connectDomains": [],
                    "resourceDomains": [],
                },
            },
            "openai/widgetDescription": (
                "A disposable local host-capability verification card."
            ),
        },
    )
    def host_probe_resource() -> str:
        return HOST_PROBE_PATH.read_text("utf-8")

    @server.tool(
        title="Show ZDecision Recall host probe",
        description="Display the disposable MCP Apps host capability probe.",
        annotations=render_annotations,
        meta={
            "ui": {
                "resourceUri": HOST_PROBE_URI,
                "visibility": ["model", "app"],
            },
            "openai/outputTemplate": HOST_PROBE_URI,
            "openai/toolInvocation/invoking": "Opening host capability probe…",
            "openai/toolInvocation/invoked": "Host capability probe ready",
        },
    )
    def show_zdecision_recall_host_probe() -> CallToolResult:
        return _probe_result(store.create())

    @server.tool(
        title="Run ZDecision Recall host probe",
        description="Commit the one-time action selected in the probe app.",
        annotations=action_annotations,
        meta={"ui": {"visibility": ["app"]}},
    )
    def run_zdecision_recall_host_probe(probe_id: str) -> CallToolResult:
        probe = store.commit(probe_id)
        return _probe_result(probe)

    @server.tool(
        title="Get ZDecision Recall host probe",
        description="Read the authoritative state for probe remount recovery.",
        annotations=recovery_annotations,
        meta={"ui": {"visibility": ["app"]}},
    )
    def get_zdecision_recall_host_probe(probe_id: str) -> CallToolResult:
        probe = store.get(probe_id)
        return _probe_result(probe)

    for tool_name in (
        "show_zdecision_recall_host_probe",
        "run_zdecision_recall_host_probe",
        "get_zdecision_recall_host_probe",
    ):
        _forbid_extra_tool_input(server, tool_name)
    return server


def run_host_probe_mcp(*, database_path: Path) -> None:
    """Serve the disposable probe over stdio using only its private store."""

    store = HostCapabilityProbeStore.open(database_path)
    try:
        create_host_probe_mcp_server(store).run(transport="stdio")
    finally:
        store.close()


def _probe_result(probe: HostCapabilityProbe | None) -> CallToolResult:
    if probe is None or probe.state in ("failed", "expired"):
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text="ZDecision host capability probe is unavailable.",
                )
            ],
            structuredContent={
                "probe_version": 1,
                "state": "failed",
                "code": "invalid_probe",
            },
            _meta={},
            isError=True,
        )
    if probe.state == "ready":
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text="ZDecision host capability probe is ready.",
                )
            ],
            structuredContent={"probe_version": 1, "state": "ready"},
            _meta={"zdecision/probe_id": probe.probe_id},
        )
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text="ZDecision host capability probe state is committed.",
            )
        ],
        structuredContent={
            "probe_version": 1,
            "state": "committed",
            "receipt": probe.receipt,
            "committed_at": probe.committed_at,
        },
        _meta={
            "zdecision/probe_id": probe.probe_id,
            "zdecision/probe_marker": probe.marker,
        },
    )


def _forbid_extra_tool_input(server: FastMCP, tool_name: str) -> None:
    tool = server._tool_manager.get_tool(tool_name)
    if tool is None:  # pragma: no cover - registration invariant
        raise RuntimeError("host probe MCP tool registration failed")
    argument_model = tool.fn_metadata.arg_model
    argument_model.model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
    )
    argument_model.model_rebuild(force=True)
    tool.parameters = argument_model.model_json_schema(by_alias=True)
