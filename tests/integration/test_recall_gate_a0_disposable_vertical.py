"""Behavior tests for the disposable Recall Gate A0 vertical."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import queue
import re
import shlex
import sqlite3
import subprocess
import tempfile
import threading
import time
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
    "1. Use only the typed intent and delivered Decisions. "
    "2. Call the disposable counter before application and observe denial. "
    "3. Submit complete classifications once. "
    "4. Call the disposable counter once after application_committed and observe "
    "counter == 1. "
    "5. Do not call shell, search, file-read, status, or render tools. "
    "6. Do not guess host-owned identifiers."
)
TEST_INTENT = {
    "target_decision_space_ids": ["prod_4d7b16e1616dd4cd1aeb2411836fd687"],
    "explicit_multi_space": False,
    "feature_goal": "Validate Recall handoff for the security-services application",
    "domain_objects": ["security-services", "Recall handoff"],
    "repository_relative_paths": [
        "packages/products/third-party-services/apps/security-services/"
    ],
    "constraints": ["Apply only Decisions governing this feature scope"],
    "exclusions": ["backup-services"],
}
LIVE_SESSION = "7live-session-private-sentinel"
LIVE_RENDER_TURN = "8live-render-turn-private-sentinel"
LIVE_APPLICATION_TURN = "9live-application-turn-private-sentinel"
MODEL_SESSION = "model-session-must-be-discarded"
MODEL_TURN = "model-turn-must-be-discarded"
MODEL_CWD = "/model-authored/cwd/must-be-discarded"

FIXTURE_ONE_BYTES = (
    '{"claim":"Security-services Recall handoff applies only to security-services feature work.",'
    '"decision_id":"dec_11111111111111111111111111111111",'
    '"format":"zdecision-decision/v1",'
    '"future_action":"Use this Decision when validating Recall handoff for security-services.",'
    '"invalidation_conditions":["The security-services Recall handoff contract changes."],'
    '"lifecycle":"active",'
    '"product_id":"prod_4d7b16e1616dd4cd1aeb2411836fd687",'
    '"product_name":"安恒",'
    '"publication_preview_id":"pub_11111111111111111111111111111111",'
    '"review_approval":{"actor":"user",'
    '"recorded_at":"2026-08-10T00:00:00Z",'
    '"thread_id":"fixture-review-one",'
    '"turn_id":"fixture-review-turn-one"},'
    '"revision":1,"schema_version":1,'
    '"scope":{"paths":["packages/products/third-party-services/apps/security-services/"],'
    '"repositories":["https://example.invalid/zdecision-gate-a0.git"],'
    '"summary":"Security-services Recall handoff validation"},'
    '"source":{"thread_id":"fixture-source-one",'
    '"turn_id":"fixture-turn-one"},'
    '"supersedes":[],"variant_of":[]}\n'
)
FIXTURE_TWO_BYTES = (
    '{"claim":"Backup-services Recall handoff applies only to backup-services feature work.",'
    '"decision_id":"dec_22222222222222222222222222222222",'
    '"format":"zdecision-decision/v1",'
    '"future_action":"Apply backup-services-specific Recall handoff procedures only to backup-services.",'
    '"invalidation_conditions":["The backup-services Recall handoff contract changes."],'
    '"lifecycle":"active",'
    '"product_id":"prod_4d7b16e1616dd4cd1aeb2411836fd687",'
    '"product_name":"安恒",'
    '"publication_preview_id":"pub_22222222222222222222222222222222",'
    '"review_approval":{"actor":"user",'
    '"recorded_at":"2026-08-10T00:01:00Z",'
    '"thread_id":"fixture-review-two",'
    '"turn_id":"fixture-review-turn-two"},'
    '"revision":1,"schema_version":1,'
    '"scope":{"paths":["packages/products/third-party-services/apps/backup-services/"],'
    '"repositories":["https://example.invalid/zdecision-gate-a0.git"],'
    '"summary":"Backup-services Recall handoff validation"},'
    '"source":{"thread_id":"fixture-source-two",'
    '"turn_id":"fixture-turn-two"},'
    '"supersedes":[],"variant_of":[]}\n'
)
FIXTURE_ONE_DIGEST = "e6400d33b97e9281e407abfe5825e9be93501f42cea06e93ca90081f280e0696"
FIXTURE_TWO_DIGEST = "64145131255fd0647b9d44d079a30829dc30c4a907855d33bbcae72f5ec2326e"
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


def _generated_plugin_root(root: Path) -> Path:
    marketplace_root = root / "marketplace"
    marketplace = json.loads(
        (marketplace_root / ".agents/plugins/marketplace.json").read_text("utf-8")
    )
    source = marketplace["plugins"][0]["source"]["path"]
    return (marketplace_root / source).resolve()


class McpProcess:
    def __init__(self, root: Path, env: dict[str, str]) -> None:
        mcp = json.loads(
            (_generated_plugin_root(root) / ".mcp.json").read_text("utf-8")
        )
        server = next(iter(mcp["mcpServers"].values()))
        self.process = subprocess.Popen(
            [server["command"], *server["args"]],
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


class GateA0DisposableVerticalTests(unittest.TestCase):
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
        self.mcp_processes: list[McpProcess] = []
        self._run("create", "--repository", str(self.repository))

    def tearDown(self) -> None:
        for process in reversed(self.mcp_processes):
            process.close()
        if self.root.exists():
            self._run("cleanup")

    def _mcp(self, root: Path | None = None) -> McpProcess:
        process = McpProcess(root or self.root, self.env)
        self.mcp_processes.append(process)
        return process

    def _run(
        self, command: str, *extra: str, stdin: dict[str, object] | None = None
    ) -> dict[str, object]:
        return self._run_at(self.root, command, *extra, stdin=stdin)

    def _run_at(
        self,
        root: Path,
        command: str,
        *extra: str,
        stdin: dict[str, object] | None = None,
    ) -> dict[str, object]:
        completed = subprocess.run(
            [
                str(PYTHON),
                str(HARNESS_PATH),
                command,
                "--root",
                str(root),
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
        hooks = json.loads(
            (_generated_plugin_root(self.root) / "hooks/hooks.json").read_text(
                "utf-8"
            )
        )
        command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        completed = subprocess.run(
            shlex.split(command),
            cwd=REPOSITORY_ROOT,
            env=self.env,
            input=json.dumps(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "cwd": str(cwd or self.repository),
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                }
            )
            + "\n",
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

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

    def test_model_visible_show_needs_no_model_supplied_opaque_id(self) -> None:
        client = self._mcp()
        tools = client.request("tools/list", {})["tools"]
        show = next(
            tool for tool in tools if tool["name"] == "show_zdecision_gate_a0"
        )

        self.assertNotIn("attempt_id", show["inputSchema"].get("required", []))
        rejected = client.call("show_zdecision_gate_a0", {})
        self.assertEqual("failed", rejected["structuredContent"]["state"])
        self.assertEqual("invalid_attempt", rejected["structuredContent"]["code"])

        binding = self._hook(
            "mcp__zdecision_gate_a0__show_zdecision_gate_a0",
            {},
        )
        updated = self._updated_input(binding)
        self.assertEqual({"attempt_id"}, set(updated))
        rendered = client.call("show_zdecision_gate_a0", updated)
        self.assertEqual(
            "pending_confirmation", rendered["structuredContent"]["state"]
        )

    def test_public_application_and_counter_schemas_are_model_complete(self) -> None:
        client = self._mcp()
        tools = client.request("tools/list", {})["tools"]
        by_name = {tool["name"]: tool for tool in tools}
        application_schema = by_name["apply_zdecision_gate_a0_delivery"][
            "inputSchema"
        ]
        counter_schema = by_name["increment_zdecision_gate_a0_counter"][
            "inputSchema"
        ]

        self.assertEqual(["classifications"], application_schema["required"])
        self.assertFalse(application_schema["additionalProperties"])
        self.assertEqual(
            {
                "application_binding_id",
                "delivery_id",
                "classifications",
            },
            set(application_schema["properties"]),
        )
        item_schema = application_schema["properties"]["classifications"]["items"]
        if "$ref" in item_schema:
            item_schema = application_schema["$defs"][
                item_schema["$ref"].rsplit("/", 1)[-1]
            ]
        classification_fields = {
            "decision_id",
            "revision",
            "digest",
            "classification",
            "reason",
        }
        self.assertEqual(classification_fields, set(item_schema["required"]))
        self.assertEqual(classification_fields, set(item_schema["properties"]))
        self.assertFalse(item_schema["additionalProperties"])
        self.assertEqual(
            r"^dec_[0-9a-f]{32}$",
            item_schema["properties"]["decision_id"]["pattern"],
        )
        self.assertEqual("integer", item_schema["properties"]["revision"]["type"])
        self.assertEqual(1, item_schema["properties"]["revision"]["minimum"])
        self.assertEqual(
            r"^[0-9a-f]{64}$",
            item_schema["properties"]["digest"]["pattern"],
        )
        self.assertEqual(
            ["applicable", "conflicting", "not_applicable", "uncertain"],
            sorted(item_schema["properties"]["classification"]["enum"]),
        )
        self.assertEqual(1, item_schema["properties"]["reason"]["minLength"])
        self.assertEqual(240, item_schema["properties"]["reason"]["maxLength"])
        self.assertEqual(
            r".*\S.*", item_schema["properties"]["reason"]["pattern"]
        )
        for host_field in ("application_binding_id", "delivery_id"):
            self.assertEqual(
                {"null", "string"},
                {
                    alternative["type"]
                    for alternative in application_schema["properties"][host_field][
                        "anyOf"
                    ]
                },
            )
        self.assertEqual([], counter_schema.get("required", []))
        self.assertFalse(counter_schema["additionalProperties"])
        self.assertEqual({"mutation_id"}, set(counter_schema["properties"]))
        self.assertEqual(
            {"null", "string"},
            {
                alternative["type"]
                for alternative in counter_schema["properties"]["mutation_id"][
                    "anyOf"
                ]
            },
        )

        classifications = [
            {
                "decision_id": FIXTURE_ONE["decision_id"],
                "revision": 1,
                "digest": FIXTURE_ONE_DIGEST,
                "classification": "applicable",
                "reason": "The first fixture governs this bounded action.",
            },
            {
                "decision_id": FIXTURE_TWO["decision_id"],
                "revision": 1,
                "digest": FIXTURE_TWO_DIGEST,
                "classification": "not_applicable",
                "reason": "The second fixture does not govern this action.",
            },
        ]
        direct_application = client.call(
            "apply_zdecision_gate_a0_delivery",
            {"classifications": classifications},
        )
        self.assertEqual(
            "invalid_application", direct_application["structuredContent"]["code"]
        )
        direct_counter = client.call("increment_zdecision_gate_a0_counter", {})
        self.assertEqual(
            "mutation_denied", direct_counter["structuredContent"]["code"]
        )

    def test_public_reason_enforces_utf8_byte_boundary(self) -> None:
        client = self._mcp()
        classifications = [
            {
                "decision_id": FIXTURE_ONE["decision_id"],
                "revision": 1,
                "digest": FIXTURE_ONE_DIGEST,
                "classification": "applicable",
                "reason": "界" * 80,
            },
            {
                "decision_id": FIXTURE_TWO["decision_id"],
                "revision": 1,
                "digest": FIXTURE_TWO_DIGEST,
                "classification": "not_applicable",
                "reason": "The second fixture does not govern this action.",
            },
        ]

        at_limit = client.call(
            "apply_zdecision_gate_a0_delivery",
            {"classifications": classifications},
        )
        over_limit = client.call(
            "apply_zdecision_gate_a0_delivery",
            {
                "classifications": [
                    {**classifications[0], "reason": "界" * 81},
                    classifications[1],
                ]
            },
        )

        self.assertEqual(
            "invalid_application", at_limit["structuredContent"]["code"]
        )
        self.assertNotEqual(
            "invalid_application",
            over_limit.get("structuredContent", {}).get("code"),
        )
        self.assertTrue(over_limit["isError"])
        self.assertIn("240 UTF-8 bytes", json.dumps(over_limit))

        tools = client.request("tools/list", {})["tools"]
        application_schema = next(
            tool["inputSchema"]
            for tool in tools
            if tool["name"] == "apply_zdecision_gate_a0_delivery"
        )
        item_schema = application_schema["properties"]["classifications"]["items"]
        if "$ref" in item_schema:
            item_schema = application_schema["$defs"][
                item_schema["$ref"].rsplit("/", 1)[-1]
            ]
        self.assertIn(
            "240 UTF-8 bytes", item_schema["properties"]["reason"]["description"]
        )

    def test_snapshot_freezes_intent_and_one_obvious_negative_control(self) -> None:
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
        snapshot = self._mcp().call(
            "enable_zdecision_gate_a0_delivery", {"attempt_id": attempt_id}
        )["structuredContent"]["snapshot"]

        self.assertEqual(TEST_INTENT, snapshot["intent"])
        self.assertEqual(
            ["packages/products/third-party-services/apps/security-services/"],
            snapshot["decisions"][0]["decision_revision"]["scope"]["paths"],
        )
        self.assertEqual(
            ["packages/products/third-party-services/apps/backup-services/"],
            snapshot["decisions"][1]["decision_revision"]["scope"]["paths"],
        )
        self.assertIn("backup-services", TEST_INTENT["exclusions"])

    def test_wrong_semantic_vector_is_terminal_without_application(self) -> None:
        attempt_id = self._render_attempt()
        client = self._mcp()
        snapshot = self._enable_and_ack(client, attempt_id)["snapshot"]
        first, second = snapshot["decisions"]
        wrong = [
            {
                "decision_id": first["decision_id"],
                "revision": first["revision"],
                "digest": first["digest"],
                "classification": "applicable",
                "reason": "The security-services Decision governs this feature.",
            },
            {
                "decision_id": second["decision_id"],
                "revision": second["revision"],
                "digest": second["digest"],
                "classification": "applicable",
                "reason": "The backup-services Decision was retrieved.",
            },
        ]
        bound = self._hook(
            "mcp__zdecision_gate_a0__apply_zdecision_gate_a0_delivery",
            {"classifications": wrong},
            turn_id=LIVE_APPLICATION_TURN,
        )

        result = client.call(
            "apply_zdecision_gate_a0_delivery", self._updated_input(bound)
        )
        inspect = self._run("inspect")

        self.assertEqual(
            "classification_oracle_mismatch", result["structuredContent"]["code"]
        )
        self.assertEqual(0, inspect["application_count"])
        self.assertEqual(0, inspect["mutation_count"])

    def test_handoff_instruction_requires_the_complete_counter_sequence(self) -> None:
        attempt_id = self._render_attempt()
        snapshot = self._mcp().call(
            "enable_zdecision_gate_a0_delivery", {"attempt_id": attempt_id}
        )["structuredContent"]["snapshot"]
        instruction = snapshot["application_instruction"]
        required_steps = (
            "1. Use only the typed intent and delivered Decisions.",
            "2. Call the disposable counter before application and observe denial.",
            "3. Submit complete classifications once.",
            "4. Call the disposable counter once after application_committed and observe counter == 1.",
            "5. Do not call shell, search, file-read, status, or render tools.",
            "6. Do not guess host-owned identifiers.",
        )

        self.assertEqual(APPLICATION_INSTRUCTION, instruction)
        positions = [instruction.index(step) for step in required_steps]
        self.assertEqual(sorted(positions), positions)

    def test_inspect_reports_exact_hook_trust_readiness(self) -> None:
        selector = json.loads(
            (self.root / ".zdecision-gate-a0-disposable.json").read_text("utf-8")
        )["selector"]
        source = f"{selector}@{selector}-marketplace:hooks/hooks.json:pre_tool_use:0:0"
        fallback_config = self.root / "codex-home/config.toml"
        host_config = self.parent / "fake-host-codex/config.toml"

        absent = self._run("inspect")

        self.assertEqual(source, absent["hook_trust_source"])
        self.assertFalse(absent["hook_trust_record_present"])
        host_config.parent.mkdir()
        host_config.write_text(f'[hooks.state]\n"{source}" = "trusted"\n', "utf-8")
        self.env["ZDECISION_GATE_A0_CODEX_CONFIG"] = str(host_config)

        present = self._run("inspect")

        self.assertEqual(source, present["hook_trust_source"])
        self.assertTrue(present["hook_trust_record_present"])
        diagnostic = json.dumps(present, sort_keys=True)
        self.assertNotIn("trusted", diagnostic)
        self.assertNotIn(str(host_config), diagnostic)
        self.assertNotIn("ZDECISION_GATE_A0_CODEX_CONFIG", diagnostic)

        self.env.pop("ZDECISION_GATE_A0_CODEX_CONFIG")
        fallback_config.parent.mkdir()
        fallback_config.write_text(
            f'[hooks.state]\n"{source}" = "fallback-trusted"\n', "utf-8"
        )
        fallback = self._run("inspect")
        self.assertTrue(fallback["hook_trust_record_present"])
        self.assertNotIn("fallback-trusted", json.dumps(fallback, sort_keys=True))

    def test_inspect_fails_closed_for_invalid_host_config_override(self) -> None:
        selector = json.loads(
            (self.root / ".zdecision-gate-a0-disposable.json").read_text("utf-8")
        )["selector"]
        source = f"{selector}@{selector}-marketplace:hooks/hooks.json:pre_tool_use:0:0"
        fallback_config = self.root / "codex-home/config.toml"
        fallback_config.parent.mkdir()
        fallback_config.write_text(
            f'[hooks.state]\n"{source}" = "fallback-trusted"\n', "utf-8"
        )
        malformed_config = self.parent / "malformed-host-config.toml"
        malformed_config.write_text("[hooks.state\n", "utf-8")

        for override in (
            "relative/config.toml",
            str(self.parent / "missing-host-config.toml"),
            str(malformed_config),
        ):
            with self.subTest(override=override):
                self.env["ZDECISION_GATE_A0_CODEX_CONFIG"] = override
                inspect = self._run("inspect")
                self.assertEqual(source, inspect["hook_trust_source"])
                self.assertFalse(inspect["hook_trust_record_present"])
                diagnostic = json.dumps(inspect, sort_keys=True)
                self.assertNotIn(override, diagnostic)
                self.assertNotIn("fallback-trusted", diagnostic)

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
        client = self._mcp()

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
        card = _generated_plugin_root(self.root) / "static/recall-gate-a0-v1.html"
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
        client = self._mcp()
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

    def test_application_hook_uses_delivered_row_despite_later_pending_show(
        self,
    ) -> None:
        delivered_attempt = self._render_attempt()
        client = self._mcp()
        enabled = self._enable_and_ack(client, delivered_attempt)
        delivery_id = enabled["snapshot"]["delivery_id"]
        later_show = self._hook(
            "mcp__zdecision_gate_a0__show_zdecision_gate_a0",
            {},
            turn_id="later-diagnostic-show-turn",
        )
        later_attempt = self._updated_input(later_show)["attempt_id"]
        classifications = [
            {
                "decision_id": FIXTURE_ONE["decision_id"],
                "revision": 1,
                "digest": FIXTURE_ONE_DIGEST,
                "classification": "applicable",
                "reason": "The delivered fixture governs this bounded action.",
            },
            {
                "decision_id": FIXTURE_TWO["decision_id"],
                "revision": 1,
                "digest": FIXTURE_TWO_DIGEST,
                "classification": "not_applicable",
                "reason": "The second fixture does not govern this action.",
            },
        ]

        unbound_application = client.call(
            "apply_zdecision_gate_a0_delivery",
            {"classifications": classifications},
        )

        binding = self._hook(
            "mcp__zdecision_gate_a0__apply_zdecision_gate_a0_delivery",
            {"classifications": classifications},
            turn_id=LIVE_APPLICATION_TURN,
        )
        updated = self._updated_input(binding)
        applied = client.call("apply_zdecision_gate_a0_delivery", updated)
        unbound_mutation = client.call("increment_zdecision_gate_a0_counter", {})

        self.assertNotEqual(delivered_attempt, later_attempt)
        self.assertEqual(
            "invalid_application",
            unbound_application["structuredContent"]["code"],
        )
        self.assertEqual("allow", binding["hookSpecificOutput"]["permissionDecision"])
        self.assertEqual(delivery_id, updated["delivery_id"])
        self.assertRegex(
            updated["application_binding_id"], r"^application_[0-9a-f]{32}$"
        )
        self.assertEqual(
            {
                "application_binding_id",
                "delivery_id",
                "classifications",
            },
            set(updated),
        )
        self.assertEqual(
            "application_committed", applied["structuredContent"]["state"]
        )
        self.assertEqual(
            "mutation_denied", unbound_mutation["structuredContent"]["code"]
        )
        inspect = self._run("inspect")
        self.assertEqual(2, inspect["attempt_count"])
        self.assertEqual(1, inspect["delivery_count"])
        self.assertEqual(1, inspect["application_count"])

    def test_application_hook_distinguishes_invalid_input_from_missing_delivery(
        self,
    ) -> None:
        incomplete = [
            {
                "decision_id": FIXTURE_ONE["decision_id"],
                "revision": 1,
                "digest": FIXTURE_ONE_DIGEST,
                "classification": "applicable",
            },
            {
                "decision_id": FIXTURE_TWO["decision_id"],
                "revision": 1,
                "digest": FIXTURE_TWO_DIGEST,
                "classification": "not_applicable",
            },
        ]
        complete = [
            {**incomplete[0], "reason": "The first fixture applies."},
            {**incomplete[1], "reason": "The second fixture does not apply."},
        ]

        invalid = self._hook(
            "mcp__zdecision_gate_a0__apply_zdecision_gate_a0_delivery",
            {"classifications": incomplete},
            turn_id=LIVE_APPLICATION_TURN,
        )
        missing = self._hook(
            "mcp__zdecision_gate_a0__apply_zdecision_gate_a0_delivery",
            {"classifications": complete},
            turn_id=LIVE_APPLICATION_TURN,
        )
        invalid_reason = invalid["hookSpecificOutput"]["permissionDecisionReason"]
        missing_reason = missing["hookSpecificOutput"]["permissionDecisionReason"]

        self.assertEqual("deny", invalid["hookSpecificOutput"]["permissionDecision"])
        self.assertEqual("deny", missing["hookSpecificOutput"]["permissionDecision"])
        self.assertIn("invalid classifications", invalid_reason)
        self.assertIn("nonempty reason", invalid_reason)
        self.assertIn("no delivered binding", missing_reason)
        self.assertNotEqual(invalid_reason, missing_reason)

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
        first = self._mcp()
        enabled = self._enable_and_ack(first, attempt_id)
        receipt = enabled["receipt"]
        first.close()

        restarted = self._mcp()
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
        self.assertEqual(
            [FIXTURE_ONE_DIGEST[:12], FIXTURE_TWO_DIGEST[:12]],
            inspect.get("fixture_digest_prefixes"),
        )
        self.assertRegex(
            inspect.get("snapshot_digest_prefix", ""), r"^[0-9a-f]{12}$"
        )
        instances = inspect["mcp_instances"]
        self.assertEqual(2, instances["total_count"])
        self.assertEqual(0, instances["running_count"])
        self.assertEqual(2, instances["exited_count"])
        for instance in instances["instances"]:
            self.assertRegex(
                instance["instance_id_prefix"], r"^mcp_[0-9a-f]{8}$"
            )
        self.assertNotIn("pid", json.dumps(instances))
        self.assertNotIn("ui_message_count", inspect)
        self.assertNotIn("app_server_start_count", inspect)
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
        plugin_root = _generated_plugin_root(self.root)
        manifest = json.loads(
            (plugin_root / ".codex-plugin/plugin.json").read_text("utf-8")
        )
        mcp = json.loads((plugin_root / ".mcp.json").read_text("utf-8"))
        hooks = json.loads((plugin_root / "hooks/hooks.json").read_text("utf-8"))
        marketplace = json.loads(
            (self.root / "marketplace/.agents/plugins/marketplace.json").read_text(
                "utf-8"
            )
        )

        self.assertEqual("ZDecision Gate A0", manifest["interface"]["displayName"])
        self.assertEqual(["Interactive"], manifest["interface"]["capabilities"])
        self.assertRegex(manifest["name"], r"^zdecision-gate-a0-[0-9a-f]{8}$")
        self.assertEqual({"mcpServers"}, set(mcp))
        self.assertEqual({"zdecision-gate-a0"}, set(mcp["mcpServers"]))
        generated_server = next(iter(mcp["mcpServers"].values()))
        self.assertEqual(str(PYTHON), generated_server["command"])
        self.assertEqual({"PreToolUse"}, set(hooks["hooks"]))
        generated_hook = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn(str(PYTHON), generated_hook)
        self.assertEqual({"name", "interface", "plugins"}, set(marketplace))
        self.assertEqual(1, len(marketplace["plugins"]))
        self.assertEqual(manifest["name"], marketplace["plugins"][0]["name"])
        self.assertNotEqual(REPOSITORY_ROOT / "plugins/zdecision", plugin_root)
        self.assertIsNotNone(disposable)

    def test_marketplace_resolves_a_self_contained_relative_plugin(self) -> None:
        marketplace_root = (self.root / "marketplace").resolve()
        marketplace = json.loads(
            (marketplace_root / ".agents/plugins/marketplace.json").read_text(
                "utf-8"
            )
        )
        entry = marketplace["plugins"][0]
        source = entry["source"]

        self.assertEqual("local", source["source"])
        self.assertEqual(f"./plugins/{entry['name']}", source["path"])
        self.assertFalse(Path(source["path"]).is_absolute())
        plugin_root = (marketplace_root / source["path"]).resolve()
        plugin_root.relative_to(marketplace_root)
        manifest = json.loads(
            (plugin_root / ".codex-plugin/plugin.json").read_text("utf-8")
        )
        self.assertEqual(entry["name"], manifest["name"])
        self.assertFalse((self.root / "plugin").exists())

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

    def test_cleanup_quarantines_the_root_before_recursive_deletion(self) -> None:
        script = """
