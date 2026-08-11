"""Behavior tests for the disposable Recall Gate A0 vertical."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from tests.integration import recall_gate_a0_disposable_harness as disposable
from zdecision.registry.models import DecisionRevision


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = (
    REPOSITORY_ROOT / "tests/integration/recall_gate_a0_disposable_harness.py"
)
PYTHON = REPOSITORY_ROOT / ".venv/bin/python"
PROTOCOL_VERSION = "recall-handoff-v1"
APPLICATION_INSTRUCTION = (
    "Classify every delivered Decision exactly once, then call "
    "apply_zdecision_gate_a0_delivery before development mutation."
)
LIVE_SESSION = "live-session-private-sentinel"
LIVE_RENDER_TURN = "live-render-turn-private-sentinel"
LIVE_APPLICATION_TURN = "live-application-turn-private-sentinel"
MODEL_SESSION = "model-session-must-be-discarded"
MODEL_TURN = "model-turn-must-be-discarded"
MODEL_CWD = "/model-authored/cwd/must-be-discarded"

FIXTURE_ONE_BYTES = (
    '{"claim":"Gate A0 fixture one requires server-authoritative handoff state.",'
    '"decision_id":"dec_11111111111111111111111111111111",'
    '"format":"zdecision-decision/v1",'
    '"future_action":"Keep the disposable delivery stable across remounts.",'
    '"invalidation_conditions":["The Gate A0 protocol version changes."],'
    '"lifecycle":"active",'
    '"product_id":"prod_4d7b16e1616dd4cd1aeb2411836fd687",'
    '"product_name":"安恒",'
    '"publication_preview_id":"pub_11111111111111111111111111111111",'
    '"review_approval":{"actor":"user",'
    '"recorded_at":"2026-08-10T00:00:00Z",'
    '"thread_id":"fixture-review-one",'
    '"turn_id":"fixture-review-turn-one"},'
    '"revision":1,"schema_version":1,'
    '"scope":{"paths":["tests/integration/"],'
    '"repositories":["https://example.invalid/zdecision-gate-a0.git"],'
    '"summary":"Disposable Gate A0 delivery behavior"},'
    '"source":{"thread_id":"fixture-source-one",'
    '"turn_id":"fixture-turn-one"},'
    '"supersedes":[],"variant_of":[]}\n'
)
FIXTURE_TWO_BYTES = (
    '{"claim":"Gate A0 fixture two limits application to validated classifications.",'
    '"decision_id":"dec_22222222222222222222222222222222",'
    '"format":"zdecision-decision/v1",'
    '"future_action":"Deny disposable mutation until application commits atomically.",'
    '"invalidation_conditions":["The Gate A0 application contract changes."],'
    '"lifecycle":"active",'
    '"product_id":"prod_4d7b16e1616dd4cd1aeb2411836fd687",'
    '"product_name":"安恒",'
    '"publication_preview_id":"pub_22222222222222222222222222222222",'
    '"review_approval":{"actor":"user",'
    '"recorded_at":"2026-08-10T00:01:00Z",'
    '"thread_id":"fixture-review-two",'
    '"turn_id":"fixture-review-turn-two"},'
    '"revision":1,"schema_version":1,'
    '"scope":{"paths":["tests/integration/"],'
    '"repositories":["https://example.invalid/zdecision-gate-a0.git"],'
    '"summary":"Disposable Gate A0 application guard"},'
    '"source":{"thread_id":"fixture-source-two",'
    '"turn_id":"fixture-turn-two"},'
    '"supersedes":[],"variant_of":[]}\n'
)
FIXTURE_ONE_DIGEST = "30dc189935dd11c1f9e87a900235dcc693479cbdf69106d139c2320d194ab63a"
FIXTURE_TWO_DIGEST = "4dfba3631e4a669ac024759525c868125111142aaeafb238fcad57e2af16c99a"
FIXTURE_ONE = json.loads(FIXTURE_ONE_BYTES)
FIXTURE_TWO = json.loads(FIXTURE_TWO_BYTES)


def _tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    if not path.exists():
        digest.update(b"missing")
        return digest.hexdigest()
    for member in sorted(path.rglob("*")):
        if member.is_file():
            digest.update(member.relative_to(path).as_posix().encode("utf-8"))
            digest.update(member.read_bytes())
    return digest.hexdigest()


class McpProcess:
    def __init__(self, root: Path, env: dict[str, str]) -> None:
        self.process = subprocess.Popen(
            [str(PYTHON), str(HARNESS_PATH), "mcp", "--root", str(root)],
            cwd=REPOSITORY_ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.responses: queue.Queue[str] = queue.Queue()
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()
        self.next_id = 1
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "gate-a0-test", "version": "1"},
            },
        )
        self.notify("notifications/initialized", {})

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.responses.put(line)

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        request_id = self.next_id
        self.next_id += 1
        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        try:
            line = self.responses.get(timeout=10)
        except queue.Empty:
            stderr = ""
            if self.process.poll() is not None and self.process.stderr is not None:
                stderr = self.process.stderr.read()
            self.close()
            raise AssertionError(f"MCP response timed out: {method}: {stderr}")
        response = json.loads(line)
        if response.get("id") != request_id:
            raise AssertionError(f"unexpected MCP response: {response}")
        if "error" in response:
            raise AssertionError(f"MCP request failed: {response['error']}")
        return response["result"]

    def notify(self, method: str, params: dict[str, object]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, value: dict[str, object]) -> None:
        if self.process.stdin is None:
            raise AssertionError("MCP stdin is closed")
        self.process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def call(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        if self.process.poll() is None:
            if self.process.stdin is not None:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
        self.reader.join(timeout=5)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


class DisposableRecallGateA0VerticalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.root = self.parent / "gate-a0-root"
        self.repository = REPOSITORY_ROOT.resolve()
        self.traps = self.parent / "production-traps"
        self.traps.mkdir()
        self.production_agent = self.traps / "agent.sqlite3"
        self.production_central = self.traps / "central.sqlite3"
        self.production_registry = self.traps / "registry.json"
        for path, content in (
            (self.production_agent, b"production-agent-sentinel"),
            (self.production_central, b"production-central-sentinel"),
            (self.production_registry, b"production-registry-sentinel"),
        ):
            path.write_bytes(content)
        self.env = dict(os.environ)
        self.env.update(
            {
                "ZDECISION_STATE_DIR": str(self.traps),
                "ZDECISION_DATABASE_PATH": str(self.production_agent),
                "ZDECISION_CENTRAL_DATABASE": str(self.production_central),
                "ZDECISION_REGISTRY_ROOT": str(self.production_registry),
                "PYTHONPATH": str(REPOSITORY_ROOT),
            }
        )
        self._run("create", "--repository", str(self.repository))

    def tearDown(self) -> None:
        if self.root.exists():
            self._run("cleanup")

    def _run(
        self, command: str, *extra: str, stdin: dict[str, object] | None = None
    ) -> dict[str, object]:
        completed = subprocess.run(
            [
                str(PYTHON),
                str(HARNESS_PATH),
                command,
                "--root",
                str(self.root),
                *extra,
            ],
            cwd=REPOSITORY_ROOT,
            env=self.env,
            input=(json.dumps(stdin) + "\n") if stdin is not None else None,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def _hook(
        self,
        tool_name: str,
        tool_input: dict[str, object],
        *,
        turn_id: str = LIVE_RENDER_TURN,
        session_id: str = LIVE_SESSION,
        cwd: Path | None = None,
    ) -> dict[str, object]:
        return self._run(
            "hook",
            stdin={
                "hook_event_name": "PreToolUse",
                "session_id": session_id,
                "turn_id": turn_id,
                "cwd": str(cwd or self.repository),
                "tool_name": tool_name,
                "tool_input": tool_input,
            },
        )

    @staticmethod
    def _updated_input(output: dict[str, object]) -> dict[str, object]:
        return output["hookSpecificOutput"]["updatedInput"]

    def _render_attempt(self) -> str:
        output = self._hook(
            "mcp__zdecision_gate_a0__show_zdecision_gate_a0",
            {
                "session_id": MODEL_SESSION,
                "turn_id": MODEL_TURN,
                "cwd": MODEL_CWD,
                "task_id": "model-task-must-be-discarded",
                "repository": "/model/repository/must-be-discarded",
            },
        )
        hook = output["hookSpecificOutput"]
        self.assertEqual("allow", hook["permissionDecision"])
        self.assertEqual({"attempt_id"}, set(hook["updatedInput"]))
        return hook["updatedInput"]["attempt_id"]

    def _enable_and_ack(self, client: McpProcess, attempt_id: str) -> dict[str, object]:
        enabled = client.call(
            "enable_zdecision_gate_a0_delivery", {"attempt_id": attempt_id}
        )
        structured = enabled["structuredContent"]
        client.call(
            "ack_zdecision_gate_a0_delivery",
            {"delivery_id": structured["snapshot"]["delivery_id"]},
        )
        return structured

    def test_render_hook_replaces_model_coordinates_with_one_trusted_binding(self) -> None:
        attempt_id = self._render_attempt()

        inspect = self._run("inspect")

        self.assertRegex(attempt_id, r"^attempt_[0-9a-f]{32}$")
        self.assertEqual(1, inspect["attempt_count"])
        self.assertEqual(0, inspect["delivery_count"])
        self.assertNotIn(LIVE_SESSION, json.dumps(inspect))
        self.assertNotIn(LIVE_RENDER_TURN, json.dumps(inspect))
        cross_task = self._hook(
            "mcp__zdecision_gate_a0__show_zdecision_gate_a0",
            {},
            session_id="untrusted-substitute-session",
            cwd=self.parent,
        )
        self.assertEqual(
            "deny", cross_task["hookSpecificOutput"]["permissionDecision"]
        )

    def test_enable_commits_one_stable_delivery_with_literal_canonical_fixtures(self) -> None:
        for fixture_bytes, digest in (
            (FIXTURE_ONE_BYTES, FIXTURE_ONE_DIGEST),
            (FIXTURE_TWO_BYTES, FIXTURE_TWO_DIGEST),
        ):
            self.assertEqual(digest, hashlib.sha256(fixture_bytes.encode()).hexdigest())
            self.assertEqual(
                json.loads(fixture_bytes),
                DecisionRevision.from_dict(json.loads(fixture_bytes)).to_dict(),
            )
        attempt_id = self._render_attempt()
        client = McpProcess(self.root, self.env)
        self.addCleanup(client.close)

        tools = client.request("tools/list", {})["tools"]
        by_name = {tool["name"]: tool for tool in tools}
        self.assertEqual(
            {
                "show_zdecision_gate_a0",
                "enable_zdecision_gate_a0_delivery",
                "ack_zdecision_gate_a0_delivery",
                "get_zdecision_gate_a0_status",
                "apply_zdecision_gate_a0_delivery",
                "increment_zdecision_gate_a0_counter",
            },
            set(by_name),
        )
        for app_only in (
            "enable_zdecision_gate_a0_delivery",
            "ack_zdecision_gate_a0_delivery",
            "get_zdecision_gate_a0_status",
        ):
            self.assertEqual(["app"], by_name[app_only]["_meta"]["ui"]["visibility"])
        rendered = client.call("show_zdecision_gate_a0", {"attempt_id": attempt_id})
        self.assertEqual("pending_confirmation", rendered["structuredContent"]["state"])

        first = client.call(
            "enable_zdecision_gate_a0_delivery", {"attempt_id": attempt_id}
        )["structuredContent"]
        replay = client.call(
            "enable_zdecision_gate_a0_delivery", {"attempt_id": attempt_id}
        )["structuredContent"]

        self.assertEqual(first, replay)
        self.assertEqual("context_prepared", first["state"])
        snapshot = first["snapshot"]
        self.assertEqual(PROTOCOL_VERSION, snapshot["protocol_version"])
        self.assertEqual(APPLICATION_INSTRUCTION, snapshot["application_instruction"])
        self.assertRegex(snapshot["delivery_id"], r"^delivery_[0-9a-f]{32}$")
        self.assertEqual(
            [
                {
                    "decision_id": "dec_11111111111111111111111111111111",
                    "revision": 1,
                    "digest": FIXTURE_ONE_DIGEST,
                    "decision_revision": FIXTURE_ONE,
                },
                {
                    "decision_id": "dec_22222222222222222222222222222222",
                    "revision": 1,
                    "digest": FIXTURE_TWO_DIGEST,
                    "decision_revision": FIXTURE_TWO,
                },
            ],
            snapshot["decisions"],
        )
        serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        for forbidden in (
            LIVE_SESSION,
            LIVE_RENDER_TURN,
            MODEL_SESSION,
            MODEL_TURN,
            MODEL_CWD,
            str(self.repository),
            "raw_prompt",
            "full_receipt",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertIn("fixture-source-one", serialized)
        inspect = self._run("inspect")
        self.assertEqual(1, inspect["delivery_count"])
        self.assertEqual(0, inspect["context_update_count"])

    def test_card_bridge_updates_context_once_never_messages_and_remounts_read_only(
        self,
    ) -> None:
        attempt_id = self._render_attempt()
        delivery_id = "delivery_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        receipt = "delivery_receipt_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        snapshot = {
            "protocol_version": PROTOCOL_VERSION,
            "delivery_id": delivery_id,
            "application_instruction": APPLICATION_INSTRUCTION,
            "decisions": [
                {
                    "decision_id": FIXTURE_ONE["decision_id"],
                    "revision": 1,
                    "digest": FIXTURE_ONE_DIGEST,
                    "decision_revision": FIXTURE_ONE,
                },
                {
                    "decision_id": FIXTURE_TWO["decision_id"],
                    "revision": 1,
                    "digest": FIXTURE_TWO_DIGEST,
                    "decision_revision": FIXTURE_TWO,
                },
            ],
        }
        card = self.root / "plugin/static/recall-gate-a0-v1.html"
        html = card.read_text("utf-8")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        scenario = r'''
const vm = require("node:vm");
const shippedScript = __SCRIPT__;
const attemptId = __ATTEMPT_ID__;
const snapshot = __SNAPSHOT__;
const receipt = __RECEIPT__;
function check(value, message) { if (!value) throw new Error(message); }
async function flush() { for (let i = 0; i < 12; i += 1) await Promise.resolve(); }
class Element {
  constructor() { this.disabled = false; this.textContent = ""; this.listeners = []; }
  addEventListener(name, listener) { if (name === "click") this.listeners.push(listener); }
  dispatch() { return Promise.all(this.listeners.map((listener) => listener())); }
}
async function mount(renderResult) {
  const outbound = [];
  let handler = null;
  let nextTimer = 1;
  const timers = new Map();
  const elements = Object.fromEntries([
    "gate-state", "gate-status", "gate-receipt", "enable-recall"
  ].map((id) => [id, new Element()]));
  const host = { postMessage(message) { outbound.push(message); } };
  const sandbox = {
    document: { getElementById(id) { return elements[id]; } },
    window: {
      parent: host,
      addEventListener(name, listener) { if (name === "message") handler = listener; },
    },
    setTimeout(callback) { const id = nextTimer++; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  vm.runInNewContext(shippedScript, sandbox);
  function deliver(data) { handler({ source: host, data }); }
  function respond(request, result) {
    deliver({ jsonrpc: "2.0", id: request.id, result });
  }
  const initialize = outbound.find((item) => item.method === "ui/initialize");
  check(initialize, "missing initialize");
  respond(initialize, { hostCapabilities: {
    serverTools: {}, updateModelContext: { text: {} }
  }});
  deliver({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: renderResult });
  await flush();
  return { outbound, elements, respond };
}
function result(state, extra = {}) {
  return {
    content: [{ type: "text", text: "bounded" }],
    structuredContent: { protocol_version: "recall-handoff-v1", state, ...extra },
    _meta: { "zdecision/attempt_id": attemptId },
  };
}
(async () => {
  const render = result("pending_confirmation");
  const first = await mount(render);
  const status = first.outbound.find((item) => item.method === "tools/call");
  check(status.params.name === "get_zdecision_gate_a0_status", "wrong status tool");
  first.respond(status, result("pending_confirmation"));
  await flush();
  const click = first.elements["enable-recall"].dispatch();
  await flush();
  const enable = first.outbound.filter((item) => item.method === "tools/call").at(-1);
  check(enable.params.name === "enable_zdecision_gate_a0_delivery", "wrong enable tool");
  first.respond(enable, result("context_prepared", { snapshot, receipt }));
  await flush();
  const updates = first.outbound.filter((item) => item.method === "ui/update-model-context");
  check(updates.length === 1, "context update count changed");
  check(JSON.stringify(updates[0].params.content) === JSON.stringify([
    { type: "text", text: JSON.stringify(snapshot) }
  ]), "context snapshot changed");
  first.respond(updates[0], {});
  await flush();
  const ack = first.outbound.filter((item) => item.method === "tools/call").at(-1);
  check(ack.params.name === "ack_zdecision_gate_a0_delivery", "missing ack");
  first.respond(ack, result("host_delivered", { receipt }));
  await click;
  await flush();
  check(first.outbound.filter((item) => item.method === "ui/update-model-context").length === 1,
    "context repeated");
  check(first.outbound.filter((item) => item.method === "ui/message").length === 0,
    "ui/message was used");
  await first.elements["enable-recall"].dispatch();
  check(first.outbound.filter((item) => item.params?.name === "enable_zdecision_gate_a0_delivery").length === 1,
    "duplicate click repeated enable");

  const remount = await mount(render);
  const recover = remount.outbound.find((item) => item.method === "tools/call");
  remount.respond(recover, result("host_delivered", { receipt }));
  await flush();
  check(remount.elements["gate-receipt"].textContent === receipt, "receipt not recovered");
  check(remount.outbound.filter((item) => item.method === "tools/call").length === 1,
    "remount mutated state");
  check(remount.outbound.filter((item) => item.method === "ui/update-model-context").length === 0,
    "remount repeated context");
  check(remount.outbound.filter((item) => item.method === "ui/message").length === 0,
    "remount sent message");
  process.stdout.write("bridge-pass");
})().catch((error) => { process.stderr.write(error.stack); process.exitCode = 1; });
'''
        scenario = scenario.replace("__SCRIPT__", json.dumps(script))
        scenario = scenario.replace("__ATTEMPT_ID__", json.dumps(attempt_id))
        scenario = scenario.replace("__SNAPSHOT__", json.dumps(snapshot, ensure_ascii=False))
        scenario = scenario.replace("__RECEIPT__", json.dumps(receipt))

        completed = subprocess.run(
            ["node", "-e", scenario],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("bridge-pass", completed.stdout)

    def test_application_hook_keeps_only_categories_and_guard_allows_one_mutation(
        self,
    ) -> None:
        attempt_id = self._render_attempt()
        client = McpProcess(self.root, self.env)
        self.addCleanup(client.close)
        enabled = self._enable_and_ack(client, attempt_id)
        delivery_id = enabled["snapshot"]["delivery_id"]
        classifications = [
            {
                "decision_id": FIXTURE_ONE["decision_id"],
                "revision": 1,
                "digest": FIXTURE_ONE_DIGEST,
                "classification": "applicable",
                "reason": "Directly governs the disposable handoff.",
                "session_id": MODEL_SESSION,
                "turn_id": MODEL_TURN,
            },
            {
                "decision_id": FIXTURE_TWO["decision_id"],
                "revision": 1,
                "digest": FIXTURE_TWO_DIGEST,
                "classification": "not_applicable",
                "reason": "Not needed by this bounded counter action.",
                "cwd": MODEL_CWD,
            },
        ]
        denied = self._hook(
            "mcp__zdecision_gate_a0__increment_zdecision_gate_a0_counter", {},
            turn_id=LIVE_APPLICATION_TURN,
        )
        self.assertEqual("deny", denied["hookSpecificOutput"]["permissionDecision"])
        self.assertTrue(denied["hookSpecificOutput"]["permissionDecisionReason"])

        binding = self._hook(
            "mcp__zdecision_gate_a0__apply_zdecision_gate_a0_delivery",
            {
                "delivery_id": "delivery_model_authored_must_be_discarded",
                "session_id": MODEL_SESSION,
                "turn_id": MODEL_TURN,
                "cwd": MODEL_CWD,
                "classifications": classifications,
                "unexpected": "discard me",
            },
            turn_id=LIVE_APPLICATION_TURN,
        )
        updated = self._updated_input(binding)

        self.assertEqual("allow", binding["hookSpecificOutput"]["permissionDecision"])
        self.assertEqual(delivery_id, updated["delivery_id"])
        self.assertRegex(updated["application_binding_id"], r"^application_[0-9a-f]{32}$")
        self.assertEqual(
            {
                "decision_id",
                "revision",
                "digest",
                "classification",
                "reason",
            },
            set(updated["classifications"][0]),
        )
        serialized = json.dumps(updated)
        for forbidden in (MODEL_SESSION, MODEL_TURN, MODEL_CWD, LIVE_SESSION):
            self.assertNotIn(forbidden, serialized)
        applied = client.call("apply_zdecision_gate_a0_delivery", updated)
        self.assertEqual("application_committed", applied["structuredContent"]["state"])
        self.assertEqual(1, applied["structuredContent"]["active_fixture_count"])

        permit = self._hook(
            "mcp__zdecision_gate_a0__increment_zdecision_gate_a0_counter", {},
            turn_id=LIVE_APPLICATION_TURN,
        )
        self.assertEqual("allow", permit["hookSpecificOutput"]["permissionDecision"])
        mutation_input = self._updated_input(permit)
        self.assertEqual({"mutation_id"}, set(mutation_input))
        incremented = client.call("increment_zdecision_gate_a0_counter", mutation_input)
        self.assertEqual(1, incremented["structuredContent"]["counter"])

        duplicate = self._hook(
            "mcp__zdecision_gate_a0__increment_zdecision_gate_a0_counter", {},
            turn_id=LIVE_APPLICATION_TURN,
        )
        self.assertEqual("deny", duplicate["hookSpecificOutput"]["permissionDecision"])
        inspect = self._run("inspect")
        self.assertEqual(1, inspect["application_count"])
        self.assertEqual(1, inspect["active_fixture_count"])
        self.assertEqual(1, inspect["mutation_count"])

    def test_restart_recovers_receipt_without_mutation_and_cleanup_is_isolated(self) -> None:
        protected = {
            "source": _tree_digest(REPOSITORY_ROOT / "src/zdecision"),
            "plugin": _tree_digest(REPOSITORY_ROOT / "plugins/zdecision"),
            "registry": _tree_digest(REPOSITORY_ROOT / "decision-registry"),
            "protected_doc": _tree_digest(
                REPOSITORY_ROOT
                / "docs/superpowers/acceptance/2026-08-06-recall-host-gate.md"
            ),
            "protected_test": _tree_digest(
                REPOSITORY_ROOT / "tests/integration/test_recall_host_gate.py"
            ),
        }
        trap_bytes = {
            path: path.read_bytes()
            for path in (
                self.production_agent,
                self.production_central,
                self.production_registry,
            )
        }
        attempt_id = self._render_attempt()
        first = McpProcess(self.root, self.env)
        enabled = self._enable_and_ack(first, attempt_id)
        receipt = enabled["receipt"]
        first.close()

        restarted = McpProcess(self.root, self.env)
        status = restarted.call(
            "get_zdecision_gate_a0_status", {"attempt_id": attempt_id}
        )["structuredContent"]
        child_check = subprocess.run(
            ["ps", "-o", "pid=", "--ppid", str(restarted.process.pid)],
            text=True,
            capture_output=True,
            check=False,
        )
        restarted.close()

        self.assertEqual("host_delivered", status["state"])
        self.assertEqual(receipt, status["receipt"])
        self.assertEqual("", child_check.stdout.strip(), "MCP server started a child")
        inspect = self._run("inspect")
        self.assertEqual(1, inspect["delivery_count"])
        self.assertEqual(1, inspect["context_update_count"])
        self.assertEqual(0, inspect["ui_message_count"])
        self.assertEqual(0, inspect["app_server_start_count"])
        database = sqlite3.connect(self.root / "state/gate-a0.sqlite3")
        self.addCleanup(database.close)
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for forbidden_table in (
            "candidates",
            "central",
            "registry",
            "capture_operations",
            "review_batches",
        ):
            self.assertNotIn(forbidden_table, tables)

        self._run("cleanup")

        self.assertFalse(self.root.exists())
        self.assertEqual(protected["source"], _tree_digest(REPOSITORY_ROOT / "src/zdecision"))
        self.assertEqual(protected["plugin"], _tree_digest(REPOSITORY_ROOT / "plugins/zdecision"))
        self.assertEqual(protected["registry"], _tree_digest(REPOSITORY_ROOT / "decision-registry"))
        self.assertEqual(
            protected["protected_doc"],
            _tree_digest(
                REPOSITORY_ROOT
                / "docs/superpowers/acceptance/2026-08-06-recall-host-gate.md"
            ),
        )
        self.assertEqual(
            protected["protected_test"],
            _tree_digest(REPOSITORY_ROOT / "tests/integration/test_recall_host_gate.py"),
        )
        for path, before in trap_bytes.items():
            self.assertEqual(before, path.read_bytes())

    def test_generated_plugin_is_disposable_interactive_and_decoupled(self) -> None:
        manifest = json.loads(
            (self.root / "plugin/.codex-plugin/plugin.json").read_text("utf-8")
        )
        mcp = json.loads((self.root / "plugin/.mcp.json").read_text("utf-8"))
        hooks = json.loads((self.root / "plugin/hooks/hooks.json").read_text("utf-8"))
        marketplace = json.loads(
            (self.root / "marketplace/.agents/plugins/marketplace.json").read_text(
                "utf-8"
            )
        )

        self.assertEqual("ZDecision Gate A0", manifest["interface"]["displayName"])
        self.assertEqual(["Interactive"], manifest["interface"]["capabilities"])
        self.assertRegex(manifest["name"], r"^zdecision-gate-a0-[0-9a-f]{8}$")
        self.assertEqual({"mcpServers"}, set(mcp))
        self.assertEqual(1, len(mcp["mcpServers"]))
        generated_server = next(iter(mcp["mcpServers"].values()))
        self.assertEqual(str(PYTHON), generated_server["command"])
        self.assertEqual({"PreToolUse"}, set(hooks["hooks"]))
        generated_hook = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn(str(PYTHON), generated_hook)
        self.assertEqual({"name", "interface", "plugins"}, set(marketplace))
        self.assertEqual(1, len(marketplace["plugins"]))
        self.assertEqual(manifest["name"], marketplace["plugins"][0]["name"])
        self.assertNotEqual(REPOSITORY_ROOT / "plugins/zdecision", self.root / "plugin")
        self.assertIsNotNone(disposable)

    def test_cleanup_refuses_a_marker_copied_to_another_root(self) -> None:
        wrong_root = self.parent / "wrong-root"
        wrong_root.mkdir()
        marker = self.root / ".zdecision-gate-a0-disposable.json"
        (wrong_root / marker.name).write_bytes(marker.read_bytes())
        sentinel = wrong_root / "must-survive"
        sentinel.write_text("bounded", "utf-8")

        completed = subprocess.run(
            [
                str(PYTHON),
                str(HARNESS_PATH),
                "cleanup",
                "--root",
                str(wrong_root),
            ],
            cwd=REPOSITORY_ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertEqual("bounded", sentinel.read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
