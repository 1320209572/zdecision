"""Tests for the disposable Recall MCP Apps host-capability probe."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from collections import deque
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from zdecision.agent.host_capability_probe import HostCapabilityProbeStore
from zdecision.agent.host_capability_probe_mcp import (
    HOST_PROBE_MIME_TYPE,
    HOST_PROBE_PATH,
    HOST_PROBE_URI,
    create_host_probe_mcp_server,
)


NOW = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)


async def call_tool(
    server: FastMCP,
    name: str,
    arguments: dict[str, object],
) -> CallToolResult:
    return await server.call_tool(name, arguments)


class MutableClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class TokenSequence:
    def __init__(self, *values: str) -> None:
        self.values = deque(values)

    def __call__(self) -> str:
        return self.values.popleft()


class HostCapabilityProbeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = (
            Path(self.temporary_directory.name) / "host-probe" / "probe.sqlite3"
        )
        self.clock = MutableClock()
        self.tokens = TokenSequence(
            "a" * 32,
            "b" * 32,
            "c" * 32,
            "d" * 32,
            "e" * 32,
            "f" * 32,
            "g" * 32,
            "h" * 32,
            "i" * 32,
        )
        self.store = HostCapabilityProbeStore.open(
            self.database_path,
            clock=self.clock,
            token=self.tokens,
        )
        self.addCleanup(self.store.close)

    def test_create_commit_and_replay_return_one_authoritative_receipt(self) -> None:
        created = self.store.create()

        committed = self.store.commit(created.probe_id)
        replay = self.store.commit(created.probe_id)

        self.assertEqual("ready", created.state)
        self.assertIsNotNone(committed)
        self.assertEqual("committed", committed.state)
        self.assertEqual(committed, replay)
        self.assertEqual(created.marker, committed.marker)
        self.assertEqual(created.receipt, committed.receipt)
        self.assertEqual(
            NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            committed.committed_at,
        )

    def test_reopen_recovers_committed_probe(self) -> None:
        created = self.store.create()
        committed = self.store.commit(created.probe_id)
        self.store.close()

        reopened = HostCapabilityProbeStore.open(
            self.database_path,
            clock=self.clock,
            token=self.tokens,
        )
        self.addCleanup(reopened.close)

        self.assertEqual(committed, reopened.get(created.probe_id))

    def test_unknown_malformed_and_expired_ids_do_not_commit(self) -> None:
        self.assertIsNone(self.store.commit("not-a-probe"))
        self.assertIsNone(self.store.commit("probe_" + "!" * 32))
        created = self.store.create()
        self.clock.now = datetime.fromisoformat(
            created.expires_at.replace("Z", "+00:00")
        ) + timedelta(seconds=1)

        expired = self.store.get(created.probe_id)

        self.assertIsNotNone(expired)
        self.assertEqual("expired", expired.state)
        self.assertIsNone(self.store.commit(created.probe_id))
        self.assertEqual("expired", self.store.get(created.probe_id).state)

    def test_store_bytes_exclude_business_sentinels(self) -> None:
        self.store.create()

        serialized = self.database_path.read_bytes().lower()

        for sentinel in (
            b"private_prompt_sentinel",
            b"private_transcript_sentinel",
            b"private_decision_sentinel",
            b"private_repository_sentinel",
        ):
            self.assertNotIn(sentinel, serialized)

    def test_two_connections_commit_once_and_replay_the_same_timestamp(self) -> None:
        created = self.store.create()
        other_clock = MutableClock(NOW + timedelta(seconds=5))
        other = HostCapabilityProbeStore.open(
            self.database_path,
            clock=other_clock,
            token=TokenSequence("j" * 32, "k" * 32, "l" * 32),
        )
        self.addCleanup(other.close)

        first = other.commit(created.probe_id)
        self.clock.now = NOW + timedelta(seconds=10)
        replay = self.store.commit(created.probe_id)

        self.assertEqual(first, replay)
        self.assertEqual(
            (NOW + timedelta(seconds=5))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            replay.committed_at,
        )

    def test_clock_must_return_a_utc_aware_datetime(self) -> None:
        for invalid_now in (
            datetime(2026, 8, 10, 0, 0),
            datetime(2026, 8, 10, 1, 0, tzinfo=timezone(timedelta(hours=1))),
            "2026-08-10T00:00:00Z",
        ):
            with self.subTest(invalid_now=invalid_now):
                store = HostCapabilityProbeStore.open(
                    Path(self.temporary_directory.name)
                    / f"invalid-{len(str(invalid_now))}"
                    / "probe.sqlite3",
                    clock=lambda invalid_now=invalid_now: invalid_now,
                    token=TokenSequence("m" * 32, "n" * 32, "o" * 32),
                )
                self.addCleanup(store.close)
                with self.assertRaises(ValueError):
                    store.create()

    def test_duplicate_generated_id_is_retried_without_overwriting(self) -> None:
        first = self.store.create()
        duplicate_then_fresh = TokenSequence(
            "a" * 32,
            "d" * 32,
            "e" * 32,
            "f" * 32,
            "g" * 32,
            "h" * 32,
        )
        other = HostCapabilityProbeStore.open(
            self.database_path,
            clock=self.clock,
            token=duplicate_then_fresh,
        )
        self.addCleanup(other.close)

        second = other.create()

        self.assertEqual("probe_" + "a" * 32, first.probe_id)
        self.assertEqual("probe_" + "f" * 32, second.probe_id)
        self.assertEqual(first, self.store.get(first.probe_id))

    def test_closed_store_rejects_all_operations(self) -> None:
        created = self.store.create()
        self.store.close()

        for operation in (
            self.store.create,
            lambda: self.store.get(created.probe_id),
            lambda: self.store.commit(created.probe_id),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(RuntimeError):
                    operation()

    def test_database_and_parent_are_owner_only(self) -> None:
        self.assertEqual(0o700, os.stat(self.database_path.parent).st_mode & 0o777)
        self.assertEqual(0o600, os.stat(self.database_path).st_mode & 0o777)

    def test_generated_coordinates_have_exact_bounded_shapes(self) -> None:
        created = self.store.create()

        self.assertEqual(38, len(created.probe_id))
        self.assertEqual(53, len(created.marker))
        self.assertEqual(40, len(created.receipt))
        self.assertRegex(created.probe_id, r"^probe_[A-Za-z0-9_-]{32}$")
        self.assertRegex(created.marker, r"^ZDECISION_HOST_PROBE_[A-Za-z0-9_-]{32}$")
        self.assertRegex(created.receipt, r"^receipt_[A-Za-z0-9_-]{32}$")
        self.assertEqual(1, created.probe_version)
        self.assertEqual(
            (NOW + timedelta(hours=24))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            created.expires_at,
        )

    def test_schema_contains_only_the_probe_table(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }

        self.assertEqual({"recall_host_capability_probes"}, tables)


class IsolatedHostProbeMcpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.database_path = Path(temporary_directory.name) / "probe.sqlite3"
        self.store = HostCapabilityProbeStore.open(
            self.database_path,
            clock=lambda: NOW,
            token=TokenSequence(
                "a" * 32,
                "b" * 32,
                "c" * 32,
                "d" * 32,
                "e" * 32,
                "f" * 32,
            ),
        )
        self.addCleanup(self.store.close)
        self.server = create_host_probe_mcp_server(self.store)

    async def test_server_registers_one_resource_and_exact_tool_visibility(
        self,
    ) -> None:
        resources = await self.server.list_resources()
        tools = {tool.name: tool for tool in await self.server.list_tools()}

        self.assertEqual([HOST_PROBE_URI], [str(item.uri) for item in resources])
        self.assertEqual(HOST_PROBE_MIME_TYPE, resources[0].mimeType)
        self.assertEqual(
            {
                "show_zdecision_recall_host_probe",
                "run_zdecision_recall_host_probe",
                "get_zdecision_recall_host_probe",
            },
            set(tools),
        )
        self.assertEqual(
            ["model", "app"],
            tools["show_zdecision_recall_host_probe"].meta["ui"][
                "visibility"
            ],
        )
        for name in (
            "run_zdecision_recall_host_probe",
            "get_zdecision_recall_host_probe",
        ):
            self.assertEqual(["app"], tools[name].meta["ui"]["visibility"])

        show = tools["show_zdecision_recall_host_probe"]
        run = tools["run_zdecision_recall_host_probe"]
        get = tools["get_zdecision_recall_host_probe"]
        self.assertEqual({}, show.inputSchema["properties"])
        self.assertEqual([], show.inputSchema.get("required", []))
        self.assertEqual({"probe_id"}, set(run.inputSchema["properties"]))
        self.assertEqual(["probe_id"], run.inputSchema["required"])
        self.assertEqual({"probe_id"}, set(get.inputSchema["properties"]))
        self.assertEqual(["probe_id"], get.inputSchema["required"])
        for tool in tools.values():
            self.assertFalse(tool.inputSchema.get("additionalProperties", True))
            self.assertFalse(tool.annotations.destructiveHint)
            self.assertFalse(tool.annotations.openWorldHint)
        self.assertFalse(show.annotations.readOnlyHint)
        self.assertFalse(show.annotations.idempotentHint)
        self.assertFalse(run.annotations.readOnlyHint)
        self.assertTrue(run.annotations.idempotentHint)
        self.assertTrue(get.annotations.readOnlyHint)
        self.assertTrue(get.annotations.idempotentHint)

    async def test_marker_is_app_private_and_commit_replays_same_receipt(
        self,
    ) -> None:
        render = await call_tool(
            self.server, "show_zdecision_recall_host_probe", {}
        )
        probe_id = render.meta["zdecision/probe_id"]

        first = await call_tool(
            self.server,
            "run_zdecision_recall_host_probe",
            {"probe_id": probe_id},
        )
        second = await call_tool(
            self.server,
            "run_zdecision_recall_host_probe",
            {"probe_id": probe_id},
        )

        self.assertFalse(render.isError)
        self.assertEqual(
            {"probe_version": 1, "state": "ready"},
            render.structuredContent,
        )
        self.assertNotIn(
            "ZDECISION_HOST_PROBE_", json.dumps(render.structuredContent)
        )
        self.assertEqual(first.structuredContent, second.structuredContent)
        self.assertEqual(first.meta, second.meta)
        marker = first.meta["zdecision/probe_marker"]
        model_visible = json.dumps(
            {
                "content": [item.model_dump() for item in first.content],
                "structuredContent": first.structuredContent,
            },
            sort_keys=True,
        )
        self.assertNotIn(marker, model_visible)

        recovered = await call_tool(
            self.server,
            "get_zdecision_recall_host_probe",
            {"probe_id": probe_id},
        )
        self.assertEqual(first.structuredContent, recovered.structuredContent)
        self.assertEqual(first.meta, recovered.meta)

    async def test_invalid_probe_is_an_unambiguous_closed_error(self) -> None:
        for name in (
            "run_zdecision_recall_host_probe",
            "get_zdecision_recall_host_probe",
        ):
            with self.subTest(name=name):
                result = await call_tool(
                    self.server,
                    name,
                    {"probe_id": "probe_" + "9" * 32},
                )
                self.assertTrue(result.isError)
                self.assertEqual(
                    {
                        "probe_version": 1,
                        "state": "failed",
                        "code": "invalid_probe",
                    },
                    result.structuredContent,
                )
                self.assertEqual({}, result.meta)
                self.assertIn("unavailable", result.content[0].text.lower())
                self.assertNotIn("ready", result.content[0].text.lower())

    async def test_tools_expose_no_business_coordinate_keys(self) -> None:
        render = await call_tool(
            self.server, "show_zdecision_recall_host_probe", {}
        )
        probe_id = render.meta["zdecision/probe_id"]
        results = [
            render,
            await call_tool(
                self.server,
                "run_zdecision_recall_host_probe",
                {"probe_id": probe_id},
            ),
        ]
        forbidden = (
            '"session_id"',
            '"turn_id"',
            '"repository_id"',
            '"product_id"',
            '"prompt"',
            '"decision_id"',
            '"candidate_id"',
            '"cwd"',
            "/users/",
        )
        for result in results:
            serialized = json.dumps(
                {
                    "content": [item.model_dump() for item in result.content],
                    "structuredContent": result.structuredContent,
                    "_meta_keys": sorted(result.meta),
                },
                sort_keys=True,
            ).lower()
            for value in forbidden:
                with self.subTest(value=value):
                    self.assertNotIn(value, serialized)


class HostProbeCardProtocolTests(unittest.TestCase):
    def test_success_uses_the_exact_order_and_keeps_marker_out_of_message(
        self,
    ) -> None:
        output = self._run_card(
            r"""
  const widget = await mount({
    hostCapabilities: {
      serverTools: {},
      updateModelContext: { text: {} },
      message: { text: {} },
    },
    renderResult: readyResult,
  });
  check(widget.outbound("ui/notifications/initialized").length === 1,
    "initialized notification missing");
  const get = widget.outbound("tools/call")[0];
  check(get.params.name === "get_zdecision_recall_host_probe",
    "mount did not use read-only recovery");
  widget.respond(get, readyResult);
  await flush();

  const click = widget.clickRun();
  await flush();
  const run = widget.outbound("tools/call").at(-1);
  check(run.params.name === "run_zdecision_recall_host_probe", "wrong action");
  check(run.params.arguments.probe_id === probeId, "wrong probe id");
  widget.respond(run, committedResult);
  await flush();

  const update = widget.outbound("ui/update-model-context")[0];
  check(update.params.content.length === 1, "context was fragmented");
  check(update.params.content[0].text.includes(marker), "marker not staged");
  check(widget.outbound("ui/message").length === 0,
    "message raced ahead of context acknowledgement");
  widget.respond(update, {});
  await flush();

  const message = widget.outbound("ui/message")[0];
  check(message.params.role === "user", "wrong message role");
  check(Array.isArray(message.params.content), "message content is not an array");
  check(message.params.content.length === 1, "message was not bounded");
  check(!message.params.content[0].text.includes(marker), "message leaked marker");
  widget.respond(message, { isError: false });
  await click;
  check(widget.element("probe-state").textContent === "验证完成",
    "success state missing");
  process.stdout.write("ordered-success");
