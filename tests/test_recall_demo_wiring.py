"""Process-boundary wiring for the bounded local Recall demo."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from zdecision.agent import cli
from zdecision.agent import mcp_server
from zdecision.agent.hooks import HookResponse
from zdecision.recall.demo import provider as demo_provider
from zdecision.recall.provider import UnavailableRecallProvider
from zdecision.recall.session import RecallIntent


class _CapturedOutput:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, value: str) -> int:
        return self.buffer.write(value.encode("utf-8"))

    def flush(self) -> None:
        return None


class _StoppedMcpServer:
    def run(self, *, transport: str) -> None:
        if transport != "stdio":
            raise AssertionError("MCP must use stdio")


class RecallDemoWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.state_root = self.root / "state"
        self.config_path = self.state_root / "agent" / "recall-demo.json"

    def _run_hook(self, value: dict[str, object]) -> dict[str, object]:
        stdout = _CapturedOutput()
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(value).encode("utf-8")))
        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            self.assertEqual(0, cli.main(["hook"]))
        return json.loads(stdout.buffer.getvalue())

    def _run_mcp(self):
        captured: list[object] = []

        def create(_local_tools: object, recall_tools: object) -> _StoppedMcpServer:
            captured.append(recall_tools)
            return _StoppedMcpServer()

        with patch.object(mcp_server, "create_mcp_server", side_effect=create):
            self.assertEqual(0, cli.main(["mcp"]))
        return captured[0]

    def test_hook_and_mcp_load_provider_from_same_config_path(self) -> None:
        """A process boundary regression cannot select differing provider files."""
        with (
            patch.dict(
                os.environ,
                {"ZDECISION_STATE_DIR": str(self.state_root)},
                clear=True,
            ),
            patch(
                "zdecision.recall.demo.provider.configured_recall_provider",
                return_value=UnavailableRecallProvider(),
            ) as configured,
        ):
            self._run_hook({"hook_event_name": "PreToolUse"})
            self._run_mcp()

        self.assertEqual(
            [((self.config_path,), {}), ((self.config_path,), {})],
            [(call.args, call.kwargs) for call in configured.call_args_list],
        )

    def test_missing_config_keeps_unavailable_provider_in_both_processes(self) -> None:
        """Absent owner state stays bounded instead of opening a fallback provider."""
        with patch.dict(
            os.environ,
            {"ZDECISION_STATE_DIR": str(self.state_root)},
            clear=True,
        ), patch(
            "zdecision.agent.hooks.handle_hook",
            return_value=HookResponse(event_id="", output={}),
        ) as handle_hook:
            self._run_hook({"hook_event_name": "PreToolUse"})
            recall_tools = self._run_mcp()

        hook_provider = handle_hook.call_args.kwargs["recall_provider"]
        mcp_provider = recall_tools.handoff_service.provider
        intent = RecallIntent.from_dict(
            {
                "target_decision_space_ids": ["prod_" + "3e6e73b8defbfee89ce7bf26e739b1dc"],
                "explicit_multi_space": False,
                "feature_goal": "Bounded unavailable provider check",
                "domain_objects": ["RecallProvider"],
                "repository_relative_paths": ["src/zdecision/agent/cli.py"],
                "constraints": ["owner-only configuration"],
                "exclusions": ["fallback configuration"],
            }
        )
        for provider in (hook_provider, mcp_provider):
            self.assertIsInstance(provider, UnavailableRecallProvider)
            self.assertEqual(
                "recall_not_ready",
                provider.preflight(
                    repository_id="repo_" + "1" * 32,
                    repository_display_name="zstack-ui-next",
                    intent=intent,
                    now=datetime.now(UTC),
                ).code,
            )

    def test_invalid_config_never_falls_back_to_private_argument_or_environment(self) -> None:
        """A corrupt fixed config cannot select a model- or environment-provided path."""
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text("not valid json", "utf-8")
        self.config_path.chmod(0o600)
        private_path = self.root / "private-recall-demo.json"
        private_path.write_text("{}", "utf-8")
        private_path.chmod(0o600)
        with (
            patch.dict(
                os.environ,
                {
                    "ZDECISION_STATE_DIR": str(self.state_root),
                    "RECALL_DEMO_CONFIG_PATH": str(private_path),
                    "RECALL_DEMO_CONFIG": str(private_path),
                },
                clear=True,
            ),
            patch(
                "zdecision.recall.demo.provider.configured_recall_provider",
                wraps=demo_provider.configured_recall_provider,
            ) as configured,
        ):
            self._run_hook({"hook_event_name": "PreToolUse", "config_path": str(private_path)})
            self._run_mcp()

        self.assertEqual(
            [self.config_path, self.config_path],
            [call.args[0] for call in configured.call_args_list],
        )
        self.assertNotIn(private_path, [call.args[0] for call in configured.call_args_list])


if __name__ == "__main__":
    unittest.main()
