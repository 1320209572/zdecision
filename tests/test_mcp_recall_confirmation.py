from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mcp.server.fastmcp.exceptions import ToolError

from zdecision.agent import mcp_server
from zdecision.agent.db import AgentDatabase
from zdecision.agent.mcp_server import LocalMcpTools
from zdecision.agent.recall_handoff import RecallHandoffService
from zdecision.agent.recall_host_state import RecallHostStore
from zdecision.agent.recall_mcp import ReadinessRecallGateProvider, RecallMcpTools
from zdecision.recall.handoff import (
    RecallPreflightReady,
    RecallShortlist,
    RecalledDecision,
)
from zdecision.recall.session import RecallIntent

from tests.test_recall_handoff_contracts import formal_decision


NOW = datetime(2026, 8, 9, 4, 0, tzinfo=UTC)
ATTEMPT_ID = "activation_" + "1" * 32
REPOSITORY_ID = "repo_" + "2" * 32
REPOSITORY_NAME = "zdecision"
WIDGET_URI = "ui://zdecision/recall-confirmation-v1.html"
WIDGET_MIME_TYPE = "text/html;profile=mcp-app"
WIDGET_PATH = (
    Path(mcp_server.__file__).resolve().parent
    / "static"
    / "recall-confirmation-v1.html"
)
VALID_INTENT: dict[str, object] = {
    "target_decision_space_ids": ["dsp_" + "3" * 32],
    "explicit_multi_space": False,
    "feature_goal": "Implement the Recall confirmation preflight",
    "domain_objects": ["RecallIntent", "ConfirmationAttempt"],
    "repository_relative_paths": ["src/zdecision/agent"],
    "constraints": ["Use only trusted local state"],
    "exclusions": ["Decision retrieval"],
}

DELIVERY_ID = "delivery_" + "4" * 32


class _McpRecallProvider:
    def __init__(self, shortlist: RecallShortlist) -> None:
        self.shortlist = shortlist
        self.retrieve_calls = 0

    def retrieve(self, preflight):
        self.retrieve_calls += 1
        return self.shortlist


def _ready_preflight(intent: RecallIntent) -> RecallPreflightReady:
    return RecallPreflightReady(
        repository_id=REPOSITORY_ID,
        repository_display_name=REPOSITORY_NAME,
        intent=intent,
        target_decision_space_ids=("dsp_" + "3" * 32,),
        target_display_names=("ZDecision",),
        catalog_digest="a" * 64,
        generation=4,
        generation_digest="b" * 64,
        retrieval_profile_digest="c" * 64,
        index_generation=3,
        freshness="degraded",
        expires_at="2026-08-09T05:00:00Z",
    )


class RecallConfirmationMcpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.cwd = str(self.root / "enabled-repository")
        Path(self.cwd).mkdir()
        self.database_path = self.root / "agent" / "zdecision.sqlite3"
        self.database = AgentDatabase.open(self.database_path)
        self.addCleanup(self.database.close)
        self.store = RecallHostStore.open(self.database_path)
        self.addCleanup(self.store.close)
        self.intent = RecallIntent.from_dict(VALID_INTENT)
        self.preflight = _ready_preflight(self.intent)
        self.store.create_activation_attempt(
            session_id="private-session",
            turn_id="private-turn",
            cwd=self.cwd,
            repository_id=REPOSITORY_ID,
            repository_display_name=REPOSITORY_NAME,
            attempt_id=ATTEMPT_ID,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
            intent=self.intent,
            preflight=self.preflight,
        )
        local = LocalMcpTools(database=self.database, cwd=self.cwd)
        recall = RecallMcpTools(
            host_store=self.store,
            provider=ReadinessRecallGateProvider(),
            cwd=self.cwd,
            clock=lambda: NOW,
        )
        self.server = mcp_server.create_mcp_server(local, recall)

    async def test_registers_bordered_resource_and_closed_visibility_tools(
        self,
    ) -> None:
        resources = {
            str(item.uri): item for item in await self.server.list_resources()
        }
        tools = {item.name: item for item in await self.server.list_tools()}

        self.assertIn(WIDGET_URI, resources)
        resource = resources[WIDGET_URI]
        self.assertEqual(WIDGET_MIME_TYPE, resource.mimeType)
        self.assertTrue(resource.meta["ui"]["prefersBorder"])
        self.assertEqual(
            {"connectDomains": [], "resourceDomains": []},
            resource.meta["ui"]["csp"],
        )
        self.assertEqual(
            ["model", "app"],
            tools["show_zdecision_recall_confirmation"].meta["ui"][
                "visibility"
            ],
        )
        self.assertEqual(
            ["app"],
            tools["decide_zdecision_recall"].meta["ui"]["visibility"],
        )
        self.assertNotIn("activate_zdecision_recall", tools)

        render_schema = tools[
            "show_zdecision_recall_confirmation"
        ].inputSchema
        decision_schema = tools["decide_zdecision_recall"].inputSchema
        self.assertTrue(
            tools["show_zdecision_recall_confirmation"].annotations.readOnlyHint
        )
        self.assertEqual(
            {"activation_attempt_id", "intent"},
            set(render_schema["properties"]),
        )
        self.assertNotIn(
            "activation_attempt_id", render_schema.get("required", [])
        )
        self.assertIn("intent", render_schema.get("required", []))
        intent_schema = render_schema["$defs"]["RecallIntentInput"]
        self.assertEqual(set(VALID_INTENT), set(intent_schema["properties"]))
        self.assertEqual(set(VALID_INTENT), set(intent_schema["required"]))
        self.assertFalse(intent_schema.get("additionalProperties", True))
        self.assertEqual(
            {"activation_attempt_id", "action"},
            set(decision_schema["properties"]),
        )
        self.assertEqual(
            ["enable", "decline"],
            decision_schema["properties"]["action"]["enum"],
        )
        self.assertFalse(render_schema.get("additionalProperties", True))
        self.assertFalse(decision_schema.get("additionalProperties", True))

    async def test_render_without_hook_binding_fails_closed_after_schema_validation(
        self,
    ) -> None:
        """This catches MCP validation preventing the trusted Hook from binding."""

        result = await self.server.call_tool(
            "show_zdecision_recall_confirmation",
            {"intent": dict(VALID_INTENT)},
        )

        self.assertTrue(result.isError)
        self.assertEqual(
            {"state": "blocked", "code": "invalid_confirmation"},
            result.structuredContent,
        )

    async def test_render_binds_exact_html_digest_and_keeps_identity_in_meta(
        self,
    ) -> None:
        result = await self.server.call_tool(
            "show_zdecision_recall_confirmation",
            {
                "activation_attempt_id": ATTEMPT_ID,
                "intent": dict(VALID_INTENT),
            },
        )

        self.assertFalse(result.isError)
        self.assertEqual(
            {"state": "pending_confirmation"}, result.structuredContent
        )
        self.assertEqual(
            ATTEMPT_ID,
            result.meta["zdecision/activation_attempt_id"],
        )
        self.assertEqual(
            REPOSITORY_NAME,
            result.meta["zdecision/repository_display_name"],
        )
        self.assertEqual(
            ["ZDecision"], result.meta["zdecision/target_display_names"]
        )
        self.assertEqual("degraded", result.meta["zdecision/freshness"])
        self.assertEqual(
            {
                "zdecision/activation_attempt_id",
                "zdecision/repository_display_name",
                "zdecision/target_display_names",
                "zdecision/freshness",
            },
            set(result.meta),
        )
        model_visible = json.dumps(
            {
                "content": [item.model_dump() for item in result.content],
                "structuredContent": result.structuredContent,
            },
            sort_keys=True,
        )
        for private in (ATTEMPT_ID, REPOSITORY_ID, REPOSITORY_NAME):
            self.assertNotIn(private, model_visible)
        expected_digest = hashlib.sha256(WIDGET_PATH.read_bytes()).hexdigest()
        self.assertEqual(
            expected_digest,
            self.store.get_activation_attempt(ATTEMPT_ID).ui_digest,
        )

    async def test_missing_trusted_attempt_is_an_unambiguous_tool_error(
        self,
    ) -> None:
        """This catches a blocked confirmation masquerading as a ready card."""

        result = await self.server.call_tool(
            "show_zdecision_recall_confirmation",
            {
                "activation_attempt_id": "activation_" + "9" * 32,
                "intent": dict(VALID_INTENT),
            },
        )

        self.assertTrue(result.isError)
        self.assertEqual(
            {"state": "blocked", "code": "invalid_confirmation"},
            result.structuredContent,
        )
        model_text = " ".join(item.text for item in result.content)
        self.assertIn("unavailable", model_text.lower())
        self.assertIn("Do not retry or guess", model_text)
        self.assertNotIn("confirmation is ready", model_text.lower())
        self.assertEqual({}, result.meta)

    async def test_decline_uses_the_same_card_digest_and_remains_app_only(
        self,
    ) -> None:
        await self.server.call_tool(
            "show_zdecision_recall_confirmation",
            {
                "activation_attempt_id": ATTEMPT_ID,
                "intent": dict(VALID_INTENT),
            },
        )

        result = await self.server.call_tool(
            "decide_zdecision_recall",
            {"activation_attempt_id": ATTEMPT_ID, "action": "decline"},
        )

        self.assertFalse(result.isError)
        self.assertEqual({"state": "declined"}, result.structuredContent)
        self.assertEqual(
            ATTEMPT_ID,
            result.meta["zdecision/activation_attempt_id"],
        )
        self.assertEqual(
            {
                "zdecision/activation_attempt_id",
                "zdecision/repository_display_name",
                "zdecision/target_display_names",
                "zdecision/freshness",
            },
            set(result.meta),
        )
        self.assertEqual(
            REPOSITORY_NAME,
            result.meta["zdecision/repository_display_name"],
        )
        self.assertIsNone(self.store.get_session("private-session"))

    async def test_v1_enable_delegates_to_one_private_frozen_delivery(self) -> None:
        """This catches MCP committing consent without handoff preparation."""

        await self.server.call_tool(
            "show_zdecision_recall_confirmation",
            {
                "activation_attempt_id": ATTEMPT_ID,
                "intent": dict(VALID_INTENT),
            },
        )
        item = RecalledDecision.create(
            decision_space_id=self.preflight.target_decision_space_ids[0],
            revision=formal_decision(),
            match_reason="Exact product match",
        )
        provider = _McpRecallProvider(
            RecallShortlist.create(preflight=self.preflight, items=(item,))
        )
        service = RecallHandoffService(
            store=self.store,
            provider=provider,
            clock=lambda: NOW,
            delivery_id_factory=lambda _: DELIVERY_ID,
            claim_token_factory=lambda: "claim_" + "5" * 32,
        )
        local = LocalMcpTools(database=self.database, cwd=self.cwd)
        recall = RecallMcpTools(
            host_store=self.store,
            provider=ReadinessRecallGateProvider(),
            handoff_service=service,
            cwd=self.cwd,
            clock=lambda: NOW,
        )
        server = mcp_server.create_mcp_server(local, recall)

        result = await server.call_tool(
            "decide_zdecision_recall",
            {"activation_attempt_id": ATTEMPT_ID, "action": "enable"},
        )

        self.assertFalse(result.isError)
        self.assertEqual({"state": "delivery_claimed"}, result.structuredContent)
        self.assertEqual(1, provider.retrieve_calls)
        self.assertEqual(
            {
                "zdecision/activation_attempt_id",
                "zdecision/repository_display_name",
                "zdecision/target_display_names",
                "zdecision/freshness",
                "zdecision/delivery_id",
                "zdecision/context_text",
                "zdecision/snapshot_digest",
                "zdecision/context_digest",
            },
            set(result.meta),
        )
        self.assertEqual(DELIVERY_ID, result.meta["zdecision/delivery_id"])
        self.assertEqual("activating", self.store.get_session("private-session").state)
        model_visible = json.dumps(
            {
                "content": [content.model_dump() for content in result.content],
                "structuredContent": result.structuredContent,
            },
            sort_keys=True,
        )
        for private in (
            ATTEMPT_ID,
            DELIVERY_ID,
            result.meta["zdecision/context_text"],
            result.meta["zdecision/snapshot_digest"],
            result.meta["zdecision/context_digest"],
        ):
            self.assertNotIn(private, model_visible)

        replay = await server.call_tool(
            "decide_zdecision_recall",
            {"activation_attempt_id": ATTEMPT_ID, "action": "enable"},
        )
        self.assertFalse(replay.isError)
        self.assertEqual(
            {"state": "delivery_claimed", "code": "delivery_in_progress"},
            replay.structuredContent,
        )
        self.assertEqual(1, provider.retrieve_calls)
        self.assertEqual(DELIVERY_ID, replay.meta["zdecision/delivery_id"])
        self.assertEqual(
            result.meta["zdecision/context_digest"],
            replay.meta["zdecision/context_digest"],
        )
        self.assertNotIn("zdecision/context_text", replay.meta)

    async def test_default_provider_fails_closed_after_v1_consent(self) -> None:
        """This catches production fabricating Recall data before Gates B and C."""

        await self.server.call_tool(
            "show_zdecision_recall_confirmation",
            {
                "activation_attempt_id": ATTEMPT_ID,
                "intent": dict(VALID_INTENT),
            },
        )

        result = await self.server.call_tool(
            "decide_zdecision_recall",
            {"activation_attempt_id": ATTEMPT_ID, "action": "enable"},
        )

        self.assertTrue(result.isError)
        self.assertEqual(
            {"state": "blocked", "code": "delivery_prepare_failed"},
            result.structuredContent,
        )
        self.assertNotIn("zdecision/context_text", result.meta)
        self.assertEqual("preparing", self.store.delivery_for_attempt(ATTEMPT_ID).state)
        self.assertEqual("activating", self.store.get_session("private-session").state)

    async def test_legacy_attempt_enable_keeps_the_old_decision_path(self) -> None:
        """This catches the v1 handoff breaking the prior host-gate protocol."""

        legacy_attempt_id = "activation_" + "8" * 32
        self.store.create_activation_attempt(
            session_id="legacy-session",
            turn_id="legacy-turn",
            cwd=self.cwd,
            repository_id=REPOSITORY_ID,
            repository_display_name=REPOSITORY_NAME,
            attempt_id=legacy_attempt_id,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
        )
        self.store.attach_activation_card(
            legacy_attempt_id,
            ui_digest=hashlib.sha256(WIDGET_PATH.read_bytes()).hexdigest(),
        )

        result = await self.server.call_tool(
            "decide_zdecision_recall",
            {"activation_attempt_id": legacy_attempt_id, "action": "enable"},
        )

        self.assertFalse(result.isError)
        self.assertEqual({"state": "committed"}, result.structuredContent)
        session = self.store.get_session("legacy-session")
        self.assertEqual("active", session.state)
        self.assertIsNone(session.protocol_version)
        self.assertIsNone(self.store.delivery_for_attempt(legacy_attempt_id))

    async def test_render_rejects_mismatched_intent_and_legacy_attempt(self) -> None:
        mismatched = {**VALID_INTENT, "feature_goal": "A substituted intent"}
        legacy_attempt_id = "activation_" + "8" * 32
        self.store.create_activation_attempt(
            session_id="legacy-session",
            turn_id="legacy-turn",
            cwd=self.cwd,
            repository_id=REPOSITORY_ID,
            repository_display_name=REPOSITORY_NAME,
            attempt_id=legacy_attempt_id,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
        )

        for attempt_id, intent in (
            (ATTEMPT_ID, mismatched),
            (legacy_attempt_id, VALID_INTENT),
        ):
            with self.subTest(attempt_id=attempt_id):
                result = await self.server.call_tool(
                    "show_zdecision_recall_confirmation",
                    {
                        "activation_attempt_id": attempt_id,
                        "intent": dict(intent),
                    },
                )
                self.assertTrue(result.isError)
                self.assertEqual(
                    {"state": "blocked", "code": "invalid_confirmation"},
                    result.structuredContent,
                )

    async def test_tools_reject_host_coordinates_and_untrusted_flags(
        self,
    ) -> None:
        registered = {item.name for item in await self.server.list_tools()}
        self.assertIn("show_zdecision_recall_confirmation", registered)
        self.assertIn("decide_zdecision_recall", registered)
        forbidden = {
            "session_id": "private-session",
            "cwd": self.cwd,
            "repository_id": REPOSITORY_ID,
            "confirmed": True,
        }
        for name, arguments in (
            (
                "show_zdecision_recall_confirmation",
                {
                    "activation_attempt_id": ATTEMPT_ID,
                    "intent": dict(VALID_INTENT),
                },
            ),
            (
                "decide_zdecision_recall",
                {"activation_attempt_id": ATTEMPT_ID, "action": "decline"},
            ),
        ):
            for field, value in forbidden.items():
                with self.subTest(tool=name, field=field):
                    with self.assertRaises(ToolError):
                        await self.server.call_tool(
                            name, {**arguments, field: value}
                        )