""",
        )
        self.assertEqual("ordered-success", output)

    def test_missing_or_malformed_capabilities_are_unsupported(self) -> None:
        output = self._run_card(
            r"""
  const cases = [
    {},
    { serverTools: true },
    { serverTools: [], updateModelContext: { text: {} }, message: { text: {} } },
    { serverTools: {}, updateModelContext: { text: true }, message: { text: {} } },
    { serverTools: {}, updateModelContext: { text: {} }, message: { text: true } },
  ];
  for (const hostCapabilities of cases) {
    const widget = await mount({ hostCapabilities, renderResult: readyResult });
    check(widget.outbound("tools/call").length === 0,
      "unsupported host still called a tool");
    check(widget.element("probe-state").textContent === "宿主不支持",
      "unsupported state missing");
    check(widget.element("run-probe").disabled, "unsupported action enabled");
  }
  process.stdout.write("unsupported-is-closed");
""",
        )
        self.assertEqual("unsupported-is-closed", output)

    def test_partial_capabilities_and_failed_requests_do_not_fall_through(
        self,
    ) -> None:
        output = self._run_card(
            r"""
  const noContext = await readyWidget({ serverTools: {} });
  let click = noContext.clickRun();
  await flush();
  noContext.respond(noContext.outbound("tools/call").at(-1), committedResult);
  await click;
  check(noContext.outbound("ui/update-model-context").length === 0,
    "context request sent without capability");
  check(noContext.outbound("ui/message").length === 0,
    "message sent without context");
  check(noContext.element("probe-state").textContent === "部分支持",
    "missing context was not partial");

  const noMessage = await readyWidget({
    serverTools: {}, updateModelContext: { text: {} },
  });
  click = noMessage.clickRun();
  await flush();
  noMessage.respond(noMessage.outbound("tools/call").at(-1), committedResult);
  await flush();
  noMessage.respond(noMessage.outbound("ui/update-model-context")[0], {});
  await click;
  check(noMessage.outbound("ui/message").length === 0,
    "message request sent without capability");
  check(noMessage.element("probe-state").textContent === "部分支持",
    "missing message was not partial");

  const contextError = await readyWidget({
    serverTools: {},
    updateModelContext: { text: {} },
    message: { text: {} },
  });
  click = contextError.clickRun();
  await flush();
  contextError.respond(
    contextError.outbound("tools/call").at(-1), committedResult,
  );
  await flush();
  contextError.reject(
    contextError.outbound("ui/update-model-context")[0],
    { code: -32000, message: "rejected" },
  );
  await click;
  check(contextError.outbound("ui/message").length === 0,
    "context failure still sent a message");
  check(contextError.outbound("ui/update-model-context").length === 1,
    "context failure retried");

  const contextTimeout = await readyWidget({
    serverTools: {},
    updateModelContext: { text: {} },
    message: { text: {} },
  });
  click = contextTimeout.clickRun();
  await flush();
  contextTimeout.respond(
    contextTimeout.outbound("tools/call").at(-1), committedResult,
  );
  await flush();
  contextTimeout.expireLatest();
  await click;
  check(contextTimeout.outbound("ui/update-model-context").length === 1,
    "context timeout retried");
  check(contextTimeout.outbound("ui/message").length === 0,
    "context timeout still sent a message");

  const messageError = await readyWidget({
    serverTools: {},
    updateModelContext: { text: {} },
    message: { text: {} },
  });
  click = messageError.clickRun();
  await flush();
  messageError.respond(
    messageError.outbound("tools/call").at(-1), committedResult,
  );
  await flush();
  messageError.respond(messageError.outbound("ui/update-model-context")[0], {});
  await flush();
  messageError.respond(messageError.outbound("ui/message")[0], { isError: true });
  await click;
  check(messageError.outbound("ui/message").length === 1,
    "message failure retried");
  check(messageError.element("probe-state").textContent === "后续消息失败",
    "message failure state missing");

  const messageTimeout = await readyWidget({
    serverTools: {},
    updateModelContext: { text: {} },
    message: { text: {} },
  });
  click = messageTimeout.clickRun();
  await flush();
  messageTimeout.respond(
    messageTimeout.outbound("tools/call").at(-1), committedResult,
  );
  await flush();
  messageTimeout.respond(
    messageTimeout.outbound("ui/update-model-context")[0], {},
  );
  await flush();
  messageTimeout.expireLatest();
  await click;
  check(messageTimeout.outbound("ui/message").length === 1,
    "message timeout retried");
  check(messageTimeout.element("probe-state").textContent === "后续消息失败",
    "message timeout state missing");
  process.stdout.write("partial-and-failure-closed");