import json
import sys
from pathlib import Path
from tests.integration import recall_gate_a0_disposable_harness as harness

root = Path(sys.argv[1])
events = []

def audit(event, arguments):
    if event == "os.rename" and Path(arguments[0]) == root:
        events.append("rename")
    elif event == "shutil.rmtree":
        events.append("rmtree")

sys.addaudithook(audit)
result = harness.cleanup(root)
print(json.dumps({"events": events, "result": result}, sort_keys=True))
"""
        completed = subprocess.run(
            [str(PYTHON), "-c", script, str(self.root)],
            cwd=REPOSITORY_ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertEqual(["rename", "rmtree"], output["events"][:2])
        self.assertTrue(output["result"]["removed"])
        self.assertFalse(self.root.exists())
        self.assertEqual(
            [], list(self.parent.glob(f".{self.root.name}.cleanup-*"))
        )

    def test_stale_cleanup_cannot_remove_a_recreated_root_generation(self) -> None:
        ready = self.parent / "cleanup-ready"
        resume = self.parent / "cleanup-resume"
        script = """
import sys
import time
from pathlib import Path
from tests.integration import recall_gate_a0_disposable_harness as harness

root = Path(sys.argv[1])
ready = Path(sys.argv[2])
resume = Path(sys.argv[3])
lifecycle = str(root / "state/mcp-lifecycle.lock")
paused = False

