from __future__ import annotations

import inspect
import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp.types import CallToolResult

from zdecision.agent import mcp_server
from zdecision.agent.central_client import CentralClientError
from zdecision.agent.control_bindings import ControlBindingStore
from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import RepositorySnapshot, TestRepositoryMapping
from zdecision.agent.mcp_server import LocalMcpTools
from zdecision.agent.service import AgentServiceConfigError
from zdecision.sync.contracts import CaptureRequestCreate, CaptureRequestView


NOW = datetime(2026, 7, 31, 3, 0, tzinfo=UTC)
CONTROL_ID = "ctl_" + "1" * 32
REPOSITORY_ID = "repo_" + "2" * 32
OTHER_REPOSITORY_ID = "repo_" + "8" * 32
PRODUCT_ID = "prod_" + "3" * 32
REQUEST_ID = "crq_" + "4" * 32
WIDGET_URI = "ui://zdecision/update-candidates-v1.html"
WIDGET_MIME_TYPE = "text/html;profile=mcp-app"
CENTRAL_BASE_URL = "http://127.0.0.1:8765"


def request_view(
    *,
    state: str = "queued",
    progress_code: str = "request_queued",
    candidate_revision_count: int | None = None,
    repository_id: str = REPOSITORY_ID,
) -> CaptureRequestView:
    return CaptureRequestView(
        request_id=REQUEST_ID,
        repository_id=repository_id,
        product_id=PRODUCT_ID,
        product_name="ZDecision",
        template_id="business",
        state=state,
        progress_code=progress_code,
        candidate_revision_count=candidate_revision_count,
        last_sequence=1,
        created_at="2026-07-31T03:00:00Z",
        updated_at="2026-07-31T03:00:00Z",
    )


class StaticRepositoryResolver:
    def __init__(self, repository_id: str = REPOSITORY_ID) -> None:
        self.repository_id = repository_id

    def resolve(self, cwd: str) -> RepositorySnapshot:
        return RepositorySnapshot(
            repository_id=self.repository_id,
            worktree_root=cwd,
            branch="main",
            head_commit="a" * 40,
        )


class RecordingCentralClient:
    def __init__(self) -> None:
        self.create_calls: list[CaptureRequestCreate] = []
        self.get_calls: list[str] = []
        self.views_by_action: dict[str, CaptureRequestView] = {}
        self.views_by_request: dict[str, CaptureRequestView] = {}
        self.before_create = None
        self.busy = False
        self.lose_first_response = False
        self.next_view = request_view()

    def create_capture_request(
        self, command: CaptureRequestCreate
    ) -> CaptureRequestView:
        self.create_calls.append(command)
        if self.before_create is not None:
            self.before_create(command)
        if self.busy:
            raise CentralClientError("repository_capture_busy")
        view = self.views_by_action.setdefault(
            command.client_action_id, self.next_view
        )
        self.views_by_request[view.request_id] = view
        if self.lose_first_response:
            self.lose_first_response = False
            raise CentralClientError("central_connection_unavailable")
        return view

    def get_capture_request(self, request_id: str) -> CaptureRequestView:
        self.get_calls.append(request_id)
        return self.views_by_request[request_id]


class McpInlineRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.cwd = str(self.root)
        self.database_path = self.root / "agent" / "zdecision.sqlite3"
        self.database = AgentDatabase.open(self.database_path)
        self.addCleanup(self.database.close)
        self.binding_store = ControlBindingStore.open(self.database_path)
        self.addCleanup(self.binding_store.close)
        self.database.put_test_repository_mapping(
            TestRepositoryMapping(
                repository_id=REPOSITORY_ID,
                product_id=PRODUCT_ID,
                product_name="ZDecision",
                enabled=True,
            )
        )
        self.resolver = StaticRepositoryResolver()
        self.client = RecordingCentralClient()
        self.action_ids = iter(
            ("codex_action_first", "codex_action_second")
        )
        self.create_binding()

    def create_binding(
        self,
        *,
        control_id: str = CONTROL_ID,
        repository_id: str = REPOSITORY_ID,
        created_at: datetime = NOW,
    ):
        return self.binding_store.create_binding(
            session_id="session-private",
            render_turn_id="turn-private",
            cwd=self.cwd,
            repository_id=repository_id,
            product_id=PRODUCT_ID,
            created_at=created_at,
            expires_at=created_at + timedelta(minutes=15),
            control_id=control_id,
        )

    def domain(
        self,
        *,
        now: datetime = NOW,
        client: RecordingCentralClient | None = None,
    ) -> LocalMcpTools:
        required = {
            "binding_store",
            "central_client",
            "central_base_url",
            "clock",
            "action_id_factory",
            "repository_resolver",
        }
        parameters = set(inspect.signature(LocalMcpTools).parameters)
        self.assertTrue(
            required <= parameters,
            "LocalMcpTools must receive refresh dependencies explicitly",
        )
        return LocalMcpTools(
            database=self.database,
            cwd=self.cwd,
            binding_store=self.binding_store,
            central_client=self.client if client is None else client,
            central_base_url=CENTRAL_BASE_URL,
            clock=lambda: now,
            action_id_factory=lambda: next(self.action_ids),
            repository_resolver=self.resolver,
        )

    def basic_domain(self) -> LocalMcpTools:
        return LocalMcpTools(database=self.database, cwd=self.cwd)

    async def test_mcp_contract_registers_only_the_real_card_and_tools(
        self,
    ) -> None:
        server = mcp_server.create_mcp_server(self.basic_domain())

        resources = await server.list_resources()
        tools = {tool.name: tool for tool in await server.list_tools()}

        self.assertEqual([WIDGET_URI], [str(item.uri) for item in resources])
        self.assertEqual(WIDGET_MIME_TYPE, resources[0].mimeType)
        self.assertEqual(
            {"connectDomains": [], "resourceDomains": []},
            resources[0].meta["ui"]["csp"],
        )
        self.assertEqual(
            {
                "zdecision_status",
                "show_zdecision_update",
                "start_zdecision_candidate_refresh",
                "get_zdecision_candidate_refresh",
            },
            set(tools),
        )
        self.assertNotIn("acknowledge_zdecision_update", tools)

        render = tools["show_zdecision_update"]
        self.assertEqual(WIDGET_URI, render.meta["ui"]["resourceUri"])
        self.assertEqual(["model", "app"], render.meta["ui"]["visibility"])
        self.assertTrue(render.annotations.readOnlyHint)
        self.assertFalse(render.annotations.destructiveHint)
        self.assertTrue(render.annotations.idempotentHint)
        self.assertFalse(render.annotations.openWorldHint)

        start = tools["start_zdecision_candidate_refresh"]
        self.assertEqual(["app"], start.meta["ui"]["visibility"])
        self.assertFalse(start.annotations.readOnlyHint)
        self.assertFalse(start.annotations.destructiveHint)
        self.assertTrue(start.annotations.idempotentHint)
        self.assertFalse(start.annotations.openWorldHint)

        status = tools["get_zdecision_candidate_refresh"]
        self.assertEqual(["app"], status.meta["ui"]["visibility"])
        self.assertTrue(status.annotations.readOnlyHint)
        self.assertFalse(status.annotations.destructiveHint)
        self.assertTrue(status.annotations.idempotentHint)
        self.assertFalse(status.annotations.openWorldHint)
        status_meta = tools["zdecision_status"].meta or {}
        self.assertNotIn("resourceUri", status_meta.get("ui", {}))

    async def test_render_keeps_control_private_and_disabled_has_no_reason(
        self,
    ) -> None:
        domain = self.domain()
        server = mcp_server.create_mcp_server(domain)

        valid = await server.call_tool(
            "show_zdecision_update", {"control_id": CONTROL_ID}
        )
        disabled = await server.call_tool("show_zdecision_update", {})

        self.assertIsInstance(valid, CallToolResult)
        self.assertEqual(
            {
                "actions_enabled": True,
                "safe_state": "ready",
            },
            valid.structuredContent,
        )
        self.assertEqual(
            {"zdecision/control_id": CONTROL_ID}, valid.meta
        )
        self.assertNotIn(CONTROL_ID, valid.content[0].text)
        self.assertNotIn(CONTROL_ID, str(valid.structuredContent))
        self.assertEqual(
            {
                "actions_enabled": False,
                "safe_state": "disabled",
            },
            disabled.structuredContent,
        )
        self.assertEqual({}, disabled.meta)
        self.assertNotIn("reason", disabled.model_dump(by_alias=True))

    async def test_invalid_expired_and_cross_repository_controls_are_rejected(
        self,
    ) -> None:
        expired_id = "ctl_" + "5" * 32
        self.create_binding(
            control_id=expired_id,
            created_at=NOW - timedelta(minutes=15),
        )
        domain = self.domain()

        for control_id in ("ctl_" + "9" * 32, expired_id):
            with self.subTest(control_id=control_id):
                render = domain.show_zdecision_update(control_id)
                self.assertEqual("disabled", render.structuredContent["safe_state"])
                self.assertEqual(
                    {
                        "safe_state": "unavailable",
                        "candidate_revision_count": None,
                        "candidate_page_url": None,
                    },
                    domain.start_zdecision_candidate_refresh(
                        control_id, "current_session"
                    ),
                )

        self.resolver.repository_id = OTHER_REPOSITORY_ID
        render = domain.show_zdecision_update(CONTROL_ID)
        self.assertEqual("disabled", render.structuredContent["safe_state"])
        self.assertEqual(
            "unavailable",
            domain.get_zdecision_candidate_refresh(CONTROL_ID)["safe_state"],
        )
        self.assertEqual([], self.client.create_calls)

    async def test_start_persists_scope_and_action_before_network_call(
        self,
    ) -> None:
        domain = self.domain()

        def assert_persisted(command: CaptureRequestCreate) -> None:
            binding = self.binding_store.get(CONTROL_ID)
            self.assertEqual("current_session", binding.chosen_scope)
            self.assertEqual(command.client_action_id, binding.client_action_id)
            self.assertIsNone(binding.central_request_id)

        self.client.before_create = assert_persisted

        result = domain.start_zdecision_candidate_refresh(
            CONTROL_ID, "current_session"
        )

        self.assertEqual("queued", result["safe_state"])
        self.assertEqual(1, len(self.client.create_calls))
        command = self.client.create_calls[0]
        self.assertEqual(
            {
                "repository_id": REPOSITORY_ID,
                "template_id": "business",
                "capture_scope": "current_session",
                "client_action_id": "codex_action_first",
            },
            command.to_dict(),
        )
        self.assertEqual(
            REQUEST_ID,
            self.binding_store.get(CONTROL_ID).central_request_id,
        )

    async def test_same_scope_replay_uses_one_action_and_one_request(self) -> None:
        domain = self.domain()

        first = domain.start_zdecision_candidate_refresh(
            CONTROL_ID, "all_valid_sessions"
        )
        replay = domain.start_zdecision_candidate_refresh(
            CONTROL_ID, "all_valid_sessions"
        )

        self.assertEqual(first, replay)
        self.assertEqual(1, len(self.client.create_calls))
        self.assertEqual([REQUEST_ID], self.client.get_calls)
        binding = self.binding_store.get(CONTROL_ID)
        self.assertEqual("codex_action_first", binding.client_action_id)
        self.assertEqual(REQUEST_ID, binding.central_request_id)

    async def test_scope_conflict_and_busy_never_attach_unrelated_request(
        self,
    ) -> None:
        domain = self.domain()
        first = domain.start_zdecision_candidate_refresh(
            CONTROL_ID, "current_session"
        )
        conflict = domain.start_zdecision_candidate_refresh(
            CONTROL_ID, "all_valid_sessions"
        )

        self.assertEqual("queued", first["safe_state"])
        self.assertEqual("failed", conflict["safe_state"])
        self.assertEqual(1, len(self.client.create_calls))

        busy_control = "ctl_" + "6" * 32
        self.create_binding(control_id=busy_control)
        self.client.busy = True
        busy = domain.start_zdecision_candidate_refresh(
            busy_control, "current_session"
        )
        self.assertEqual(
            {
                "safe_state": "busy",
                "candidate_revision_count": None,
                "candidate_page_url": None,
            },
            busy,
        )
        self.assertIsNone(
            self.binding_store.get(busy_control).central_request_id
        )

    async def test_lost_response_replay_adopts_request_with_same_action(
        self,
    ) -> None:
        domain = self.domain()
        self.client.lose_first_response = True

        lost = domain.start_zdecision_candidate_refresh(
            CONTROL_ID, "current_session"
        )
        replay = domain.start_zdecision_candidate_refresh(
            CONTROL_ID, "current_session"
        )

        self.assertEqual("unavailable", lost["safe_state"])
        self.assertEqual("queued", replay["safe_state"])
        self.assertEqual(2, len(self.client.create_calls))
        self.assertEqual(
            ["codex_action_first", "codex_action_first"],
            [call.client_action_id for call in self.client.create_calls],
        )
        self.assertEqual(
            REQUEST_ID,
            self.binding_store.get(CONTROL_ID).central_request_id,
        )

    async def test_status_maps_only_allowlisted_safe_state_count_and_url(
        self,
    ) -> None:
        domain = self.domain()
        chosen = self.binding_store.choose_scope(
            CONTROL_ID,
            expected_repository_id=REPOSITORY_ID,
            scope="current_session",
            proposed_client_action_id="codex_action_first",
            now=NOW,
        )
        self.binding_store.attach_request(
            CONTROL_ID,
            client_action_id=chosen.client_action_id,
            central_request_id=REQUEST_ID,
        )

        cases = (
            ("queued", "request_queued", None, "queued"),
            ("claimed", "device_claimed", None, "queued"),
            ("running", "capture_started", None, "capturing"),
            ("running", "capturing_sessions", None, "capturing"),
            ("running", "extracting_candidates", None, "capturing"),
            ("running", "reconciling_candidates", None, "capturing"),
            ("running", "uploading_candidates", None, "syncing"),
            ("failed_retryable", "temporary_failure", None, "failed"),
            ("failed_terminal", "capture_failed", None, "failed"),
            ("cancelled", "capture_cancelled", None, "failed"),
            ("succeeded_no_candidates", "capture_succeeded_no_candidates", 0, "empty"),
            ("succeeded", "capture_succeeded", 2, "succeeded"),
        )
        for state, progress, count, safe_state in cases:
            with self.subTest(state=state, progress=progress):
                self.client.views_by_request[REQUEST_ID] = request_view(
                    state=state,
                    progress_code=progress,
                    candidate_revision_count=count,
                )
                result = domain.get_zdecision_candidate_refresh(CONTROL_ID)
                self.assertEqual(
                    {
                        "safe_state",
                        "candidate_revision_count",
                        "candidate_page_url",
                    },
                    set(result),
                )
                self.assertEqual(safe_state, result["safe_state"])
                self.assertEqual(count, result["candidate_revision_count"])
                expected_url = (
                    f"{CENTRAL_BASE_URL}/?repository_id={REPOSITORY_ID}"
                    if safe_state in {"empty", "succeeded"}
                    else None
                )
                self.assertEqual(expected_url, result["candidate_page_url"])
                serialized = str(result).lower()
                for forbidden in (
                    "session",
                    "turn",
                    "cwd",
                    "control",
                    "product",
                    "request_id",
                    "/users/",
                ):
                    self.assertNotIn(forbidden, serialized)

    async def test_widget_uses_portable_bridge_and_contains_no_candidate_payload(
        self,
    ) -> None:
        server = mcp_server.create_mcp_server(self.basic_domain())

        resource_uris = {
            str(resource.uri) for resource in await server.list_resources()
        }
        self.assertIn(WIDGET_URI, resource_uris)
        contents = list(await server.read_resource(WIDGET_URI))

        self.assertEqual(1, len(contents))
        self.assertEqual(WIDGET_MIME_TYPE, contents[0].mime_type)
        html = contents[0].content
        for required in (
            "当前 Session",
            "所有有效 Session",
            '"ui/initialize"',
            '"ui/notifications/initialized"',
            '"ui/notifications/tool-input"',
            '"ui/notifications/tool-result"',
            '"tools/call"',
            '"start_zdecision_candidate_refresh"',
            '"get_zdecision_candidate_refresh"',
            '"ui/open-link"',
            "hostCapabilities?.openLinks",
            "setTimeout(poll, 1500)",
            "正在创建更新请求",
            "等待本地设备",
            "正在整理候选决策",
            "正在同步候选决策",
            "已有更新正在进行",
            "暂时无法更新",
            "本次更新未完成",
            "没有发现新的候选决策",
            "打开候选决策页面",
        ):
            with self.subTest(required=required):
                self.assertIn(required, html)

        lowered = html.lower()
        for forbidden in (
            "window.openai",
            "acknowledge_zdecision_update",
            "ui_probe_v1",
            "能力探针",
            "session_id",
            "turn_id",
            "cwd",
            "/users/",
            "future_action",
            "scope_summary",
            "invalidation_conditions",
            "candidate.content",
            "innerhtml",
            "<link",
            "linear-gradient",
            "radial-gradient",
            "animation:",
            "inter,",
            "arial,",
            "roboto,",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)

        static_files = {
            path.name
            for path in (
                Path(mcp_server.__file__).resolve().parent / "static"
            ).glob("*.html")
        }
        self.assertEqual({"update-candidates-v1.html"}, static_files)

    async def test_widget_ignores_app_tool_result_notifications_after_render(
        self,
    ) -> None:
        html = mcp_server.UPDATE_CANDIDATES_PATH.read_text("utf-8")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        harness = f"""
const vm = require("node:vm");
const shippedScript = {json.dumps(script)};
const outbound = [];
const timers = [];
let messageHandler = null;

class Element {{
  constructor() {{
    this.disabled = false;
    this.hidden = false;
    this.textContent = "";
    this.listeners = new Map();
  }}

  addEventListener(name, listener) {{
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }}

  dispatch(name) {{
    return Promise.all((this.listeners.get(name) || []).map((listener) => listener()));
  }}
}}

const elementIds = ["current", "all", "open-page", "status"];
const elements = Object.fromEntries(elementIds.map((id) => [id, new Element()]));
const host = {{
  postMessage(message) {{ outbound.push(message); }},
}};
global.document = {{ getElementById: (id) => elements[id] }};
global.window = {{
  parent: host,
  addEventListener(name, listener) {{
    if (name === "message") messageHandler = listener;
  }},
}};
global.setTimeout = (callback, delay) => {{
  timers.push({{ callback, delay }});
  return timers.length;
}};

function check(condition, message) {{
  if (!condition) throw new Error(message);
}}

function deliver(message) {{
  messageHandler({{ source: host, data: message }});
}}

function latestToolCall(name) {{
  return [...outbound].reverse().find(
    (message) => message.method === "tools/call" && message.params?.name === name,
  );
}}

vm.runInThisContext(shippedScript);

(async () => {{
  const initialize = outbound.find((message) => message.method === "ui/initialize");
  check(initialize, "widget did not initialize the bridge");
  deliver({{
    jsonrpc: "2.0",
    id: initialize.id,
    result: {{ hostCapabilities: {{ serverTools: {{}} }} }},
  }});
  await Promise.resolve();

  const trustedControl = "ctl_11111111111111111111111111111111";
  deliver({{
    jsonrpc: "2.0",
    method: "ui/notifications/tool-result",
    params: {{
      content: [{{ type: "text", text: "ready" }}],
      structuredContent: {{ actions_enabled: true, safe_state: "ready" }},
      _meta: {{ "zdecision/control_id": trustedControl }},
    }},
  }});
  check(!elements.current.disabled && !elements.all.disabled, "render result did not enable actions");

  const click = elements.current.dispatch("click");
  const start = latestToolCall("start_zdecision_candidate_refresh");
  check(start, "current Session click did not call start");
  check(start.params.arguments.control_id === trustedControl, "start lost trusted control");
  check(elements.current.disabled && elements.all.disabled, "first click did not lock both actions");

  deliver({{
    jsonrpc: "2.0",
    id: start.id,
    result: {{
      content: [],
      structuredContent: {{
        safe_state: "queued",
        candidate_revision_count: null,
        candidate_page_url: null,
      }},
    }},
  }});
  await click;
  check(elements.status.textContent === "等待本地设备", "direct start response was not rendered");
  check(timers.length === 1 && timers[0].delay === 1500, "active start did not schedule one poll");

  deliver({{
    jsonrpc: "2.0",
    method: "ui/notifications/tool-result",
    params: {{
      content: [],
      structuredContent: {{
        safe_state: "queued",
        candidate_revision_count: null,
        candidate_page_url: null,
      }},
    }},
  }});
  check(elements.status.textContent === "等待本地设备", "start notification reset active state");

  const firstTimer = timers.shift();
  const firstPoll = firstTimer.callback();
  const statusCall = latestToolCall("get_zdecision_candidate_refresh");
  check(statusCall, "start notification stopped the next poll");
  check(statusCall.params.arguments.control_id === trustedControl, "poll lost trusted control");

  deliver({{
    jsonrpc: "2.0",
    method: "ui/notifications/tool-result",
    params: {{
      content: [],
      structuredContent: {{
        safe_state: "capturing",
        candidate_revision_count: null,
        candidate_page_url: null,
      }},
    }},
  }});
  check(elements.status.textContent === "等待本地设备", "status notification reset active state");

  deliver({{
    jsonrpc: "2.0",
    id: statusCall.id,
    result: {{
      content: [],
      structuredContent: {{
        safe_state: "capturing",
        candidate_revision_count: null,
        candidate_page_url: null,
      }},
    }},
  }});
  await firstPoll;
  check(elements.status.textContent === "正在整理候选决策", "direct status response was not rendered");
  check(timers.length === 1 && timers[0].delay === 1500, "active status did not continue polling");

  const secondPoll = timers.shift().callback();
  const secondStatusCall = latestToolCall("get_zdecision_candidate_refresh");
  check(secondStatusCall.id !== statusCall.id, "next status poll was not sent");
  check(secondStatusCall.params.arguments.control_id === trustedControl, "next poll lost trusted control");
  deliver({{
    jsonrpc: "2.0",
    id: secondStatusCall.id,
    result: {{
      content: [],
      structuredContent: {{
        safe_state: "failed",
        candidate_revision_count: null,
        candidate_page_url: null,
      }},
    }},
  }});
  await secondPoll;
  check(timers.length === 0, "terminal state scheduled another poll");
  process.stdout.write("bridge-regression-ok");
}})().catch((error) => {{
  process.stderr.write(error.stack || String(error));
  process.exitCode = 1;
}});
"""

        completed = subprocess.run(
            ["node", "-e", harness],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(
            0,
            completed.returncode,
            completed.stderr or completed.stdout,
        )
        self.assertEqual("bridge-regression-ok", completed.stdout)

    async def test_run_mcp_starts_with_disabled_card_when_locator_unavailable(
        self,
    ) -> None:
        parameters = set(inspect.signature(mcp_server.run_mcp).parameters)
        self.assertIn("config_locator_path", parameters)
        observed: dict[str, object] = {}

        class RecordingServer:
            def __init__(self, tools: LocalMcpTools) -> None:
                self.tools = tools

            def run(self, *, transport: str) -> None:
                observed["transport"] = transport
                observed["result"] = self.tools.show_zdecision_update(None)

        unavailable = AgentServiceConfigError("agent_config_locator_invalid")
        with (
            patch(
                "zdecision.agent.mcp_server.load_agent_config_path",
                side_effect=unavailable,
            ),
            patch(
                "zdecision.agent.mcp_server.create_mcp_server",
                side_effect=RecordingServer,
            ),
        ):
            mcp_server.run_mcp(
                database_path=self.root / "runtime.sqlite3",
                config_locator_path=self.root / "missing-locator.json",
                cwd=self.cwd,
            )

        self.assertEqual("stdio", observed["transport"])
        result = observed["result"]
        self.assertEqual(
            {
                "actions_enabled": False,
                "safe_state": "disabled",
            },
            result.structuredContent,
        )

    async def test_run_mcp_starts_with_disabled_card_when_client_unavailable(
        self,
    ) -> None:
        observed: dict[str, object] = {}

        class RecordingServer:
            def __init__(self, tools: LocalMcpTools) -> None:
                self.tools = tools

            def run(self, *, transport: str) -> None:
                observed["transport"] = transport
                observed["result"] = self.tools.show_zdecision_update(None)

        config = SimpleNamespace(
            central_url=CENTRAL_BASE_URL,
            device_token="device-secret-token",
        )
        with (
            patch(
                "zdecision.agent.mcp_server.load_agent_config_path",
                return_value=self.root / "agent.json",
            ),
            patch(
                "zdecision.agent.mcp_server.load_agent_config",
                return_value=config,
            ),
            patch(
                "zdecision.agent.mcp_server.CentralClient",
                side_effect=RuntimeError("client unavailable"),
            ),
            patch(
                "zdecision.agent.mcp_server.create_mcp_server",
                side_effect=RecordingServer,
            ),
        ):
            try:
                mcp_server.run_mcp(
                    database_path=self.root / "runtime-client.sqlite3",
                    config_locator_path=self.root / "locator.json",
                    cwd=self.cwd,
                )
            except RuntimeError as error:
                self.fail(f"MCP must still start: {error}")

        self.assertEqual("stdio", observed["transport"])
        self.assertEqual(
            "disabled", observed["result"].structuredContent["safe_state"]
        )


if __name__ == "__main__":
    unittest.main()
