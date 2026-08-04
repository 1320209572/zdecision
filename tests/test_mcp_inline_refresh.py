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
WIDGET_URI = "ui://zdecision/update-candidates-v3.html"
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
        self.repository_ids_by_cwd: dict[str, str | None] = {}
        self.available = True

    def resolve(self, cwd: str) -> RepositorySnapshot | None:
        if not self.available:
            return None
        repository_id = self.repository_ids_by_cwd.get(cwd, self.repository_id)
        if repository_id is None:
            return None
        return RepositorySnapshot(
            repository_id=repository_id,
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
        self.create_error: Exception | None = None
        self.lose_first_response = False
        self.get_error: Exception | None = None
        self.next_view = request_view()

    def create_capture_request(
        self, command: CaptureRequestCreate
    ) -> CaptureRequestView:
        self.create_calls.append(command)
        if self.before_create is not None:
            self.before_create(command)
        if self.busy:
            raise CentralClientError("repository_capture_busy")
        if self.create_error is not None:
            raise self.create_error
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
        if self.get_error is not None:
            raise self.get_error
        return self.views_by_request[request_id]


class RecordingBrowserLauncher:
    def __init__(self, *, accepted: bool = True) -> None:
        self.accepted = accepted
        self.urls: list[str] = []

    def open(self, url: str) -> bool:
        self.urls.append(url)
        return self.accepted


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
        self.browser_launcher = RecordingBrowserLauncher()
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
        central_base_url: str = CENTRAL_BASE_URL,
        cwd: str | None = None,
    ) -> LocalMcpTools:
        required = {
            "binding_store",
            "central_client",
            "central_base_url",
            "clock",
            "action_id_factory",
            "browser_launcher",
            "repository_resolver",
        }
        parameters = set(inspect.signature(LocalMcpTools).parameters)
        self.assertTrue(
            required <= parameters,
            "LocalMcpTools must receive refresh dependencies explicitly",
        )
        return LocalMcpTools(
            database=self.database,
            cwd=self.cwd if cwd is None else cwd,
            binding_store=self.binding_store,
            central_client=self.client if client is None else client,
            central_base_url=central_base_url,
            clock=lambda: now,
            action_id_factory=lambda: next(self.action_ids),
            browser_launcher=self.browser_launcher,
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
                "open_zdecision_dashboard",
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

        open_dashboard = tools["open_zdecision_dashboard"]
        self.assertEqual(["app"], open_dashboard.meta["ui"]["visibility"])
        self.assertFalse(open_dashboard.annotations.readOnlyHint)
        self.assertFalse(open_dashboard.annotations.destructiveHint)
        self.assertFalse(open_dashboard.annotations.idempotentHint)
        self.assertTrue(open_dashboard.annotations.openWorldHint)
        status_meta = tools["zdecision_status"].meta or {}
        self.assertNotIn("resourceUri", status_meta.get("ui", {}))

    async def test_dashboard_launch_uses_only_bound_repository_url(
        self,
    ) -> None:
        domain = self.domain()
        domain.start_zdecision_candidate_refresh(
            CONTROL_ID, "current_session"
        )

        result = domain.open_zdecision_dashboard(CONTROL_ID)

        expected = (
            "http://127.0.0.1:8765/"
            "?repository_id=repo_22222222222222222222222222222222"
        )
        self.assertEqual(
            {"safe_state": "launch_requested", "dashboard_url": expected},
            result,
        )
        self.assertEqual([expected], self.browser_launcher.urls)

    async def test_dashboard_launch_rejects_unattached_or_invalid_controls(
        self,
    ) -> None:
        domain = self.domain()

        for control_id in (CONTROL_ID, "ctl_" + "9" * 32):
            with self.subTest(control_id=control_id):
                self.assertEqual(
                    {"safe_state": "unavailable", "dashboard_url": None},
                    domain.open_zdecision_dashboard(control_id),
                )

        self.assertEqual([], self.browser_launcher.urls)

    async def test_dashboard_launch_rechecks_repository_mapping(self) -> None:
        domain = self.domain()
        domain.start_zdecision_candidate_refresh(
            CONTROL_ID, "current_session"
        )
        self.database.put_test_repository_mapping(
            TestRepositoryMapping(
                repository_id=REPOSITORY_ID,
                product_id=PRODUCT_ID,
                product_name="ZDecision",
                enabled=False,
            )
        )

        self.assertEqual(
            {"safe_state": "unavailable", "dashboard_url": None},
            domain.open_zdecision_dashboard(CONTROL_ID),
        )
        self.assertEqual([], self.browser_launcher.urls)

    async def test_dashboard_launch_exposes_fallback_when_launcher_rejects(
        self,
    ) -> None:
        self.browser_launcher.accepted = False
        domain = self.domain()
        domain.start_zdecision_candidate_refresh(
            CONTROL_ID, "current_session"
        )

        result = domain.open_zdecision_dashboard(CONTROL_ID)

        self.assertEqual("unavailable", result["safe_state"])
        self.assertEqual(
            "http://127.0.0.1:8765/"
            "?repository_id=repo_22222222222222222222222222222222",
            result["dashboard_url"],
        )
        self.assertEqual(
            [result["dashboard_url"]], self.browser_launcher.urls
        )

    async def test_dashboard_launch_derives_https_and_rejects_invalid_base(
        self,
    ) -> None:
        domain = self.domain(
            central_base_url="https://decisions.example.test"
        )
        domain.start_zdecision_candidate_refresh(
            CONTROL_ID, "current_session"
        )

        result = domain.open_zdecision_dashboard(CONTROL_ID)

        self.assertEqual(
            "https://decisions.example.test/"
            "?repository_id=repo_22222222222222222222222222222222",
            result["dashboard_url"],
        )
        self.assertEqual(
            [result["dashboard_url"]], self.browser_launcher.urls
        )

        invalid = self.domain(
            central_base_url="https://user:pass@example.test"
        )
        self.assertEqual(
            {"safe_state": "unavailable", "dashboard_url": None},
            invalid.open_zdecision_dashboard(CONTROL_ID),
        )
        self.assertEqual(1, len(self.browser_launcher.urls))

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

    async def test_invalid_expired_and_bound_repository_mismatch_are_rejected(
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
                "submission_state": "busy",
                "chosen_scope": "current_session",
            },
            busy,
        )
        self.assertIsNone(
            self.binding_store.get(busy_control).central_request_id
        )

    async def test_busy_and_permanent_rejections_never_replay(self) -> None:
        domain = self.domain()
        self.client.busy = True

        busy = domain.start_zdecision_candidate_refresh(
            CONTROL_ID, "current_session"
        )
        self.client.busy = False
        busy_replay = domain.start_zdecision_candidate_refresh(
            CONTROL_ID, "current_session"
        )
        busy_status = domain.get_zdecision_candidate_refresh(CONTROL_ID)

        self.assertEqual("busy", busy["safe_state"])
        self.assertEqual(busy, busy_replay)
        self.assertEqual(busy, busy_status)
        self.assertEqual(1, len(self.client.create_calls))
        self.assertEqual(
            "busy", self.binding_store.get(CONTROL_ID).submission_state
        )

        rejected_control = "ctl_" + "7" * 32
        self.create_binding(control_id=rejected_control)
        self.client.create_error = CentralClientError("central_request_rejected")
        rejected = domain.start_zdecision_candidate_refresh(
            rejected_control, "all_valid_sessions"
        )
        self.client.create_error = None
        rejected_replay = domain.start_zdecision_candidate_refresh(
            rejected_control, "all_valid_sessions"
        )
        rejected_status = domain.get_zdecision_candidate_refresh(
            rejected_control
        )

        self.assertEqual("failed", rejected["safe_state"])
        self.assertEqual("rejected", rejected["submission_state"])
        self.assertEqual(rejected, rejected_replay)
        self.assertEqual(rejected, rejected_status)
        self.assertEqual(2, len(self.client.create_calls))
        self.assertEqual(
            "rejected",
            self.binding_store.get(rejected_control).submission_state,
        )

    async def test_lost_response_stays_submitting_until_same_action_is_adopted(
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

        self.assertEqual("submitting", lost["safe_state"])
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

    async def test_pending_submission_status_tool_remains_read_only(self) -> None:
        domain = self.domain()
        self.client.lose_first_response = True

        started = domain.start_zdecision_candidate_refresh(
            CONTROL_ID, "current_session"
        )
        status = domain.get_zdecision_candidate_refresh(CONTROL_ID)

        self.assertEqual("submitting", started["safe_state"])
        self.assertEqual("pending", started["submission_state"])
        self.assertEqual("current_session", started["chosen_scope"])
        self.assertEqual("submitting", status["safe_state"])
        self.assertEqual("pending", status["submission_state"])
        self.assertEqual("current_session", status["chosen_scope"])
        self.assertEqual(1, len(self.client.create_calls))
        self.assertEqual([], self.client.get_calls)
        binding = self.binding_store.get(CONTROL_ID)
        self.assertEqual("current_session", binding.chosen_scope)
        self.assertEqual("codex_action_first", binding.client_action_id)
        self.assertIsNone(binding.central_request_id)

    async def test_status_restores_ready_binding_without_mutation(self) -> None:
        status = self.domain().get_zdecision_candidate_refresh(CONTROL_ID)

        self.assertEqual(
            {
                "safe_state": "ready",
                "candidate_revision_count": None,
                "candidate_page_url": None,
                "submission_state": "ready",
                "chosen_scope": None,
            },
            status,
        )
        self.assertEqual([], self.client.create_calls)
        self.assertEqual([], self.client.get_calls)

    async def test_app_status_restores_binding_across_mcp_working_directories(
        self,
    ) -> None:
        another_cwd = self.root / "another-mcp-process"
        another_cwd.mkdir()
        self.resolver.repository_ids_by_cwd[str(another_cwd)] = (
            OTHER_REPOSITORY_ID
        )

        another_domain = self.domain(cwd=str(another_cwd))
        render = another_domain.show_zdecision_update(CONTROL_ID)
        status = another_domain.get_zdecision_candidate_refresh(CONTROL_ID)

        self.assertEqual("disabled", render.structuredContent["safe_state"])
        self.assertEqual(
            {
                "safe_state": "ready",
                "candidate_revision_count": None,
                "candidate_page_url": None,
                "submission_state": "ready",
                "chosen_scope": None,
            },
            status,
        )
        self.assertEqual([], self.client.create_calls)
        self.assertEqual([], self.client.get_calls)

    async def test_app_start_uses_binding_across_mcp_working_directories(
        self,
    ) -> None:
        another_cwd = self.root / "another-mcp-process"
        another_cwd.mkdir()
        self.resolver.repository_ids_by_cwd[str(another_cwd)] = (
            OTHER_REPOSITORY_ID
        )

        result = self.domain(cwd=str(another_cwd)).start_zdecision_candidate_refresh(
            CONTROL_ID, "current_session"
        )

        self.assertEqual("queued", result["safe_state"])
        self.assertEqual("current_session", result["chosen_scope"])
        self.assertEqual(1, len(self.client.create_calls))
        self.assertEqual(REPOSITORY_ID, self.client.create_calls[0].repository_id)

    async def test_selected_request_status_uses_frozen_binding_without_git(
        self,
    ) -> None:
        domain = self.domain()
        started = domain.start_zdecision_candidate_refresh(
            CONTROL_ID, "current_session"
        )
        self.resolver.available = False

        status = domain.get_zdecision_candidate_refresh(CONTROL_ID)

        self.assertEqual("queued", started["safe_state"])
        self.assertEqual("queued", status["safe_state"])

    async def test_app_status_uses_selected_binding_across_mcp_directories(
        self,
    ) -> None:
        domain = self.domain()
        domain.start_zdecision_candidate_refresh(CONTROL_ID, "current_session")
        another_cwd = self.root / "another-repository"
        another_cwd.mkdir()

        status = self.domain(cwd=str(another_cwd)).get_zdecision_candidate_refresh(
            CONTROL_ID
        )

        self.assertEqual("queued", status["safe_state"])

    async def test_transient_central_status_failure_is_retryable(self) -> None:
        domain = self.domain()
        domain.start_zdecision_candidate_refresh(CONTROL_ID, "current_session")
        self.client.get_error = CentralClientError("central_connection_unavailable")

        status = domain.get_zdecision_candidate_refresh(CONTROL_ID)

        self.assertEqual(
            {
                "safe_state": "retrying",
                "candidate_revision_count": None,
                "candidate_page_url": None,
                "submission_state": "attached",
                "chosen_scope": "current_session",
            },
            status,
        )

        self.client.get_error = CentralClientError("central_request_rejected")
        self.assertEqual(
            "unavailable",
            domain.get_zdecision_candidate_refresh(CONTROL_ID)["safe_state"],
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
                        "submission_state",
                        "chosen_scope",
                    },
                    set(result),
                )
                self.assertEqual(safe_state, result["safe_state"])
                self.assertEqual("attached", result["submission_state"])
                self.assertEqual("current_session", result["chosen_scope"])
                self.assertEqual(count, result["candidate_revision_count"])
                expected_url = (
                    f"{CENTRAL_BASE_URL}/?repository_id={REPOSITORY_ID}"
                    if safe_state in {"empty", "succeeded"}
                    else None
                )
                self.assertEqual(expected_url, result["candidate_page_url"])
                serialized = str(result).lower()
                for forbidden in (
                    "session-private",
                    "turn-private",
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
            'name: "open_zdecision_dashboard"',
            "setTimeout(poll, 1500)",
            "正在创建更新请求",
            "等待本地设备",
            "正在整理候选决策",
            "正在同步候选决策",
            "已有更新正在进行",
            "暂时无法更新",
            "本次更新未完成",
            "没有发现新的候选决策",
            "打开决策中心",
        ):
            with self.subTest(required=required):
                self.assertIn(required, html)

        lowered = html.lower()
        for forbidden in (
            "window.openai",
            "window.open(",
            '"ui/open-link"',
            'target="_blank"',
            'rel="noopener noreferrer"',
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

const elementIds = ["current", "all", "open-page", "status", "card-state"];
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
  const restoration = latestToolCall("get_zdecision_candidate_refresh");
  check(restoration, "render result did not start read-only restoration");
  deliver({{
    jsonrpc: "2.0",
    id: restoration.id,
    result: {{
      content: [],
      structuredContent: {{
        safe_state: "ready",
        candidate_revision_count: null,
        candidate_page_url: null,
        submission_state: "ready",
        chosen_scope: null,
      }},
    }},
  }});
  await Promise.resolve();
  check(!elements.current.disabled && !elements.all.disabled, "restoration did not enable actions");

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
        submission_state: "attached",
        chosen_scope: "current_session",
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
        submission_state: "attached",
        chosen_scope: "current_session",
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
        submission_state: "attached",
        chosen_scope: "current_session",
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
        safe_state: "retrying",
        candidate_revision_count: null,
        candidate_page_url: null,
        submission_state: "attached",
        chosen_scope: "current_session",
      }},
    }},
  }});
  await secondPoll;
  check(elements.status.textContent === "连接暂时中断，正在重试", "transient status failure was not reported");
  check(timers.length === 1 && timers[0].delay === 1500, "transient status failure stopped polling");

  const finalPoll = timers.shift().callback();
  const finalStatusCall = latestToolCall("get_zdecision_candidate_refresh");
  check(finalStatusCall.id !== secondStatusCall.id, "retry did not request fresh status");
  deliver({{
    jsonrpc: "2.0",
    id: finalStatusCall.id,
    result: {{
      content: [],
      structuredContent: {{
        safe_state: "failed",
        candidate_revision_count: null,
        candidate_page_url: null,
        submission_state: "attached",
        chosen_scope: "current_session",
      }},
    }},
  }});
  await finalPoll;
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

    def _run_widget_recovery_scenario(
        self,
        scenario: str,
        expected_output: str,
    ) -> None:
        html = mcp_server.UPDATE_CANDIDATES_PATH.read_text("utf-8")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        harness = """
const vm = require("node:vm");
const shippedScript = __SHIPPED_SCRIPT__;
const controlId = "ctl_11111111111111111111111111111111";
const originalResult = {
  content: [],
  structuredContent: { actions_enabled: true, safe_state: "ready" },
  _meta: { "zdecision/control_id": controlId },
};

function check(condition, message) {{
  if (!condition) throw new Error(message);
}}

async function flush() {
  for (let index = 0; index < 6; index += 1) await Promise.resolve();
}

function state(safeState, submissionState, chosenScope = null) {
  return {
    content: [],
    structuredContent: {
      safe_state: safeState,
      candidate_revision_count: null,
      candidate_page_url: null,
      submission_state: submissionState,
      chosen_scope: chosenScope,
    },
  };
}

async function mount() {
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

    dispatch(name) {
      return Promise.all(
        (this.listeners.get(name) || []).map((listener) => listener()),
      );
    }
  }}

  const elements = Object.fromEntries(
    ["current", "all", "open-page", "page-address", "status", "card-state"].map(
      (id) => [id, new Element()],
    ),
  );
  const host = {{ postMessage(message) {{ outbound.push(message); }} }};
  const sandbox = {{
    document: {{ getElementById: (id) => elements[id] }},
    window: {{
      parent: host,
      addEventListener(name, listener) {{
        if (name === "message") messageHandler = listener;
      }},
    }},
    setTimeout(callback, delay) {{
      timers.push({{ callback, delay }});
      return timers.length;
    }},
    clearTimeout() {{}},
  }};
  vm.runInNewContext(shippedScript, sandbox);

  function deliver(message) {{
    messageHandler({{ source: host, data: message }});
  }}

  function latestToolCall(name) {{
    return [...outbound].reverse().find(
      (message) => message.method === "tools/call" && message.params?.name === name,
    );
  }}

  function toolCalls(name) {
    return outbound.filter(
      (message) => message.method === "tools/call" && message.params?.name === name,
    );
  }

  async function respond(call, result) {
    deliver({ jsonrpc: "2.0", id: call.id, result });
    await flush();
  }

  function takeTimer(delay) {
    const index = timers.findIndex((timer) => timer.delay === delay);
    if (index < 0) return null;
    return timers.splice(index, 1)[0].callback;
  }

  const initialize = outbound.find((message) => message.method === "ui/initialize");
  check(initialize, "widget did not initialize the bridge");
  deliver({
    jsonrpc: "2.0",
    id: initialize.id,
    result: { hostCapabilities: { serverTools: {} } },
  });
  await flush();
  deliver({
    jsonrpc: "2.0",
    method: "ui/notifications/tool-result",
    params: originalResult,
  });
  await flush();
  return {
    deliver,
    elements,
    timers,
    latestToolCall,
    toolCalls,
    respond,
    takeTimer,
  };
}

async function activateReady(widget) {
  const restore = widget.latestToolCall("get_zdecision_candidate_refresh");
  if (restore) await widget.respond(restore, state("ready", "ready"));
  check(
    !widget.elements.current.disabled && !widget.elements.all.disabled,
    "ready restoration did not enable actions",
  );
}

(async () => {
__SCENARIO__
})().catch((error) => {
  process.stderr.write(error.stack || String(error));
  process.exitCode = 1;
});
"""
        harness = harness.replace("{{", "{").replace("}}", "}")
        harness = harness.replace("__SHIPPED_SCRIPT__", json.dumps(script))
        harness = harness.replace("__SCENARIO__", scenario)

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
        self.assertEqual(expected_output, completed.stdout)

    async def test_widget_real_remount_restores_pending_from_original_result(
        self,
    ) -> None:
        self._run_widget_recovery_scenario(
            """
  const firstMount = await mount();
  await activateReady(firstMount);
  const click = firstMount.elements.all.dispatch("click");
  const firstStart = firstMount.latestToolCall(
    "start_zdecision_candidate_refresh",
  );
  check(firstStart, "first click did not submit");
  await firstMount.respond(
    firstStart,
    state("submitting", "pending", "all_valid_sessions"),
  );
  await click;

  const remount = await mount();
  const restore = remount.latestToolCall("get_zdecision_candidate_refresh");
  check(restore, "real remount did not restore through the app-only status tool");
  check(
    restore.params.arguments.control_id === controlId,
    "remount did not use the original private control",
  );
  await remount.respond(
    restore,
    state("submitting", "pending", "all_valid_sessions"),
  );
  const replay = remount.latestToolCall("start_zdecision_candidate_refresh");
  check(replay, "durable pending remount did not replay");
  check(
    replay.params.arguments.scope === "all_valid_sessions",
    "remount replay changed the persisted scope",
  );
  process.stdout.write("real-remount-recovery-ok");
""",
            "real-remount-recovery-ok",
        )

    async def test_widget_restores_from_wrapped_tool_result(self) -> None:
        self._run_widget_recovery_scenario(
            """
  const widget = await mount();
  const restore = widget.latestToolCall("get_zdecision_candidate_refresh");
  check(restore, "ready card did not request restoration");
  await widget.respond(restore, {
    toolResult: state("ready", "ready"),
  });
  check(
    !widget.elements.current.disabled && !widget.elements.all.disabled,
    "wrapped tool result did not restore ready actions",
  );
  process.stdout.write("wrapped-tool-result-ok");
""",
            "wrapped-tool-result-ok",
        )

    async def test_widget_ready_result_allows_omitted_scope(self) -> None:
        self._run_widget_recovery_scenario(
            """
  const widget = await mount();
  const restore = widget.latestToolCall("get_zdecision_candidate_refresh");
  check(restore, "ready card did not request restoration");
  await widget.respond(restore, {
    content: [],
    structuredContent: {
      safe_state: "ready",
      candidate_revision_count: null,
      candidate_page_url: null,
      submission_state: "ready",
    },
  });
  check(
    !widget.elements.current.disabled && !widget.elements.all.disabled,
    "omitted ready scope did not restore unselected actions",
  );
  process.stdout.write("omitted-ready-scope-ok");
""",
            "omitted-ready-scope-ok",
        )

    async def test_widget_distinguishes_current_and_historical_cards(
        self,
    ) -> None:
        self._run_widget_recovery_scenario(
            """
  const current = await mount();
  const currentRestore = current.latestToolCall(
    "get_zdecision_candidate_refresh",
  );
  await current.respond(currentRestore, state("ready", "ready"));
  check(
    current.elements["card-state"].textContent === "当前卡片",
    "ready binding was not identified as the current card",
  );
  check(
    !current.elements.current.disabled && !current.elements.all.disabled,
    "current card actions were not enabled",
  );

  const historical = await mount();
  const historicalRestore = historical.latestToolCall(
    "get_zdecision_candidate_refresh",
  );
  await historical.respond(historicalRestore, {
    content: [],
    structuredContent: {
      safe_state: "unavailable",
      candidate_revision_count: null,
      candidate_page_url: null,
    },
  });
  check(
    historical.elements["card-state"].textContent === "历史卡片",
    "expired binding was not identified as a historical card",
  );
  check(
    historical.elements.status.textContent === "此更新卡已失效",
    "historical card used the generic failure message",
  );
  check(
    historical.elements.current.disabled && historical.elements.all.disabled,
    "historical card actions stayed enabled",
  );

  const currentFailure = await mount();
  const failureRestore = currentFailure.latestToolCall(
    "get_zdecision_candidate_refresh",
  );
  await currentFailure.respond(
    failureRestore,
    state("unavailable", "attached", "current_session"),
  );
  check(
    currentFailure.elements["card-state"].textContent === "当前卡片",
    "bound failure was incorrectly identified as historical",
  );
  check(
    currentFailure.elements.status.textContent === "暂时无法更新",
    "bound failure lost the current-card failure message",
  );
  process.stdout.write("card-freshness-ok");
""",
            "card-freshness-ok",
        )

    async def test_widget_same_mount_retries_durable_pending_submission(
        self,
    ) -> None:
        self._run_widget_recovery_scenario(
            """
  const widget = await mount();
  await activateReady(widget);
  const click = widget.elements.current.dispatch("click");
  const firstStart = widget.latestToolCall("start_zdecision_candidate_refresh");
  await widget.respond(
    firstStart,
    state("submitting", "pending", "current_session"),
  );
  await click;
  const retryTimer = widget.takeTimer(1500);
  check(retryTimer, "same mount did not schedule a durable pending retry");
  retryTimer();
  await flush();
  const starts = widget.toolCalls("start_zdecision_candidate_refresh");
  check(starts.length === 2, "same mount did not replay exactly once");
  check(
    starts[1].params.arguments.scope === "current_session",
    "same-mount replay changed scope",
  );
  await widget.respond(
    starts[1],
    state("queued", "attached", "current_session"),
  );
  process.stdout.write("same-mount-retry-ok");
""",
            "same-mount-retry-ok",
        )

    async def test_widget_reused_iframe_rebinds_unselected_latest_control(
        self,
    ) -> None:
        self._run_widget_recovery_scenario(
            """
  const widget = await mount();
  await activateReady(widget);
  const nextControlId = "ctl_22222222222222222222222222222222";

  widget.deliver({
    jsonrpc: "2.0",
    method: "ui/notifications/tool-input",
    params: { control_id: nextControlId },
  });
  widget.deliver({
    jsonrpc: "2.0",
    method: "ui/notifications/tool-result",
    params: {
      content: [],
      structuredContent: { actions_enabled: true, safe_state: "ready" },
      _meta: { "zdecision/control_id": nextControlId },
    },
  });
  await flush();

  const restores = widget.toolCalls("get_zdecision_candidate_refresh");
  check(restores.length === 2, "reused iframe ignored the newer render result");
  check(
    restores[1].params.arguments.control_id === nextControlId,
    "reused iframe restored the expired control instead of the latest control",
  );
  await widget.respond(restores[1], state("ready", "ready"));
  check(
    !widget.elements.current.disabled && !widget.elements.all.disabled,
    "latest control did not restore ready actions",
  );
  process.stdout.write("reused-iframe-rebound-ok");
""",
            "reused-iframe-rebound-ok",
        )

    async def test_widget_ignores_late_restore_from_replaced_control(
        self,
    ) -> None:
        self._run_widget_recovery_scenario(
            """
  const widget = await mount();
  const oldRestore = widget.latestToolCall(
    "get_zdecision_candidate_refresh",
  );
  check(oldRestore, "initial control did not start restoration");
  const nextControlId = "ctl_22222222222222222222222222222222";

  widget.deliver({
    jsonrpc: "2.0",
    method: "ui/notifications/tool-input",
    params: { control_id: nextControlId },
  });
  widget.deliver({
    jsonrpc: "2.0",
    method: "ui/notifications/tool-result",
    params: {
      content: [],
      structuredContent: { actions_enabled: true, safe_state: "ready" },
      _meta: { "zdecision/control_id": nextControlId },
    },
  });
  await flush();

  const restores = widget.toolCalls("get_zdecision_candidate_refresh");
  check(restores.length === 2, "new control did not start restoration");
  await widget.respond(restores[1], state("ready", "ready"));
  check(
    !widget.elements.current.disabled && !widget.elements.all.disabled,
    "new control did not restore ready actions",
  );

  await widget.respond(oldRestore, {
    content: [],
    structuredContent: {
      safe_state: "unavailable",
      candidate_revision_count: null,
      candidate_page_url: null,
    },
  });
  check(
    !widget.elements.current.disabled && !widget.elements.all.disabled,
    "late old restoration overwrote the newer ready control",
  );
  check(
    widget.elements.status.textContent === "",
    "late old restoration replaced the newer ready status",
  );
  process.stdout.write("late-old-restore-ignored-ok");
""",
            "late-old-restore-ignored-ok",
        )

    async def test_widget_attached_and_terminal_remounts_never_submit(
        self,
    ) -> None:
        self._run_widget_recovery_scenario(
            """
  const attached = await mount();
  const attachedRestore = attached.latestToolCall(
    "get_zdecision_candidate_refresh",
  );
  check(attachedRestore, "attached remount did not restore status");
  await attached.respond(
    attachedRestore,
    state("queued", "attached", "current_session"),
  );
  check(
    attached.toolCalls("start_zdecision_candidate_refresh").length === 0,
    "attached remount submitted again",
  );
  const pollTimer = attached.takeTimer(1500);
  check(pollTimer, "attached active request did not continue status polling");
  pollTimer();
  await flush();
  check(
    attached.toolCalls("get_zdecision_candidate_refresh").length === 2,
    "attached remount did not stay on the read-only status path",
  );

  for (const terminal of [
    ["busy", "busy"],
    ["failed", "rejected"],
    ["failed", "legacy_unknown"],
  ]) {
    const widget = await mount();
    const restore = widget.latestToolCall("get_zdecision_candidate_refresh");
    check(restore, `${terminal[1]} remount did not restore status`);
    await widget.respond(
      restore,
      state(terminal[0], terminal[1], "all_valid_sessions"),
    );
    check(
      widget.elements.current.disabled && widget.elements.all.disabled,
      `${terminal[1]} remount enabled another action`,
    );
    check(
      widget.toolCalls("start_zdecision_candidate_refresh").length === 0,
      `${terminal[1]} remount replayed a terminal disposition`,
    );
  }
  process.stdout.write("terminal-remounts-closed-ok");
""",
            "terminal-remounts-closed-ok",
        )

    async def test_widget_requests_trusted_dashboard_launch_and_handles_uncertainty(
        self,
    ) -> None:
        html = mcp_server.UPDATE_CANDIDATES_PATH.read_text("utf-8")
        self.assertNotIn('target="_blank"', html)
        self.assertNotIn('rel="noopener noreferrer"', html)
        self.assertNotIn('"ui/open-link"', html)
        self.assertNotIn("window.open(", html)
        self.assertIn('id="page-address"', html)
        self.assertIn("打开决策中心", html)
        self.assertIn('name: "open_zdecision_dashboard"', html)

        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        harness = f"""
const vm = require("node:vm");
const shippedScript = {json.dumps(script)};
const outbound = [];
const timers = new Map();
let messageHandler = null;
let nextTimerId = 1;

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

const elementIds = [
  "current", "all", "open-page", "page-address", "status", "card-state",
];
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
  const id = nextTimerId++;
  timers.set(id, {{ callback, delay }});
  return id;
}};
global.clearTimeout = (id) => timers.delete(id);

function check(condition, message) {{
  if (!condition) throw new Error(message);
}}

function deliver(message) {{
  messageHandler({{ source: host, data: message }});
}}

function latestCall(method) {{
  return [...outbound].reverse().find((message) => message.method === method);
}}

function takeTimer(delay) {{
  const entry = [...timers.entries()].find(([, timer]) => timer.delay === delay);
  check(entry, `missing ${{delay}}ms timer`);
  timers.delete(entry[0]);
  return entry[1].callback;
}}

vm.runInThisContext(shippedScript);

(async () => {{
  const initialize = latestCall("ui/initialize");
  check(initialize, "widget did not initialize the bridge");
  deliver({{
    jsonrpc: "2.0",
    id: initialize.id,
    result: {{ hostCapabilities: {{ openLinks: {{}} }} }},
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
  const restoration = latestCall("tools/call");
  check(
    restoration.params?.name === "get_zdecision_candidate_refresh",
    "open-page fixture did not restore before enabling actions",
  );
  deliver({{
    jsonrpc: "2.0",
    id: restoration.id,
    result: {{
      content: [],
      structuredContent: {{
        safe_state: "ready",
        candidate_revision_count: null,
        candidate_page_url: null,
        submission_state: "ready",
        chosen_scope: null,
      }},
    }},
  }});
  await Promise.resolve();

  const refreshClick = elements.current.dispatch("click");
  const start = latestCall("tools/call");
  const candidateUrl = "https://decisions.example.test/?repository_id=repo_22222222222222222222222222222222";
  deliver({{
    jsonrpc: "2.0",
    id: start.id,
    result: {{
      content: [],
      structuredContent: {{
        safe_state: "succeeded",
        candidate_revision_count: 12,
        candidate_page_url: candidateUrl,
        submission_state: "attached",
        chosen_scope: "current_session",
      }},
    }},
  }});
  await refreshClick;
  check(!elements["open-page"].hidden, "terminal success did not show open action");

  const successfulClick = elements["open-page"].dispatch("click");
  const successfulOpen = latestCall("tools/call");
  check(
    successfulOpen.params.name === "open_zdecision_dashboard",
    "open action called the wrong tool",
  );
  check(
    JSON.stringify(successfulOpen.params.arguments)
      === JSON.stringify({{ control_id: trustedControl }}),
    "open action sent anything other than the trusted control",
  );
  check(elements.status.textContent === "正在请求默认浏览器", "open action had no immediate feedback");
  check(elements["open-page"].disabled, "open action allowed a duplicate call");
  deliver({{
    jsonrpc: "2.0",
    id: successfulOpen.id,
    result: {{
      structuredContent: {{
        safe_state: "launch_requested",
        dashboard_url: candidateUrl,
      }},
    }},
  }});
  await successfulClick;
  check(
    elements.status.textContent === "已请求使用默认浏览器打开决策中心",
    "accepted launch used false navigation copy",
  );
  check(!elements["open-page"].disabled, "accepted launch was not retryable");

  const rejectedClick = elements["open-page"].dispatch("click");
  const rejectedOpen = latestCall("tools/call");
  deliver({{
    jsonrpc: "2.0",
    id: rejectedOpen.id,
    result: {{
      structuredContent: {{
        safe_state: "unavailable",
        dashboard_url: candidateUrl,
      }},
    }},
  }});
  await rejectedClick;
  check(
    elements.status.textContent === "无法自动打开，请使用下方地址",
    "rejected launch hid the fallback state",
  );
  check(!elements["open-page"].disabled, "rejected launch prevented retry");
  check(!elements["page-address"].hidden, "fallback address stayed hidden");
  check(elements["page-address"].textContent === candidateUrl, "fallback address changed");

  const callCount = outbound.filter(
    (message) => message.method === "tools/call"
      && message.params?.name === "open_zdecision_dashboard",
  ).length;
  const timedOutClick = elements["open-page"].dispatch("click");
  const timedOutOpen = latestCall("tools/call");
  takeTimer(5000)();
  await timedOutClick;
  check(
    elements.status.textContent === "无法确认是否已打开；如未出现请重试",
    "lost response claimed a definite result",
  );
  check(!elements["open-page"].disabled, "lost response prevented retry");
  check(!elements["page-address"].hidden, "lost response hid the fallback address");
  check(elements["page-address"].textContent === candidateUrl, "lost response changed the fallback address");
  check(
    outbound.filter(
      (message) => message.method === "tools/call"
        && message.params?.name === "open_zdecision_dashboard",
    ).length === callCount + 1,
    "lost response triggered an automatic second launch",
  );
  deliver({{
    jsonrpc: "2.0",
    id: timedOutOpen.id,
    result: {{
      structuredContent: {{
        safe_state: "launch_requested",
        dashboard_url: candidateUrl,
      }},
    }},
  }});
  check(
    elements.status.textContent === "无法确认是否已打开；如未出现请重试",
    "late response changed the uncertain result",
  );
  process.stdout.write("default-browser-launch-ok");
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
        self.assertEqual("default-browser-launch-ok", completed.stdout)

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