def audit(event, arguments):
    global paused
    if event == "open" and arguments and str(arguments[0]) == lifecycle and not paused:
        paused = True
        ready.touch()
        while not resume.exists():
            time.sleep(0.01)

sys.addaudithook(audit)
harness.cleanup(root)
"""
        cleanup_process = subprocess.Popen(
            [str(PYTHON), "-c", script, str(self.root), str(ready), str(resume)],
            cwd=REPOSITORY_ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists(), "cleanup did not reach the lease boundary")

        old_generation = self.parent / "old-root-generation"
        self.root.rename(old_generation)
        created = self._run("create", "--repository", str(self.repository))
        replacement_selector = created["selector"]
        resume.touch()
        stdout, stderr = cleanup_process.communicate(timeout=10)

        self.assertNotEqual(0, cleanup_process.returncode, stdout or stderr)
        self.assertTrue(self.root.exists())
        replacement = json.loads(
            (self.root / ".zdecision-gate-a0-disposable.json").read_text("utf-8")
        )
        self.assertEqual(replacement_selector, replacement["selector"])
        self.assertTrue(old_generation.exists())

    def test_cleanup_revalidates_the_generation_moved_at_rename_boundary(
        self,
    ) -> None:
        old_generation = self.parent / "old-root-at-rename"
        replacement_result = self.parent / "replacement-result.json"
        original_marker = (
            self.root / ".zdecision-gate-a0-disposable.json"
        ).read_bytes()
        script = """
