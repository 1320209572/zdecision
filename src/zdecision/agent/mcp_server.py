"""Local MCP tools exposed by the ZDecision Plugin."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from mcp.types import CallToolResult, TextContent
from pydantic import ConfigDict

from zdecision.agent.browser_launcher import (
    BrowserLauncher,
    SystemDefaultBrowserLauncher,
)
from zdecision.agent.central_client import CentralClient, CentralClientError
from zdecision.agent.config_locator import load_agent_config_path
from zdecision.agent.control_bindings import (
    ControlBinding,
    ControlBindingError,
    ControlBindingStore,
    ControlScopeConflict,
)
from zdecision.agent.db import AgentDatabase
from zdecision.agent.repository import RepositoryResolver
from zdecision.agent.recall_mcp import (
    LiveHostProbeProvider,
    ReadinessRecallGateProvider,
    RecallMcpTools,
)
from zdecision.agent.recall_host_state import RecallHostStore
from zdecision.agent.service import load_agent_config
from zdecision.app_server.gateway import AppServerGateway
from zdecision.sync.contracts import (
    CaptureRequestCreate,
    CaptureRequestView,
    CaptureScope,
)


UPDATE_CANDIDATES_URI = "ui://zdecision/update-candidates-v3.html"
UPDATE_CANDIDATES_MIME_TYPE = "text/html;profile=mcp-app"
UPDATE_CANDIDATES_PATH = (
    Path(__file__).resolve().parent / "static" / "update-candidates-v1.html"
)
RECALL_CONFIRMATION_URI = "ui://zdecision/recall-confirmation-v1.html"
RECALL_CONFIRMATION_MIME_TYPE = "text/html;profile=mcp-app"
RECALL_CONFIRMATION_PATH = (
    Path(__file__).resolve().parent / "static" / "recall-confirmation-v1.html"
)

_CAPTURING_PROGRESS = frozenset(
    (
        "capture_started",
        "capturing_sessions",
        "extracting_candidates",
        "reconciling_candidates",
    )
)
_SYNCING_PROGRESS = frozenset(("uploading_candidates",))


class LocalMcpTools:
    """Testable domain methods behind the stdio MCP adapter."""

    def __init__(
        self,
        *,
        database: AgentDatabase,
        cwd: str,
        binding_store: ControlBindingStore | None = None,
        central_client: CentralClient | None = None,
        central_base_url: str | None = None,
        clock: Callable[[], datetime] | None = None,
        action_id_factory: Callable[[], str] | None = None,
        browser_launcher: BrowserLauncher | None = None,
        repository_resolver: RepositoryResolver | None = None,
    ) -> None:
        self.database = database
        self.cwd = os.path.normpath(cwd)
        self.binding_store = binding_store
        self.central_client = central_client
        self.central_base_url = central_base_url
        self.clock = clock or (lambda: datetime.now(UTC))
        self.action_id_factory = action_id_factory or (
            lambda: f"codex_action_{uuid4().hex}"
        )
        self.browser_launcher = browser_launcher
        self.repository_resolver = repository_resolver or RepositoryResolver()

    def zdecision_status(self) -> dict[str, object]:
        snapshot = RepositoryResolver().resolve(self.cwd)
        repository = (
            None
            if snapshot is None
            else self.database.get_enabled_repository(snapshot.repository_id)
        )
        boundary = self.database.latest_open_boundary(self.cwd)
        return {
            "repository_registered": repository is not None,
            "repository_enabled": bool(
                repository is not None and repository.enabled
            ),
            "event_count": self.database.count_events(cwd=self.cwd),
            "active_session_bound": boundary is not None,
        }

    def show_zdecision_update(
        self, control_id: str | None = None
    ) -> CallToolResult:
        binding = self._valid_binding(control_id, require_current_cwd=True)
        selected = binding is not None and binding.chosen_scope is not None
        request_attached = bool(
            binding is not None and binding.central_request_id is not None
        )
        enabled = binding is not None and not selected
        safe_state = (
            "ready"
            if enabled
            else "queued"
            if request_attached
            else "submitting"
            if selected
            else "disabled"
        )
        meta: dict[str, object] = {}
        if binding is not None:
            meta["zdecision/control_id"] = binding.control_id
        if selected:
            meta["zdecision/chosen_scope"] = binding.chosen_scope
            meta["zdecision/request_attached"] = request_attached
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text="ZDecision Candidate refresh control is ready.",
                )
            ],
            structuredContent={
                "actions_enabled": enabled,
                "safe_state": safe_state,
            },
            _meta=meta,
        )

    def start_zdecision_candidate_refresh(
        self,
        control_id: str,
        scope: CaptureScope,
    ) -> dict[str, object]:
        binding = self._valid_binding(control_id, require_current_cwd=False)
        if binding is None or self.binding_store is None:
            return _safe_output("unavailable")
        if scope not in ("current_session", "all_valid_sessions"):
            return _safe_output("failed")
        try:
            binding = self.binding_store.choose_scope(
                binding.control_id,
                expected_repository_id=binding.repository_id,
                scope=scope,
                proposed_client_action_id=(
                    binding.client_action_id or self.action_id_factory()
                ),
                now=self.clock(),
            )
        except ControlScopeConflict:
            return _safe_output("failed")
        except (ControlBindingError, OSError, ValueError):
            return _safe_output("unavailable")

        if binding.submission_state == "attached":
            return self._read_request(binding)
        if binding.submission_state == "busy":
            return _binding_output(binding, "busy")
        if binding.submission_state in ("rejected", "legacy_unknown"):
            return _binding_output(binding, "failed")
        if binding.submission_state != "pending":
            return _safe_output("unavailable")

        return self._create_and_attach_request(binding)

    def _create_and_attach_request(
        self, binding: ControlBinding
    ) -> dict[str, object]:
        command = CaptureRequestCreate(
            repository_id=binding.repository_id,
            template_id="business",
            capture_scope=binding.chosen_scope,
            client_action_id=binding.client_action_id,
        )
        try:
            view = self.central_client.create_capture_request(command)
        except CentralClientError as error:
            if error.code == "repository_capture_busy":
                return self._finish_submission(binding, "busy", "busy")
            if error.code in (
                "central_connection_unavailable",
                "central_temporarily_unavailable",
            ):
                return _binding_output(binding, "submitting")
            return self._finish_submission(binding, "rejected", "failed")
        except Exception:
            return self._finish_submission(binding, "rejected", "failed")

        if not _request_matches_binding(view, binding):
            return self._finish_submission(binding, "rejected", "failed")
        try:
            attached = self.binding_store.attach_request(
                binding.control_id,
                client_action_id=binding.client_action_id,
                central_request_id=view.request_id,
            )
        except (ControlBindingError, OSError, ValueError):
            return _binding_output(binding, "submitting")
        return self._safe_request_output(view, attached)

    def _finish_submission(
        self,
        binding: ControlBinding,
        disposition: str,
        safe_state: str,
    ) -> dict[str, object]:
        try:
            finished = self.binding_store.finish_submission(
                binding.control_id,
                client_action_id=binding.client_action_id,
                disposition=disposition,
            )
        except (ControlBindingError, OSError, ValueError):
            return _safe_output("unavailable")
        return _binding_output(finished, safe_state)

    def get_zdecision_candidate_refresh(
        self, control_id: str
    ) -> dict[str, object]:
        binding = self._valid_binding(control_id, require_current_cwd=False)
        if binding is None:
            return _safe_output("unavailable")
        if binding.submission_state == "ready":
            return _binding_output(binding, "ready")
        if binding.submission_state == "pending":
            return _binding_output(binding, "submitting")
        if binding.submission_state == "attached":
            return self._read_request(binding)
        if binding.submission_state == "busy":
            return _binding_output(binding, "busy")
        if binding.submission_state in ("rejected", "legacy_unknown"):
            return _binding_output(binding, "failed")
        return _safe_output("unavailable")

    def open_zdecision_dashboard(
        self, control_id: str
    ) -> dict[str, object]:
        binding = self._valid_binding(control_id, require_current_cwd=False)
        if (
            binding is None
            or binding.submission_state != "attached"
            or binding.chosen_scope is None
            or binding.central_request_id is None
            or self.browser_launcher is None
        ):
            return _dashboard_output("unavailable")

        dashboard_url = _dashboard_url(
            self.central_base_url, binding.repository_id
        )
        if dashboard_url is None:
            return _dashboard_output("unavailable")
        try:
            accepted = self.browser_launcher.open(dashboard_url)
        except Exception:
            accepted = False
        return _dashboard_output(
            "launch_requested" if accepted else "unavailable",
            dashboard_url=dashboard_url,
        )

    def _read_request(self, binding: ControlBinding) -> dict[str, object]:
        try:
            view = self.central_client.get_capture_request(
                binding.central_request_id
            )
        except CentralClientError as error:
            if error.code in (
                "central_connection_unavailable",
                "central_temporarily_unavailable",
            ):
                return _binding_output(binding, "retrying")
            return _binding_output(binding, "unavailable")
        except Exception:
            return _binding_output(binding, "unavailable")
        if (
            view.request_id != binding.central_request_id
            or not _request_matches_binding(view, binding)
        ):
            return _binding_output(binding, "unavailable")
        return self._safe_request_output(view, binding)

    def _safe_request_output(
        self,
        view: CaptureRequestView,
        binding: ControlBinding,
    ) -> dict[str, object]:
        safe_state = _safe_request_state(view)
        if safe_state is None:
            return _binding_output(binding, "unavailable")
        terminal_success = safe_state in ("empty", "succeeded")
        count = view.candidate_revision_count if terminal_success else None
        page_url = (
            _dashboard_url(self.central_base_url, binding.repository_id)
            if terminal_success
            else None
        )
        if terminal_success and page_url is None:
            return _binding_output(binding, "unavailable")
        return _binding_output(
            binding,
            safe_state,
            candidate_revision_count=count,
            candidate_page_url=page_url,
        )

    def _valid_binding(
        self,
        control_id: object,
        *,
        require_current_cwd: bool,
    ) -> ControlBinding | None:
        if (
            self.binding_store is None
            or self.central_client is None
            or _validated_base_url(self.central_base_url) is None
            or not isinstance(control_id, str)
        ):
            return None
        try:
            binding = self.binding_store.get(control_id)
            if binding is None:
                return None
            if (
                require_current_cwd
                and os.path.normpath(binding.cwd) != self.cwd
            ):
                return None
            repository = self.database.get_enabled_repository(
                binding.repository_id
            )
            if repository is None or not repository.enabled:
                return None
            if binding.repository_id != repository.repository_id:
                return None
            if binding.chosen_scope is not None:
                return binding
            if self.clock() >= _timestamp(binding.expires_at):
                return None
            snapshot = self.repository_resolver.resolve(binding.cwd)
            if (
                snapshot is None
                or snapshot.repository_id != binding.repository_id
            ):
                return None
            return binding
        except Exception:
            return None


def create_mcp_server(
    tools: LocalMcpTools,
    recall_tools: RecallMcpTools | None = None,
):
    """Create the local server and its isolated Candidate refresh card."""

    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP("zdecision-local")
    read_only = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    app_action = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    browser_action = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
    recall_action = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @server.resource(
        UPDATE_CANDIDATES_URI,
        name="zdecision-update-candidates",
        title="ZDecision Candidate refresh control",
        description="Two-scope Candidate refresh control for the current repository.",
        mime_type=UPDATE_CANDIDATES_MIME_TYPE,
        meta={
            "ui": {
                "prefersBorder": True,
                "csp": {
                    "connectDomains": [],
                    "resourceDomains": [],
                },
            },
            "openai/widgetDescription": (
                "A compact ZDecision control for refreshing Candidate revisions."
            ),
        },
    )
    def update_candidates_resource() -> str:
        return UPDATE_CANDIDATES_PATH.read_text("utf-8")

    @server.resource(
        RECALL_CONFIRMATION_URI,
        name="zdecision-recall-confirmation",
        title="ZDecision Recall confirmation",
        description="One-task confirmation for ZDecision Recall.",
        mime_type=RECALL_CONFIRMATION_MIME_TYPE,
        meta={
            "ui": {
                "prefersBorder": True,
                "csp": {
                    "connectDomains": [],
                    "resourceDomains": [],
                },
            },
            "openai/widgetDescription": (
                "A compact confirmation for enabling Recall in the current task."
            ),
        },
    )
    def recall_confirmation_resource() -> str:
        return RECALL_CONFIRMATION_PATH.read_bytes().decode("utf-8")

    @server.tool(annotations=read_only)
    def zdecision_status() -> dict[str, object]:
        """Return local registration, event, and Session-binding status."""

        return tools.zdecision_status()

    @server.tool(
        title="Show ZDecision Candidate refresh",
        description=(
            "Display the read-only ZDecision controls for a trusted local binding."
        ),
        annotations=read_only,
        meta={
            "ui": {
                "resourceUri": UPDATE_CANDIDATES_URI,
                "visibility": ["model", "app"],
            },
            "openai/outputTemplate": UPDATE_CANDIDATES_URI,
            "openai/toolInvocation/invoking": "Opening ZDecision control…",
            "openai/toolInvocation/invoked": "ZDecision control ready",
        },
    )
    def show_zdecision_update(
        control_id: str | None = None,
    ) -> CallToolResult:
        return tools.show_zdecision_update(control_id)

    @server.tool(
        title="Start ZDecision Candidate refresh",
        description="Start the trusted refresh scope selected in the app.",
        annotations=app_action,
        meta={"ui": {"visibility": ["app"]}},
    )
    def start_zdecision_candidate_refresh(
        control_id: str,
        scope: CaptureScope,
    ) -> dict[str, object]:
        return tools.start_zdecision_candidate_refresh(control_id, scope)

    @server.tool(
        title="Get ZDecision Candidate refresh status",
        description="Read bounded progress for the app's trusted refresh control.",
        annotations=read_only,
        meta={"ui": {"visibility": ["app"]}},
    )
    def get_zdecision_candidate_refresh(
        control_id: str,
    ) -> dict[str, object]:
        return tools.get_zdecision_candidate_refresh(control_id)

    @server.tool(
        title="Open ZDecision dashboard",
        description="Open the trusted product dashboard in the default browser.",
        annotations=browser_action,
        meta={"ui": {"visibility": ["app"]}},
    )
    def open_zdecision_dashboard(control_id: str) -> dict[str, object]:
        return tools.open_zdecision_dashboard(control_id)

    if recall_tools is not None:

        @server.tool(
            title="Show ZDecision Recall confirmation",
            description="Display the trusted Recall confirmation for this task.",
            annotations=recall_action,
            meta={
                "ui": {
                    "resourceUri": RECALL_CONFIRMATION_URI,
                    "visibility": ["model", "app"],
                },
                "openai/outputTemplate": RECALL_CONFIRMATION_URI,
                "openai/toolInvocation/invoking": "Opening Recall confirmation…",
                "openai/toolInvocation/invoked": "Recall confirmation ready",
            },
        )
        def show_zdecision_recall_confirmation(
            activation_attempt_id: str,
        ) -> CallToolResult:
            result = recall_tools.show_recall_confirmation(
                activation_attempt_id=activation_attempt_id,
                ui_digest=_recall_confirmation_digest(),
            )
            return _confirmation_call_result(result)

        @server.tool(
            title="Decide ZDecision Recall",
            description="Commit the confirmation action selected in the app.",
            annotations=recall_action,
            meta={"ui": {"visibility": ["app"]}},
        )
        def decide_zdecision_recall(
            activation_attempt_id: str,
            action: Literal["enable", "decline"],
        ) -> CallToolResult:
            result = recall_tools.decide_recall_confirmation(
                activation_attempt_id=activation_attempt_id,
                action=action,
                current_ui_digest=_recall_confirmation_digest(),
            )
            return _confirmation_call_result(result)

        @server.tool(
            title="Gate ZDecision Recall Turn",
            description="Gate one native Turn through its trusted host binding.",
            annotations=recall_action,
        )
        def gate_zdecision_turn(
            turn_gate_id: str,
            intent: object,
        ) -> dict[str, object]:
            return recall_tools.gate_zdecision_turn(
                turn_gate_id=turn_gate_id,
                intent=intent,
            )

        _forbid_extra_tool_input(
            server, "show_zdecision_recall_confirmation"
        )
        _forbid_extra_tool_input(server, "decide_zdecision_recall")
        _forbid_extra_tool_input(server, "gate_zdecision_turn")

    return server


def _forbid_extra_tool_input(server: object, tool_name: str) -> None:
    """Make the current MCP SDK's generated argument model closed-world."""

    tool = server._tool_manager.get_tool(tool_name)
    if tool is None:
        raise RuntimeError("Recall MCP tool registration failed")
    argument_model = tool.fn_metadata.arg_model
    argument_model.model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
    )
    argument_model.model_rebuild(force=True)
    tool.parameters = argument_model.model_json_schema(by_alias=True)