""",
        )
        self.assertEqual("partial-and-failure-closed", output)

    def test_timeout_duplicate_click_and_remount_never_repeat_mutation(
        self,
    ) -> None:
        output = self._run_card(
            r"""
  const timed = await readyWidget({
    serverTools: {},
    updateModelContext: { text: {} },
    message: { text: {} },
  });
  const firstClick = timed.clickRun();
  const duplicateClick = timed.clickRun();
  await flush();
  check(timed.outbound("tools/call").filter(
    (item) => item.params.name === "run_zdecision_recall_host_probe"
  ).length === 1, "duplicate click repeated mutation");
  timed.expireLatest();
  await Promise.all([firstClick, duplicateClick]);
  await flush();
  check(timed.outbound("tools/call").filter(
    (item) => item.params.name === "run_zdecision_recall_host_probe"
  ).length === 1, "action timeout retried mutation");
  check(timed.element("probe-state").textContent === "结果未知",
    "unknown action result missing");

  const restored = await mount({
    hostCapabilities: {
      serverTools: {},
      updateModelContext: { text: {} },
      message: { text: {} },
    },
    renderResult: readyResult,
  });
  const get = restored.outbound("tools/call")[0];
  restored.respond(get, committedResult);
  await flush();
  check(restored.outbound("tools/call").length === 1,
    "remount did more than one read-only get");
  check(restored.outbound("ui/update-model-context").length === 0,
    "remount repeated context update");
  check(restored.outbound("ui/message").length === 0,
    "remount repeated message");
  check(restored.element("probe-receipt").textContent.includes("receipt_"),
    "receipt was not restored");
  process.stdout.write("no-mutation-retry");
