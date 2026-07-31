from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from zdecision.agent import mcp_server
from zdecision.agent.db import AgentDatabase
from zdecision.agent.mcp_server import LocalMcpTools


WIDGET_URI = "ui://zdecision/update-probe-v1.html"
WIDGET_MIME_TYPE = "text/html;profile=mcp-app"


class McpUiProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.database = AgentDatabase.open(root / "state.sqlite3")
        self.addCleanup(self.database.close)
        self.cwd = str(root)

    def _server(self):
        factory = getattr(mcp_server, "create_mcp_server", None)
        self.assertIsNotNone(
            factory,
            "create_mcp_server must register the MCP Apps probe contract",
        )
        return factory(
            LocalMcpTools(database=self.database, cwd=self.cwd)
        )

    async def test_render_tool_links_to_versioned_widget_resource(self) -> None:
        server = self._server()

        resources = await server.list_resources()
        tools = {tool.name: tool for tool in await server.list_tools()}

        self.assertEqual([WIDGET_URI], [str(item.uri) for item in resources])
        self.assertEqual(WIDGET_MIME_TYPE, resources[0].mimeType)
        self.assertEqual(
            {"connectDomains": [], "resourceDomains": []},
            resources[0].meta["ui"]["csp"],
        )
        render_tool = tools["show_zdecision_update"]
        self.assertEqual(
            WIDGET_URI, render_tool.meta["ui"]["resourceUri"]
        )
        self.assertTrue(render_tool.annotations.readOnlyHint)
        self.assertFalse(render_tool.annotations.openWorldHint)

    async def test_widget_uses_only_the_portable_mcp_apps_bridge(self) -> None:
        server = self._server()

        contents = list(await server.read_resource(WIDGET_URI))

        self.assertEqual(1, len(contents))
        self.assertEqual(WIDGET_MIME_TYPE, contents[0].mime_type)
        html = contents[0].content
        self.assertIn("更新候选决策", html)
        self.assertIn('"ui/initialize"', html)
        self.assertIn('"ui/notifications/initialized"', html)
        self.assertIn('"tools/call"', html)
        self.assertIn('"acknowledge_zdecision_update"', html)
        self.assertNotIn("window.openai", html)

    async def test_probe_action_is_deterministic_and_changes_no_agent_state(
        self,
    ) -> None:
        server = self._server()
        before = self.database.count_events(cwd=self.cwd)

        first = await server.call_tool(
            "acknowledge_zdecision_update",
            {"action_id": "ui_probe_v1"},
        )
        second = await server.call_tool(
            "acknowledge_zdecision_update",
            {"action_id": "ui_probe_v1"},
        )

        self.assertEqual(first, second)
        self.assertEqual(0, before)
        self.assertEqual(before, self.database.count_events(cwd=self.cwd))
        structured = first[1]
        self.assertEqual(
            {
                "action_id": "ui_probe_v1",
                "probe_acknowledged": True,
                "side_effects": "none",
            },
            structured,
        )


if __name__ == "__main__":
    unittest.main()