def _recall_confirmation_digest() -> str:
    return hashlib.sha256(RECALL_CONFIRMATION_PATH.read_bytes()).hexdigest()


def _confirmation_call_result(value: object) -> CallToolResult:
    if not isinstance(value, dict):
        value = {"state": "blocked", "code": "invalid_confirmation"}
    state = value.get("state")
    if state not in (
        "pending_confirmation",
        "committed",
        "declined",
        "cancelled",
        "failed",
        "blocked",
    ):
        state = "blocked"
    structured: dict[str, object] = {"state": state}
    if state == "blocked" and value.get("code") == "invalid_confirmation":
        structured["code"] = "invalid_confirmation"
    meta = value.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
    safe_meta = {
        key: item
        for key, item in meta.items()
        if key
        in (
            "zdecision/activation_attempt_id",
            "zdecision/repository_id",
            "zdecision/repository_display_name",
        )
        and isinstance(item, str)
    }
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text="ZDecision Recall confirmation is ready.",
            )
        ],
        structuredContent=structured,
        _meta=safe_meta,
    )


def run_mcp(
    *,
    database_path: Path,
    config_locator_path: Path,
    cwd: str,
) -> None:
    """Start the MCP SDK only for the explicit `mcp` subcommand."""

    database = AgentDatabase.open(database_path)
    recall_store = RecallHostStore.open(database_path)
    binding_store: ControlBindingStore | None = None
    client: CentralClient | None = None
    central_base_url: str | None = None
    try:
        try:
            binding_store = ControlBindingStore.open(database_path)
        except Exception:
            binding_store = None
        try:
            config_path = load_agent_config_path(config_locator_path)
            config = load_agent_config(config_path)
            client = CentralClient(config.central_url, config.device_token)
            central_base_url = config.central_url
        except Exception:
            client = None
            central_base_url = None

        tools = LocalMcpTools(
            database=database,
            cwd=cwd,
            binding_store=binding_store,
            central_client=client,
            central_base_url=central_base_url,
            browser_launcher=SystemDefaultBrowserLauncher(),
        )
        live_acceptance = os.environ.get("ZDECISION_LIVE_ACCEPTANCE") == "1"
        provider = (
            LiveHostProbeProvider(database_path, cwd)
            if live_acceptance
            else ReadinessRecallGateProvider()
        )
        recall_tools = RecallMcpTools(
            host_store=recall_store,
            provider=provider,
            cwd=cwd,
            live_acceptance=live_acceptance,
            evidence_gateway_factory=lambda: AppServerGateway.connect(
                database=database
            ),
            recall_skill_path=None,
        )
        create_mcp_server(tools, recall_tools).run(transport="stdio")
    finally:
        if client is not None:
            client.close()
        if binding_store is not None:
            binding_store.close()
        recall_store.close()
        database.close()


