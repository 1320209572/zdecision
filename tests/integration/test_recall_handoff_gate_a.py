"""Production-boundary checks for the disposable Recall Gate A harness."""

from __future__ import annotations

import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
import os
import subprocess
from unittest.mock import patch

from tests.integration import recall_gate_a_desktop_harness as harness
from zdecision.agent.mcp_server import RECALL_CONFIRMATION_PATH
from zdecision.recall.session import RecallIntent


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
                card = runtime.recall_tools.show_recall_confirmation(
                    activation_attempt_id=attempt_id,
                    intent=intent.to_dict(),
                    ui_digest=sha256(RECALL_CONFIRMATION_PATH.read_bytes()).hexdigest(),
                )
                self.assertEqual("pending_confirmation", card["state"])
                enabled = runtime.recall_tools.decide_recall_confirmation(
                    activation_attempt_id=attempt_id, action="enable",
                    current_ui_digest=sha256(RECALL_CONFIRMATION_PATH.read_bytes()).hexdigest(),
                )
                delivery = runtime.store.delivery_for_attempt(attempt_id)
                self.assertEqual("delivery_claimed", enabled["state"])
                self.assertEqual(1, runtime.provider.preflight_calls)
                self.assertEqual(1, runtime.provider.retrieve_calls)
                runtime.recall_tools.ack_recall_delivery(
                    activation_attempt_id=attempt_id, delivery_id=delivery.delivery_id,
                    context_digest=delivery.context_digest,
                )
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
                applied = runtime.recall_tools.apply_recall_delivery(
                    turn_gate_id=harness.gate_id_for_turn("turn-apply"), delivery_id=delivery.delivery_id, items=items
                )
                self.assertEqual("application_committed", applied["state"])
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
                self.assertEqual(1, runtime.provider.retrieve_calls)

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