""",
        )
        self.assertEqual("no-mutation-retry", output)

    def test_mismatched_committed_identity_fails_closed(self) -> None:
        output = self._run_card(
            r"""
  const widget = await readyWidget({
    serverTools: {},
    updateModelContext: { text: {} },
    message: { text: {} },
  });
  const click = widget.clickRun();
  await flush();
  const mismatch = result({
    probe_version: 1,
    state: "committed",
    receipt: "receipt_cccccccccccccccccccccccccccccccc",
    committed_at: "2026-08-10T00:00:00.000000Z",
  }, {
    "zdecision/probe_id": "probe_99999999999999999999999999999999",
    "zdecision/probe_marker": marker,
  });
  widget.respond(widget.outbound("tools/call").at(-1), mismatch);
  await click;
  check(widget.element("probe-state").textContent === "验证失败",
    "mismatched identity was accepted");
  check(widget.outbound("ui/update-model-context").length === 0,
    "mismatched identity updated context");
  process.stdout.write("identity-bound");
""",
        )
        self.assertEqual("identity-bound", output)

    def _run_card(self, scenario: str) -> str:
        self.assertTrue(HOST_PROBE_PATH.is_file(), f"missing card: {HOST_PROBE_PATH}")
        html = HOST_PROBE_PATH.read_text("utf-8")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        harness = r"""