import json
import os
import sys
from pathlib import Path
from tests.integration import recall_gate_a0_disposable_harness as harness

root = Path(sys.argv[1])
repository = Path(sys.argv[2])
old_generation = Path(sys.argv[3])
replacement_result = Path(sys.argv[4])
injected = False

def audit(event, arguments):
    global injected
    if event != "os.rename" or injected:
        return
    source = Path(arguments[0])
    target = Path(arguments[1])
    if source != root or not target.name.startswith(f".{root.name}.cleanup-"):
        return
    injected = True
    os.rename(root, old_generation)
    created = harness.create(root, repository)
    replacement_result.write_text(
        json.dumps({"selector": created["selector"]}), "utf-8"
    )

sys.addaudithook(audit)
harness.cleanup(root)
"""
        completed = subprocess.run(
            [
                str(PYTHON),
                "-c",
                script,
                str(self.root),
                str(self.repository),
                str(old_generation),
                str(replacement_result),
            ],
            cwd=REPOSITORY_ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

        self.assertNotEqual(0, completed.returncode, completed.stdout)
        replacement_selector = json.loads(
            replacement_result.read_text("utf-8")
        )["selector"]
        replacement = json.loads(
            (self.root / ".zdecision-gate-a0-disposable.json").read_text("utf-8")
        )
        self.assertEqual(replacement_selector, replacement["selector"])
        self.assertTrue(old_generation.exists())
        self.assertEqual(
            original_marker,
            (old_generation / ".zdecision-gate-a0-disposable.json").read_bytes(),
        )
        self.assertEqual(
            [], list(self.parent.glob(f".{self.root.name}.cleanup-*"))
        )

    def test_digit_leading_host_ids_bind_render_application_and_mutation(self) -> None:
        attempt_id = self._render_attempt()
        client = self._mcp()
        self._enable_and_ack(client, attempt_id)
        classifications = [
            {
                "decision_id": FIXTURE_ONE["decision_id"],
                "revision": 1,
                "digest": FIXTURE_ONE_DIGEST,
                "classification": "applicable",
                "reason": "Digit-leading host binding remains trusted.",
            },
            {
                "decision_id": FIXTURE_TWO["decision_id"],
                "revision": 1,
                "digest": FIXTURE_TWO_DIGEST,
                "classification": "not_applicable",
                "reason": "The second fixture remains outside this mutation.",
            },
        ]

        application = self._hook(
            "mcp__zdecision_gate_a0__apply_zdecision_gate_a0_delivery",
            {"classifications": classifications},
            turn_id=LIVE_APPLICATION_TURN,
        )
        applied = client.call(
            "apply_zdecision_gate_a0_delivery", self._updated_input(application)
        )
        mutation = self._hook(
            "mcp__zdecision_gate_a0__increment_zdecision_gate_a0_counter",
            {},
            turn_id=LIVE_APPLICATION_TURN,
        )
        incremented = client.call(
            "increment_zdecision_gate_a0_counter", self._updated_input(mutation)
        )

        self.assertEqual("application_committed", applied["structuredContent"]["state"])
        self.assertEqual(1, incremented["structuredContent"]["counter"])

    def test_hook_request_runs_the_generated_command(self) -> None:
        secondary = self.parent / "secondary-hook-root"
        self._run_at(
            secondary,
            "create",
            "--repository",
            str(self.repository),
        )
        self.addCleanup(
            lambda: secondary.exists() and self._run_at(secondary, "cleanup")
        )
        hooks_path = _generated_plugin_root(self.root) / "hooks/hooks.json"
        hooks = json.loads(hooks_path.read_text("utf-8"))
        hook = hooks["hooks"]["PreToolUse"][0]["hooks"][0]
        command = shlex.split(hook["command"])
        command[-1] = str(secondary)
        hook["command"] = shlex.join(command)
        hooks_path.write_text(json.dumps(hooks), "utf-8")

        output = self._hook(
            "mcp__zdecision_gate_a0__show_zdecision_gate_a0",
            {"session_id": MODEL_SESSION, "turn_id": MODEL_TURN, "cwd": MODEL_CWD},
            session_id="launch-session",
            turn_id="launch-turn",
        )

        self.assertEqual("allow", output["hookSpecificOutput"]["permissionDecision"])
        self.assertEqual(0, self._run("inspect")["attempt_count"])
        self.assertEqual(1, self._run_at(secondary, "inspect")["attempt_count"])

    def test_mcp_process_runs_the_generated_command_and_arguments(self) -> None:
        secondary = self.parent / "secondary-mcp-root"
        self._run_at(
            secondary,
            "create",
            "--repository",
            str(self.repository),
        )
        self.addCleanup(
            lambda: secondary.exists() and self._run_at(secondary, "cleanup")
        )
        bound = self._run_at(
            secondary,
            "hook",
            stdin={
                "hook_event_name": "PreToolUse",
                "session_id": "launch-session",
                "turn_id": "launch-turn",
                "cwd": str(self.repository),
                "tool_name": "mcp__zdecision_gate_a0__show_zdecision_gate_a0",
                "tool_input": {},
            },
        )
        attempt_id = self._updated_input(bound)["attempt_id"]
        mcp_path = _generated_plugin_root(self.root) / ".mcp.json"
        mcp = json.loads(mcp_path.read_text("utf-8"))
        generated = next(iter(mcp["mcpServers"].values()))
        generated["args"][-1] = str(secondary)
        mcp_path.write_text(json.dumps(mcp), "utf-8")

        client = self._mcp()
        rendered = client.call("show_zdecision_gate_a0", {"attempt_id": attempt_id})

        self.assertEqual(
            "pending_confirmation", rendered["structuredContent"]["state"]
        )

    def test_two_generated_mcp_clients_for_one_root_remain_independently_ready(
        self,
    ) -> None:
        first = self._mcp()
        second = self._mcp()

        for client in (first, second):
            tools = client.request("tools/list", {})["tools"]
            self.assertIn(
                "show_zdecision_gate_a0", {tool["name"] for tool in tools}
            )
            self.assertIsNone(client.process.poll())
            child_check = subprocess.run(
                ["ps", "-axo", "pid=,ppid="],
                text=True,
                capture_output=True,
                check=False,
            )
            children = [
                line
                for line in child_check.stdout.splitlines()
                if len(line.split()) == 2
                and line.split()[1] == str(client.process.pid)
            ]
            self.assertEqual(
                [], children, "MCP instance started a child"
            )
            self.assertEqual(0, child_check.returncode)
            self.assertEqual("", child_check.stderr.strip())
            tcp_check = subprocess.run(
                [
                    "lsof",
                    "-nP",
                    "-a",
                    "-p",
                    str(client.process.pid),
                    "-iTCP",
                    "-Fn",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                "", tcp_check.stdout.strip(), "MCP instance opened a TCP connection"
            )
            self.assertEqual("", tcp_check.stderr.strip())
            unix_check = subprocess.run(
                ["lsof", "-nP", "-a", "-p", str(client.process.pid), "-U"],
                text=True,
                capture_output=True,
                check=False,
            )
            unix_lines = unix_check.stdout.splitlines()[1:]
            self.assertEqual("", unix_check.stderr.strip())
            endpoints = []
            for line in unix_lines:
                match = re.search(
                    r"\sunix\s+(0x[0-9a-f]+)\s+\S+\s+->(0x[0-9a-f]+)$",
                    line,
                )
                self.assertIsNotNone(
                    match, f"MCP instance opened a named Unix connection: {line}"
                )
                endpoints.append(match.groups())
            self.assertEqual(
                {node for node, _ in endpoints},
                {peer for _, peer in endpoints},
                "MCP instance connected to an external Unix endpoint",
            )

        running = self._run("inspect")["mcp_instances"]
        self.assertEqual(2, running["total_count"])
        self.assertEqual(2, running["running_count"])
        self.assertEqual(0, running["exited_count"])
        self.assertEqual(2, len(running["instances"]))
        prefixes = {
            instance["instance_id_prefix"] for instance in running["instances"]
        }
        self.assertEqual(2, len(prefixes))
        self.assertTrue(
            all(instance["state"] == "running" for instance in running["instances"])
        )
        self.assertNotIn("pid", json.dumps(running))

    def test_inspect_bounds_historical_instance_prefixes(self) -> None:
        instances_root = self.root / "state/mcp-instances"
        instances_root.mkdir()
        instance_ids = [f"mcp_{index:08x}{index:024x}" for index in range(12)]
        for index, instance_id in enumerate(instance_ids):
            (instances_root / f"{instance_id}.json").write_text(
                json.dumps(
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "instance_id": instance_id,
                        "pid": 10_000 + index,
                        "state": "exited",
                    }
                ),
                "utf-8",
            )
            (instances_root / f"{instance_id}.lock").touch()

        instances = self._run("inspect")["mcp_instances"]

        self.assertEqual(12, instances["total_count"])
        self.assertEqual(0, instances["running_count"])
        self.assertEqual(12, instances["exited_count"])
        self.assertEqual(8, len(instances["instances"]))
        self.assertEqual(4, instances["omitted_count"])
        serialized = json.dumps(instances)
        for instance_id in instance_ids:
            self.assertNotIn(instance_id, serialized)

    def test_cleanup_waits_for_every_concurrent_mcp_instance(self) -> None:
        first = self._mcp()
        second = self._mcp()

        records = sorted((self.root / "state/mcp-instances").glob("*.json"))
        self.assertEqual(2, len(records))
        identities = [json.loads(record.read_text("utf-8")) for record in records]
        self.assertEqual(
            {first.process.pid, second.process.pid},
            {identity["pid"] for identity in identities},
        )
        self.assertTrue(
            all(
                re.fullmatch(r"mcp_[0-9a-f]{32}", identity["instance_id"])
                for identity in identities
            )
        )

        running = self._run("inspect")["mcp_instances"]
        self.assertEqual(2, running["running_count"])
        for identity in identities:
            self.assertNotIn(identity["instance_id"], json.dumps(running))

        completed = subprocess.run(
            [
                str(PYTHON),
                str(HARNESS_PATH),
                "cleanup",
                "--root",
                str(self.root),
            ],
            cwd=REPOSITORY_ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertTrue(self.root.exists())

        first.close()
        one_running = self._run("inspect")["mcp_instances"]
        self.assertEqual(1, one_running["running_count"])
        self.assertEqual(1, one_running["exited_count"])
        completed = subprocess.run(
            [
                str(PYTHON),
                str(HARNESS_PATH),
                "cleanup",
                "--root",
                str(self.root),
            ],
            cwd=REPOSITORY_ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertNotEqual(0, completed.returncode)
        self.assertTrue(self.root.exists())

        second.close()
        all_exited = self._run("inspect")["mcp_instances"]
        self.assertEqual(0, all_exited["running_count"])
        self.assertEqual(2, all_exited["exited_count"])
        self._run("cleanup")
        self.assertFalse(self.root.exists())

    def test_prior_root_legacy_lease_remains_safe_and_restartable(self) -> None:
        (self.root / "state/mcp-lifecycle.lock").unlink()
        legacy_id = "mcp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        (self.root / "state/mcp-process.json").write_text(
            json.dumps(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "process_id": legacy_id,
                    "pid": os.getpid(),
                    "state": "running",
                }
            ),
            "utf-8",
        )
        legacy_lease = (self.root / "state/mcp-process.lock").open(
            "a+", encoding="utf-8"
        )
        fcntl.flock(legacy_lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            running = self._run("inspect")["mcp_instances"]
            self.assertEqual(1, running["running_count"])
            self.assertEqual(legacy_id[:12], running["instances"][0]["instance_id_prefix"])
            completed = subprocess.run(
                [
                    str(PYTHON),
                    str(HARNESS_PATH),
                    "cleanup",
                    "--root",
                    str(self.root),
                ],
                cwd=REPOSITORY_ROOT,
                env=self.env,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertTrue(self.root.exists())
        finally:
            fcntl.flock(legacy_lease.fileno(), fcntl.LOCK_UN)
            legacy_lease.close()

        (self.root / "state/mcp-lifecycle.lock").unlink()
        client = self._mcp()
        tools = client.request("tools/list", {})["tools"]
        self.assertIn("show_zdecision_gate_a0", {tool["name"] for tool in tools})
        running = self._run("inspect")["mcp_instances"]
        self.assertEqual(2, running["total_count"])
        self.assertEqual(1, running["running_count"])
        client.close()
        self._run("cleanup")
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