class RecallConfirmationCardTests(unittest.TestCase):
    def test_card_contains_only_the_two_confirmation_buttons(self) -> None:
        self.assertTrue(WIDGET_PATH.is_file(), f"missing card: {WIDGET_PATH}")
        html = WIDGET_PATH.read_text("utf-8")

        self.assertEqual(2, html.count("<button"))
        self.assertIn("启用本任务决策召回", html)
        self.assertIn("暂不启用", html)
        self.assertIn("当前任务期间", html)
        lowered = html.lower()
        for forbidden in (
            "session_id",
            "turn_id",
            "cwd",
            "/users/",
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

    def test_load_remount_duplicate_result_and_timeout_never_auto_enable(
        self,
    ) -> None:
        output = self._run_card(
            """
  const first = await mount();
  check(first.toolCalls().length === 0, "initialization called a tool");
  first.deliverRender();
  first.deliverRender();
  await flush();
  check(first.toolCalls().length === 0, "render notification called a tool");

  const second = await mount();
  second.deliverRender();
  await flush();
  check(second.toolCalls().length === 0, "remount called a tool");

  const click = second.elements.enable.dispatch("click");
  const calls = second.toolCalls();
  check(calls.length === 1, "one click did not make exactly one decision call");
  check(
    JSON.stringify(calls[0].params) === JSON.stringify({
      name: "decide_zdecision_recall",
      arguments: { activation_attempt_id: attemptId, action: "enable" },
    }),
    "enable click sent an unexpected payload",
  );
  second.takeTimer(5000)();
  await click;
  await flush();
  second.runAllTimers();
  await flush();
  check(second.toolCalls().length === 1, "timeout automatically retried enable");
  process.stdout.write("no-auto-enable-ok");
""",
        )
        self.assertEqual("no-auto-enable-ok", output)

    def test_committed_enable_sends_at_most_one_bounded_ui_message(self) -> None:
        output = self._run_card(
            """
  const widget = await mount();
  widget.deliverRender();
  await flush();
  const click = widget.elements.enable.dispatch("click");
  const decision = widget.toolCalls()[0];
  widget.respond(decision, result("committed"));
  await click;
  await flush();
  widget.deliverCommittedNotification();
  widget.deliverCommittedNotification();
  await flush();
  const messages = widget.messages();
  check(messages.length === 1, "committed enable sent more than one ui/message");
  check(messages[0].params?.role === "user", "ui/message used the wrong role");
  const continuation = messages[0].params?.content;
  check(Array.isArray(continuation), "ui/message content was not an array");
  check(continuation.length === 1, "ui/message content was not bounded");
  check(continuation[0]?.type === "text", "ui/message was not bounded text");
  check(
    continuation[0].text === "继续当前任务，并执行已启用的 ZDecision Recall。",
    "ui/message text changed",
  );
  check(!continuation[0].text.includes(attemptId), "ui/message exposed the attempt");
  const messageTimeout = widget.takeTimer(3000);
  if (messageTimeout) messageTimeout();
  await flush();
  check(widget.messages().length === 1, "ui/message timeout retried automatically");
  check(
    widget.elements.status.textContent.includes("下一条原生消息"),
    "card omitted the native-message fallback",
  );
  process.stdout.write("bounded-continuation-ok");
""",
        )
        self.assertEqual("bounded-continuation-ok", output)

    def test_decline_never_enables_or_requests_recall_continuation(self) -> None:
        output = self._run_card(
            """
  const widget = await mount();
  widget.deliverRender();
  await flush();
  const click = widget.elements.decline.dispatch("click");
  const decisions = widget.toolCalls();
  check(decisions.length === 1, "decline did not make one decision call");
  check(
    decisions[0].params?.arguments?.action === "decline",
    "decline sent an enable action",
  );
  widget.respond(decisions[0], result("declined"));
  await click;
  await flush();
  widget.deliverDeclinedNotification();
  await flush();
  check(
    widget.toolCalls().every((call) => call.params?.arguments?.action !== "enable"),
    "decline emitted enable",
  );
  check(widget.messages().length === 0, "decline requested Recall continuation");
  process.stdout.write("decline-is-terminal-ok");
""",
        )
        self.assertEqual("decline-is-terminal-ok", output)

    def test_decision_results_require_the_current_attempt_for_both_actions(
        self,
    ) -> None:
        output = self._run_card(
            """
  const cases = [
    { action: "enable", state: "committed", button: "enable" },
    { action: "decline", state: "declined", button: "decline" },
  ];
  const mutations = [
    { label: "missing", responseAttempt: null, includeAttempt: false },
    {
      label: "mismatch",
      responseAttempt: "activation_99999999999999999999999999999999",
      includeAttempt: true,
    },
  ];

  for (const item of cases) {
    for (const mutation of mutations) {
      const widget = await mount();
      widget.deliverRender();
      await flush();
      const click = widget.elements[item.button].dispatch("click");
      const decision = widget.toolCalls()[0];
      widget.respond(
        decision,
        result(item.state, mutation.responseAttempt, mutation.includeAttempt),
      );
      await click;
      await flush();
      check(
        widget.elements["card-state"].textContent === "无法确认",
        `${item.action}/${mutation.label} displayed terminal success`,
      );
      check(
        widget.messages().length === 0,
        `${item.action}/${mutation.label} requested continuation`,
      );
    }
  }
  process.stdout.write("attempt-bound-decisions-ok");
""",
        )
        self.assertEqual("attempt-bound-decisions-ok", output)

    def _run_card(self, scenario: str) -> str:
        self.assertTrue(WIDGET_PATH.is_file(), f"missing card: {WIDGET_PATH}")
        html = WIDGET_PATH.read_text("utf-8")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        harness = r"""
const vm = require("node:vm");
const shippedScript = __SHIPPED_SCRIPT__;
const attemptId = "activation_11111111111111111111111111111111";
const repositoryName = "zdecision";

function check(condition, message) {
  if (!condition) throw new Error(message);
}

async function flush() {
  for (let index = 0; index < 6; index += 1) await Promise.resolve();
}

function result(state, responseAttempt = attemptId, includeAttempt = true) {
  const meta = {
    "zdecision/repository_display_name": repositoryName,
  };
  if (includeAttempt) {
    meta["zdecision/activation_attempt_id"] = responseAttempt;
  }
  return {
    content: [{ type: "text", text: "bounded" }],
    structuredContent: { state },
    _meta: meta,
  };
}

async function mount() {
  const outbound = [];
  const timers = new Map();
  let nextTimerId = 1;
  let messageHandler = null;

  class Element {
    constructor() {
      this.disabled = false;
      this.hidden = false;
      this.textContent = "";
      this.listeners = new Map();
    }

    addEventListener(name, listener) {
      const listeners = this.listeners.get(name) || [];
      listeners.push(listener);
      this.listeners.set(name, listeners);
    }

    dispatch(name) {
      return Promise.all(
        (this.listeners.get(name) || []).map((listener) => listener()),
      );
    }
  }

  const elements = Object.fromEntries(
    ["enable", "decline", "repository", "status", "card-state"].map(
      (id) => [id, new Element()],
    ),
  );
  const host = { postMessage(message) { outbound.push(message); } };
  const sandbox = {
    document: { getElementById: (id) => elements[id] },
    window: {
      parent: host,
      addEventListener(name, listener) {
        if (name === "message") messageHandler = listener;
      },
    },
    setTimeout(callback, delay) {
      const id = nextTimerId++;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
  };
  vm.runInNewContext(shippedScript, sandbox);

  function deliver(message, source = host) {
    messageHandler({ source, data: message });
  }

  function respond(call, response) {
    deliver({ jsonrpc: "2.0", id: call.id, result: response });
  }

  function toolCalls() {
    return outbound.filter((message) => message.method === "tools/call");
  }

  function messages() {
    return outbound.filter((message) => message.method === "ui/message");
  }

  function takeTimer(delay) {
    const entry = [...timers.entries()].find(([, timer]) => timer.delay === delay);
    check(entry, `missing ${delay}ms timer`);
    timers.delete(entry[0]);
    return entry[1].callback;
  }

  function runAllTimers() {
    const callbacks = [...timers.values()].map((timer) => timer.callback);
    timers.clear();
    for (const callback of callbacks) callback();
  }

  function deliverRender() {
    deliver({
      jsonrpc: "2.0",
      method: "ui/notifications/tool-result",
      params: result("pending_confirmation"),
    });
  }

  function deliverCommittedNotification() {
    deliver({
      jsonrpc: "2.0",
      method: "ui/notifications/tool-result",
      params: result("committed"),
    });
  }

  function deliverDeclinedNotification() {
    deliver({
      jsonrpc: "2.0",
      method: "ui/notifications/tool-result",
      params: result("declined"),
    });
  }

  const initialize = outbound.find(
    (message) => message.method === "ui/initialize",
  );
  check(initialize, "card did not initialize the MCP Apps bridge");
  deliver({
    jsonrpc: "2.0",
    id: initialize.id,
    result: { hostCapabilities: { serverTools: {} } },
  });
  await flush();
  return {
    deliver,
    deliverRender,
    deliverCommittedNotification,
    deliverDeclinedNotification,
    elements,
    messages,
    outbound,
    respond,
    runAllTimers,
    takeTimer,
    toolCalls,
  };
}

(async () => {
__SCENARIO__
})().catch((error) => {
  process.stderr.write(error.stack || String(error));
  process.exitCode = 1;
});
"""
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
        return completed.stdout


if __name__ == "__main__":
    unittest.main()