const vm = require("node:vm");
const shippedScript = __SHIPPED_SCRIPT__;
const probeId = "probe_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const marker = "ZDECISION_HOST_PROBE_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

function check(condition, message) {
  if (!condition) throw new Error(message);
}

async function flush() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
}

function result(structuredContent, meta = {}) {
  return {
    content: [{ type: "text", text: "bounded" }],
    structuredContent,
    _meta: meta,
  };
}

const readyResult = result({ probe_version: 1, state: "ready" }, {
  "zdecision/probe_id": probeId,
});
const committedResult = result({
  probe_version: 1,
  state: "committed",
  receipt: "receipt_cccccccccccccccccccccccccccccccc",
  committed_at: "2026-08-10T00:00:00.000000Z",
}, {
  "zdecision/probe_id": probeId,
  "zdecision/probe_marker": marker,
});

async function mount({ hostCapabilities, renderResult }) {
  const messages = [];
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

  const ids = [
    "cap-server-tools", "cap-update-context", "cap-message",
    "probe-state", "probe-receipt", "probe-status", "run-probe",
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, new Element()]));
  const host = { postMessage(message) { messages.push(message); } };
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

  function deliver(message) {
    messageHandler({ source: host, data: message });
  }
  function respond(request, response) {
    deliver({ jsonrpc: "2.0", id: request.id, result: response });
  }
  function reject(request, error) {
    deliver({ jsonrpc: "2.0", id: request.id, error });
  }
  function outbound(method) {
    return messages.filter((message) => message.method === method);
  }
  function expireLatest() {
    const entries = [...timers.entries()];
    check(entries.length > 0, "no pending timeout");
    const [id, timer] = entries.at(-1);
    timers.delete(id);
    timer.callback();
  }

  const initialize = outbound("ui/initialize")[0];
  check(initialize, "card did not initialize");
  check(initialize.params.protocolVersion === "2026-01-26",
    "wrong protocol version");
  respond(initialize, { hostCapabilities });
  deliver({
    jsonrpc: "2.0",
    method: "ui/notifications/tool-result",
    params: renderResult,
  });
  await flush();
  return {
    clickRun: () => elements["run-probe"].dispatch("click"),
    element: (id) => elements[id],
    expireLatest,
    outbound,
    reject,
    respond,
  };
}

async function readyWidget(hostCapabilities) {
  const widget = await mount({ hostCapabilities, renderResult: readyResult });
  const get = widget.outbound("tools/call")[0];
  check(get.params.name === "get_zdecision_recall_host_probe",
    "ready mount did not recover");
  widget.respond(get, readyResult);
  await flush();
  return widget;
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