def _request_matches_binding(
    view: CaptureRequestView, binding: ControlBinding
) -> bool:
    return bool(
        isinstance(view, CaptureRequestView)
        and view.repository_id == binding.repository_id
        and view.template_id == "business"
    )


def _safe_request_state(view: CaptureRequestView) -> str | None:
    if view.state in ("queued", "claimed"):
        return "queued"
    if view.state == "running":
        if view.progress_code in _CAPTURING_PROGRESS:
            return "capturing"
        if view.progress_code in _SYNCING_PROGRESS:
            return "syncing"
        return None
    if view.state in ("failed_retryable", "failed_terminal", "cancelled"):
        return "failed"
    if view.state in ("succeeded", "succeeded_no_candidates"):
        if view.candidate_revision_count == 0:
            return "empty"
        if (
            isinstance(view.candidate_revision_count, int)
            and view.candidate_revision_count > 0
        ):
            return "succeeded"
    return None


def _safe_output(
    safe_state: str,
    *,
    candidate_revision_count: int | None = None,
    candidate_page_url: str | None = None,
) -> dict[str, object]:
    return {
        "safe_state": safe_state,
        "candidate_revision_count": candidate_revision_count,
        "candidate_page_url": candidate_page_url,
    }


def _binding_output(
    binding: ControlBinding,
    safe_state: str,
    *,
    candidate_revision_count: int | None = None,
    candidate_page_url: str | None = None,
) -> dict[str, object]:
    return {
        **_safe_output(
            safe_state,
            candidate_revision_count=candidate_revision_count,
            candidate_page_url=candidate_page_url,
        ),
        "submission_state": binding.submission_state,
        "chosen_scope": binding.chosen_scope,
    }


def _dashboard_output(
    safe_state: str,
    *,
    dashboard_url: str | None = None,
) -> dict[str, object]:
    return {
        "safe_state": safe_state,
        "dashboard_url": dashboard_url,
    }


def _validated_base_url(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (
        parsed.scheme not in ("http", "https")
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    return value.rstrip("/")


def _dashboard_url(
    central_base_url: object, repository_id: str
) -> str | None:
    base_url = _validated_base_url(central_base_url)
    if base_url is None:
        return None
    return f"{base_url}/?{urlencode({'repository_id': repository_id})}"


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
