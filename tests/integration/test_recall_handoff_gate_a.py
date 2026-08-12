"""Production-boundary checks for the disposable Recall Gate A harness."""

from __future__ import annotations

import tempfile
import unittest
import sqlite3
from hashlib import sha256
from pathlib import Path
import os
import json
import queue
import subprocess
import threading
from dataclasses import replace
from unittest.mock import patch

from tests.integration import recall_gate_a_desktop_harness as harness
from zdecision.agent.capture_operation_store import CaptureOperationStore
from zdecision.agent.mcp_server import RECALL_CONFIRMATION_PATH
from zdecision.agent.request_state import RequestStateStore
from zdecision.recall.session import RecallIntent


class _McpClient:
    def __init__(self, launcher: Path, plugin: Path) -> None:
        self.process = subprocess.Popen(
            [str(Path.cwd() / ".venv/bin/python"), str(launcher), "mcp"],
            cwd=Path.cwd(), env={**os.environ, "PLUGIN_ROOT": str(plugin)},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self.lines: queue.Queue[str] = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.identifier = 1
        self.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "gate-a", "version": "1"}})
        self.notify("notifications/initialized", {})

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.put(line)

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        identifier = self.identifier; self.identifier += 1
        self._write({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
        response = json.loads(self.lines.get(timeout=10))
        if response.get("id") != identifier or "error" in response:
            raise AssertionError(response)
        return response["result"]

    def notify(self, method: str, params: dict[str, object]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, value: dict[str, object]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n"); self.process.stdin.flush()

    def tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        if self.process.poll() is None and self.process.stdin is not None:
            self.process.stdin.close()
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired: self.process.terminate(); self.process.wait(timeout=5)
        self.reader.join(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


class RecallGateAVerticalTests(unittest.TestCase):
    def test_generated_disposable_bundle_has_a_verified_unique_identity(self) -> None:
        """The vertical must use a generated bundle, never production identity."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "gate-a"
            created = harness.create(root=root, repository=Path.cwd())

            self.assertNotEqual("zdecision", created["plugin_name"])
            self.assertNotEqual("zdecision-local", created["mcp_server_key"])
            inspected = harness.inspect(root=root)
            self.assertEqual("ready", inspected["state"])

    def test_production_hook_store_handoff_and_reuse_vertical(self) -> None:
        """One native-shaped delivery reaches only production Gate A boundaries."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "gate-a"
            created = harness.create(root=root, repository=Path.cwd())
            plugin = root / "marketplace" / "plugins" / created["plugin_name"]
            database_path = root / "state" / "agent.sqlite3"
            candidate_state = RequestStateStore.open(database_path)
            capture_state = CaptureOperationStore.open(root / "state" / "capture.sqlite3")
            self.addCleanup(candidate_state.close)
            self.addCleanup(capture_state.close)
            def table_snapshot(path: Path, prefix: str) -> tuple[tuple[str, int, str], ...]:
                with sqlite3.connect(path) as connection:
                    rows = connection.execute(
                        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name LIKE ? ORDER BY name",
                        (prefix + "%",),
                    ).fetchall()
                    return tuple(
                        (name, int(connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]), sha256(sql.encode("utf-8")).hexdigest())
                        for name, sql in rows
                    )
            before_candidate = table_snapshot(database_path, "candidate_") + table_snapshot(database_path, "slice_candidate_")
            before_capture = table_snapshot(capture_state.path, "capture_")
            before_plugin = tuple(
                (path.relative_to(plugin).as_posix(), sha256(path.read_bytes()).hexdigest())
                for path in sorted(plugin.rglob("*")) if path.is_file()
            )
            before_marketplace = sha256((root / "marketplace" / ".agents" / "plugins" / "marketplace.json").read_bytes()).hexdigest()
            client = _McpClient(plugin / "recall_gate_a_launcher.py", plugin)
            self.addCleanup(client.close)
            runtime = harness.GateARuntime(
                root=root,
                repository=Path.cwd(),
                identity=harness._identity_from_fields(
                    harness._read_configuration(root)["identity"]
                ),
            )
            self.addCleanup(runtime.close)
            intent = RecallIntent.from_dict(
                {
                    "target_decision_space_ids": ["space-gate-a"],
                    "explicit_multi_space": False,
                    "feature_goal": "Exercise production Recall Gate A",
                    "domain_objects": ["Recall"],
                    "repository_relative_paths": ["src/gate-a"],
                    "constraints": ["local only"],
                    "exclusions": ["network"],
                }
            )
            with patch.dict("os.environ", {"PLUGIN_ROOT": str(plugin)}, clear=False):
                runtime.hook({
                    "hook_event_name": "UserPromptSubmit", "session_id": "session-a",
                    "turn_id": "turn-authorize", "cwd": str(Path.cwd()), "prompt": "native turn",
                })
                bound = runtime.hook({
                    "hook_event_name": "PreToolUse", "session_id": "session-a",
                    "turn_id": "turn-authorize", "cwd": str(Path.cwd()),
                    "tool_name": runtime.identity.tool_name("show_zdecision_recall_confirmation"),
                    "tool_input": {"intent": intent.to_dict()},
                })
                rewritten = bound["hookSpecificOutput"]["updatedInput"]
                attempt_id = rewritten["activation_attempt_id"]
                resource = client.request("resources/read", {"uri": "ui://zdecision/recall-confirmation-v1.html"})
                self.assertIn("contents", resource)
                card = client.tool("show_zdecision_recall_confirmation", {"activation_attempt_id": attempt_id, "intent": intent.to_dict()})
                self.assertEqual("pending_confirmation", card["structuredContent"]["state"])
                enabled = client.tool("decide_zdecision_recall", {"activation_attempt_id": attempt_id, "action": "enable"})
                delivery = runtime.store.delivery_for_attempt(attempt_id)
                self.assertEqual("delivery_claimed", enabled["structuredContent"]["state"])
                client.tool("ack_zdecision_recall_delivery", {"activation_attempt_id": attempt_id, "delivery_id": delivery.delivery_id, "context_digest": delivery.context_digest})
                runtime.hook({
                    "hook_event_name": "UserPromptSubmit", "session_id": "session-a",
                    "turn_id": "turn-apply", "cwd": str(Path.cwd()), "prompt": "next native turn",
                })
                denied = runtime.hook({
                    "hook_event_name": "PreToolUse", "session_id": "session-a",
                    "turn_id": "turn-apply", "cwd": str(Path.cwd()), "tool_name": "Bash", "tool_input": {},
                })
                self.assertEqual("deny", denied["hookSpecificOutput"]["permissionDecision"])
                items = [
                    {"decision_id": item.revision.decision_id, "revision": item.revision.revision,
                     "digest": item.digest, "disposition": disposition, "reason": "fixed test classification"}
                    for item, disposition in zip(delivery.shortlist.items, ("applicable", "not_applicable"), strict=True)
                ]
                applied_binding = runtime.hook({
                    "hook_event_name": "PreToolUse", "session_id": "session-a", "turn_id": "turn-apply", "cwd": str(Path.cwd()),
                    "tool_name": runtime.identity.tool_name("apply_zdecision_recall_delivery"), "tool_input": {"items": items},
                })["hookSpecificOutput"]["updatedInput"]
                applied = client.tool("apply_zdecision_recall_delivery", applied_binding)
                self.assertEqual("application_committed", applied["structuredContent"]["state"])
                released = runtime.hook({
                    "hook_event_name": "PreToolUse", "session_id": "session-a",
                    "turn_id": "turn-apply", "cwd": str(Path.cwd()), "tool_name": "Bash", "tool_input": {},
                })
                self.assertEqual({}, released)
                runtime.hook({
                    "hook_event_name": "UserPromptSubmit", "session_id": "session-a",
                    "turn_id": "turn-reuse", "cwd": str(Path.cwd()), "prompt": "same intent",
                })
                gate = runtime.hook({
                    "hook_event_name": "PreToolUse", "session_id": "session-a",
                    "turn_id": "turn-reuse", "cwd": str(Path.cwd()),
                    "tool_name": runtime.identity.tool_name("gate_zdecision_turn"), "tool_input": {"intent": intent.to_dict()},
                })["hookSpecificOutput"]["updatedInput"]
                self.assertEqual("reuse", runtime.recall_tools.gate_zdecision_turn(**gate)["state"])
                self.assertEqual("application_committed", runtime.store.get_delivery(delivery.delivery_id).state)
                runtime.hook({"hook_event_name": "PreCompact", "session_id": "session-a", "turn_id": "turn-reuse", "cwd": str(Path.cwd()), "trigger": "auto"})
                runtime.hook({"hook_event_name": "PostCompact", "session_id": "session-a", "turn_id": "turn-reuse", "cwd": str(Path.cwd()), "trigger": "auto"})
                restored = runtime.hook({"hook_event_name": "SessionStart", "session_id": "session-a", "turn_id": "turn-reuse", "cwd": str(Path.cwd()), "source": "compact"})
                envelope = json.loads(restored["hookSpecificOutput"]["additionalContext"])
                self.assertEqual("ZDECISION_RECALL_RESTORATION", envelope["marker"])
                self.assertEqual(
                    [delivery.shortlist.items[0].to_dict()], envelope["decisions"]
                )
                self.assertEqual(1, harness.inspect(root=root)["delivery_count"])

                identity = runtime.identity
                runtime.close()
                reopened_runtime = harness.GateARuntime(
                    root=root, repository=Path.cwd(), identity=identity
                )
                self.addCleanup(reopened_runtime.close)
                self.assertEqual(
                    1, len(reopened_runtime.store.list_active_items("session-a"))
                )
                self.assertIsNotNone(
                    reopened_runtime.store.bound_recall_skill_path("attempt", attempt_id)
                )
                reopened_runtime.hook({
                    "hook_event_name": "UserPromptSubmit", "session_id": "session-a",
                    "turn_id": "turn-reopen-reuse", "cwd": str(Path.cwd()),
                    "prompt": "same intent after restart",
                })
                reopened_gate = reopened_runtime.hook({
                    "hook_event_name": "PreToolUse", "session_id": "session-a",
                    "turn_id": "turn-reopen-reuse", "cwd": str(Path.cwd()),
                    "tool_name": identity.tool_name("gate_zdecision_turn"),
                    "tool_input": {"intent": intent.to_dict()},
                })["hookSpecificOutput"]["updatedInput"]
                self.assertEqual(
                    "reuse", reopened_runtime.recall_tools.gate_zdecision_turn(**reopened_gate)["state"]
                )
                self.assertEqual(0, reopened_runtime.provider.retrieve_calls)
                foreign = replace(identity, plugin_name="zdecision-gatea-foreign", mcp_server_key="zdecision-gatea-foreign")
                other = harness.RecallHostStore.open(root / "state" / "agent.sqlite3", identity=foreign)
                self.assertIsNone(other.bound_recall_skill_path("attempt", attempt_id))
                other.close()
            self.assertEqual(before_candidate, table_snapshot(database_path, "candidate_") + table_snapshot(database_path, "slice_candidate_"))
            self.assertEqual(before_capture, table_snapshot(capture_state.path, "capture_"))
            self.assertEqual(before_plugin, tuple((path.relative_to(plugin).as_posix(), sha256(path.read_bytes()).hexdigest()) for path in sorted(plugin.rglob("*")) if path.is_file()))
            self.assertEqual(before_marketplace, sha256((root / "marketplace" / ".agents" / "plugins" / "marketplace.json").read_bytes()).hexdigest())

    def test_launcher_and_cleanup_fail_closed_on_mismatch_or_live_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "gate-a"
            created = harness.create(root=root, repository=Path.cwd())
            plugin = root / "marketplace" / "plugins" / created["plugin_name"]
            launcher = plugin / "recall_gate_a_launcher.py"
            wrong_root = Path(temporary) / "wrong"
            wrong_root.mkdir()
            failed = subprocess.run(
                [str(Path.cwd() / ".venv/bin/python"), str(launcher), "hook"],
                input="{}", text=True, capture_output=True,
                env={**os.environ, "PLUGIN_ROOT": str(wrong_root)},
                check=False,
            )
            self.assertNotEqual(0, failed.returncode)
            self.assertTrue(root.exists())
            with harness._McpLease(root):
                with self.assertRaises(RuntimeError):
                    harness.cleanup(root=root)
            self.assertEqual({"state": "removed"}, harness.cleanup(root=root))

    def test_cleanup_preserves_a_root_replaced_after_validation(self) -> None:
        """Cleanup must never delete a path substituted after its marker check."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "gate-a"
            harness.create(root=root, repository=Path.cwd())
            original_marker = harness._marker
            displaced = Path(temporary) / "displaced-original"
            replacement = root / "replacement-sentinel"
            validated_root = root.resolve()
            substituted = False

            def substitute_after_validation(value: Path):
                nonlocal substituted
                result = original_marker(value)
                if not substituted and value == validated_root:
                    substituted = True
                    root.rename(displaced)
                    root.mkdir()
                    replacement.write_text("preserve this replacement", "utf-8")
                return result

            with patch.object(harness, "_marker", side_effect=substitute_after_validation):
                with self.assertRaises(RuntimeError):
                    harness.cleanup(root=root)
            self.assertTrue(replacement.is_file())
            self.assertTrue(displaced.is_dir())

    def test_marker_generation_mutation_preserves_the_uncertain_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "gate-a"
            created = harness.create(root=root, repository=Path.cwd())
            launcher = root / "marketplace" / "plugins" / created["plugin_name"] / "recall_gate_a_launcher.py"
            marker = root / ".recall-gate-a-marker.json"
            value = __import__("json").loads(marker.read_text("utf-8"))
            value["generation"] = "0" * 32
            marker.write_text(__import__("json").dumps(value), "utf-8")
            failed = subprocess.run([str(Path.cwd() / ".venv/bin/python"), str(launcher), "hook"], input="{}", text=True, capture_output=True, check=False)
            self.assertNotEqual(0, failed.returncode)
            with self.assertRaises(RuntimeError): harness.inspect(root=root)
            with self.assertRaises(RuntimeError): harness.cleanup(root=root)
            self.assertTrue(root.exists())

    def test_coordinated_generation_substitution_cannot_fool_tracked_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "gate-a"
            created = harness.create(root=root, repository=Path.cwd())
            plugin = root / "marketplace" / "plugins" / created["plugin_name"]
            launcher = plugin / "recall_gate_a_launcher.py"
            for name in (".recall-gate-a-marker.json", ".recall-gate-a-runtime.json"):
                path = root / name
                value = json.loads(path.read_text("utf-8"))
                value["generation"] = "0" * 32
                path.write_text(json.dumps(value), "utf-8")
            failed = subprocess.run(
                [str(Path.cwd() / ".venv/bin/python"), str(launcher), "hook"],
                input="{}", text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(0, failed.returncode)
            with self.assertRaises(RuntimeError):
                harness.inspect(root=root)
            with self.assertRaises(RuntimeError):
                harness.cleanup(root=root)
            self.assertTrue(root.exists())

    def test_cleanup_preserves_replacement_after_quarantine_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "gate-a"
            harness.create(root=root, repository=Path.cwd())
            original_marker = harness._marker
            displaced = Path(temporary) / "validated-quarantine"
            replacement = Path(temporary) / "replacement-sentinel"
            calls = 0

            def substitute_after_second_validation(value: Path):
                nonlocal calls
                result = original_marker(value)
                calls += 1
                if calls == 2:
                    value.rename(displaced)
                    value.mkdir()
                    replacement = value / "preserve-me"
                    replacement.write_text("replacement", "utf-8")
                return result

            with patch.object(harness, "_marker", side_effect=substitute_after_second_validation):
                with self.assertRaises(RuntimeError):
                    harness.cleanup(root=root)
            self.assertTrue((root / "preserve-me").is_file())
            self.assertTrue(displaced.is_dir())

    def test_cleanup_never_follows_a_child_swapped_to_external_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "gate-a"
            harness.create(root=root, repository=Path.cwd())
            child = root / "race-dir"
            child.mkdir()
            (child / "owned").write_text("owned", "utf-8")
            external = Path(temporary) / "external"
            external.mkdir()
            sentinel = external / "must-survive"
            sentinel.write_text("external", "utf-8")
            displaced = Path(temporary) / "displaced-child"
            original_stat = harness.os.stat
            swapped = False

            def swap_after_child_stat(name, *args, **kwargs):
                nonlocal swapped
                result = original_stat(name, *args, **kwargs)
                if (
                    not swapped
                    and name == "race-dir"
                    and kwargs.get("follow_symlinks") is False
                    and kwargs.get("dir_fd") is not None
                ):
                    swapped = True
                    quarantined = next(
                        path
                        for path in Path(temporary).iterdir()
                        if path.name.startswith(".gate-a.cleanup-")
                    )
                    live_child = quarantined / name
                    live_child.rename(displaced)
                    os.symlink(external, live_child)
                return result

            with patch.object(harness.os, "stat", side_effect=swap_after_child_stat):
                with self.assertRaises((RuntimeError, OSError)):
                    harness.cleanup(root=root)
            self.assertTrue(swapped)
            self.assertTrue(sentinel.is_file())
            self.assertTrue(root.exists())
            self.assertTrue(displaced.is_dir())

    def test_launcher_substitution_and_stale_lease_never_expand_cleanup_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "gate-a"
            created = harness.create(root=root, repository=Path.cwd())
            plugin = root / "marketplace" / "plugins" / created["plugin_name"]
            launcher = plugin / "recall_gate_a_launcher.py"
            stale = root / ".recall-gate-a-leases" / "stale-client"
            stale.write_text("99999999", "ascii")
            self.assertEqual(0, harness.inspect(root=root)["live_mcp_leases"])
            self.assertFalse(stale.exists())

            launcher.write_text(launcher.read_text("utf-8") + "\n# substituted\n", "utf-8")
            failed = subprocess.run(
                [str(Path.cwd() / ".venv/bin/python"), str(launcher), "mcp"],
                text=True,
                input="",
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(0, failed.returncode)
            with self.assertRaises(RuntimeError):
                harness.inspect(root=root)
            with self.assertRaises(RuntimeError):
                harness.cleanup(root=root)
            self.assertTrue(root.exists())

    def test_missing_exact_mcp_client_creates_no_cleanup_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "gate-a"
            created = harness.create(root=root, repository=Path.cwd())
            plugin = root / "marketplace" / "plugins" / created["plugin_name"]
            launcher = plugin / "recall_gate_a_launcher.py"
            wrong_root = Path(temporary) / "not-the-plugin"
            wrong_root.mkdir()
            failed = subprocess.run(
                [str(Path.cwd() / ".venv/bin/python"), str(launcher), "mcp"],
                env={**os.environ, "PLUGIN_ROOT": str(wrong_root)},
                text=True,
                input="",
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(0, failed.returncode)
            self.assertEqual(0, harness.inspect(root=root)["live_mcp_leases"])
            self.assertEqual({"state": "removed"}, harness.cleanup(root=root))

    def test_two_generated_mcp_clients_hold_distinct_cleanup_leases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "gate-a"
            created = harness.create(root=root, repository=Path.cwd())
            plugin = root / "marketplace" / "plugins" / created["plugin_name"]
            launcher = plugin / "recall_gate_a_launcher.py"
            self.assertEqual(0, harness.inspect(root=root)["live_mcp_leases"])
            first = _McpClient(launcher, plugin); second = _McpClient(launcher, plugin)
            try:
                self.assertEqual(2, harness.inspect(root=root)["live_mcp_leases"])
                with self.assertRaises(RuntimeError): harness.cleanup(root=root)
                first.close()
                self.assertEqual(1, harness.inspect(root=root)["live_mcp_leases"])
                with self.assertRaises(RuntimeError): harness.cleanup(root=root)
                second.close()
                self.assertEqual({"state": "removed"}, harness.cleanup(root=root))
            finally:
                first.close(); second.close()

    def test_mixed_production_namespace_is_denied_by_disposable_composition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "gate-a"
            created = harness.create(root=root, repository=Path.cwd())
            plugin = root / "marketplace" / "plugins" / created["plugin_name"]
            runtime = harness.GateARuntime(
                root=root, repository=Path.cwd(),
                identity=harness._identity_from_fields(harness._read_configuration(root)["identity"]),
            )
            try:
                with patch.dict("os.environ", {"PLUGIN_ROOT": str(plugin)}, clear=False):
                    runtime.hook({
                        "hook_event_name": "UserPromptSubmit", "session_id": "session-mixed",
                        "turn_id": "turn-mixed", "cwd": str(Path.cwd()), "prompt": "native turn",
                    })
                    output = runtime.hook({
                        "hook_event_name": "PreToolUse", "session_id": "session-mixed",
                        "turn_id": "turn-mixed", "cwd": str(Path.cwd()),
                        "tool_name": "mcp__zdecision_local__show_zdecision_recall_confirmation",
                        "tool_input": {},
                    })
                self.assertEqual({}, output)
            finally:
                runtime.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
