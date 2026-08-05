from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from zdecision.agent.control_bindings import ControlBindingStore
from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import TestRepositoryMapping
from zdecision.agent.hooks import handle_control_binding_hook, handle_hook
from zdecision.agent.repository import RepositoryResolver
from zdecision.ids import product_id


NOW = datetime(2026, 7, 31, 3, 0, tzinfo=UTC)
CONTROL_ID = "ctl_0123456789abcdef0123456789abcdef"
MODEL_CONTROL_ID = "ctl_ffffffffffffffffffffffffffffffff"
TOOL_NAME = "mcp__zdecision_local__show_zdecision_update"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DENIED_OUTPUT = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
    }
}


class FailingControlStore:
    def __init__(self) -> None:
        self.closed = False

    def create_binding(self, **_: object) -> None:
        raise RuntimeError("persistence unavailable")

    def close(self) -> None:
        self.closed = True


class RecordingControlStore:
    def __init__(self) -> None:
        self.control_ids: list[object] = []

    def create_binding(self, **values: object) -> None:
        self.control_ids.append(values["control_id"])


class ControlBindingHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "tests@example.com")
        self._git("config", "user.name", "ZDecision Tests")
        (self.repository / "README.md").write_text("fixture\n", "utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "fixture")
        self._git(
            "remote", "add", "origin", "https://github.com/OpenAI/example.git"
        )
        self.state_root = self.root / "state"
        self.database_path = self.state_root / "agent" / "zdecision.sqlite3"
        self.database = AgentDatabase.open(self.database_path)
        self.addCleanup(self.database.close)
        self.control_store = ControlBindingStore.open(self.database_path)
        self.addCleanup(self.control_store.close)
        self.repository_resolver = RepositoryResolver(timeout_seconds=0.5)
        self.snapshot = self.repository_resolver.resolve(self.repository)
        self.assertIsNotNone(self.snapshot)
        self.mapping = TestRepositoryMapping(
            repository_id=self.snapshot.repository_id,
            product_id=product_id("Example"),
            product_name="Example",
            enabled=True,
        )
        self.database.put_test_repository_mapping(self.mapping)

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.repository,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _raw(self, **overrides: object) -> dict[str, object]:
        raw: dict[str, object] = {
            "hook_event_name": "PreToolUse",
            "session_id": "session_a",
            "turn_id": "turn_a",
            "cwd": str(self.repository),
            "tool_name": TOOL_NAME,
            "tool_input": {
                "control_id": MODEL_CONTROL_ID,
                "discarded": "TOOL-INPUT-SECRET",
            },
            "model_payload": "MODEL-SECRET",
        }
        raw.update(overrides)
        return raw

    def _handle(self, raw: object):
        return handle_hook(
            raw,
            database=self.database,
            clock=lambda: NOW,
            repository_resolver=self.repository_resolver,
            control_store=self.control_store,
            control_id_factory=lambda: CONTROL_ID,
            worker_waker=lambda _: self.fail("must not wake worker"),
        )

    def _observe_prompt(
        self,
        *,
        session_id: str = "session_a",
        turn_id: str = "turn_a",
        cwd: Path | None = None,
    ) -> None:
        response = handle_hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session_id,
                "turn_id": turn_id,
                "cwd": str(cwd or self.repository),
            },
            database=self.database,
            clock=lambda: NOW,
            repository_resolver=self.repository_resolver,
            worker_waker=lambda _: None,
        )
        self.assertNotEqual("", response.event_id)

    def _end_session(self, *, session_id: str = "session_a") -> None:
        response = handle_hook(
            {
                "hook_event_name": "SessionEnd",
                "session_id": session_id,
                "cwd": str(self.repository),
                "reason": "other",
            },
            database=self.database,
            clock=lambda: NOW,
            repository_resolver=self.repository_resolver,
            worker_waker=lambda _: None,
        )
        self.assertNotEqual("", response.event_id)

    def test_trusted_envelope_replaces_all_model_input_with_local_binding(self) -> None:
        self._observe_prompt()
        event_count = self.database.count_events()

        response = self._handle(self._raw())

        self.assertEqual(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": {"control_id": CONTROL_ID},
                }
            },
            response.output,
        )
        self.assertEqual("", response.event_id)
        self.assertEqual(event_count, self.database.count_events())
        binding = self.control_store.get(CONTROL_ID)
        self.assertIsNotNone(binding)
        self.assertEqual("session_a", binding.session_id)
        self.assertEqual("turn_a", binding.render_turn_id)
        self.assertEqual(str(self.repository), binding.cwd)
        self.assertEqual(self.snapshot.repository_id, binding.repository_id)
        self.assertEqual(self.mapping.product_id, binding.product_id)
        self.assertEqual("2026-07-31T03:00:00.000000Z", binding.created_at)
        self.assertEqual("2026-07-31T03:15:00.000000Z", binding.expires_at)
        database_bytes = b"".join(
            path.read_bytes()
            for path in self.database_path.parent.iterdir()
            if path.is_file()
        )
        self.assertNotIn(MODEL_CONTROL_ID.encode(), database_bytes)
        self.assertNotIn(b"TOOL-INPUT-SECRET", database_bytes)
        self.assertNotIn(b"MODEL-SECRET", database_bytes)

    def test_untrusted_or_unavailable_envelopes_are_denied(self) -> None:
        unregistered = self.root / "unregistered"
        unregistered.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=unregistered, check=True,
            capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "tests@example.com"],
            cwd=unregistered, check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "ZDecision Tests"],
            cwd=unregistered, check=True, capture_output=True, text=True,
        )
        (unregistered / "README.md").write_text("fixture\n", "utf-8")
        subprocess.run(
            ["git", "add", "README.md"], cwd=unregistered, check=True,
            capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "fixture"], cwd=unregistered, check=True,
            capture_output=True, text=True,
        )
        subprocess.run(
            [
                "git", "remote", "add", "origin",
                "https://github.com/OpenAI/unregistered.git",
            ],
            cwd=unregistered, check=True, capture_output=True, text=True,
        )
        cases = (
            ("unresolved", self._raw(cwd=str(self.root / "missing"))),
            ("unregistered", self._raw(cwd=str(unregistered))),
            ("subagent", self._raw(agent_id="agent_child")),
            ("missing session", self._raw(session_id=None)),
            ("unsafe session", self._raw(session_id="session with spaces")),
            ("missing turn", self._raw(turn_id=None)),
            ("relative cwd", self._raw(cwd="relative/path")),
            ("wrong tool", self._raw(tool_name="mcp__other__render")),
        )

        for name, raw in cases:
            with self.subTest(name=name):
                self.assertEqual(DENIED_OUTPUT, self._handle(raw).output)
                self.assertIsNone(self.control_store.get(CONTROL_ID))

        self._observe_prompt()
        self.database.put_test_repository_mapping(replace(self.mapping, enabled=False))
        self.assertEqual(DENIED_OUTPUT, self._handle(self._raw()).output)
        self.assertIsNone(self.control_store.get(CONTROL_ID))

    def test_unobserved_envelope_is_denied(self) -> None:
        self.assertEqual(DENIED_OUTPUT, self._handle(self._raw()).output)
        self.assertIsNone(self.control_store.get(CONTROL_ID))

    def test_different_session_in_same_cwd_is_denied(self) -> None:
        self._observe_prompt(session_id="session_b")

        self.assertEqual(DENIED_OUTPUT, self._handle(self._raw()).output)
        self.assertIsNone(self.control_store.get(CONTROL_ID))

    def test_wrong_or_superseded_turn_is_denied(self) -> None:
        self._observe_prompt(turn_id="turn_old")
        self.assertEqual(DENIED_OUTPUT, self._handle(self._raw()).output)

        self._observe_prompt(turn_id="turn_new")
        self.assertEqual(
            DENIED_OUTPUT,
            self._handle(self._raw(turn_id="turn_old")).output,
        )
        self.assertIsNone(self.control_store.get(CONTROL_ID))

    def test_ended_session_is_denied(self) -> None:
        self._observe_prompt()
        self._end_session()

        self.assertEqual(DENIED_OUTPUT, self._handle(self._raw()).output)
        self.assertIsNone(self.control_store.get(CONTROL_ID))

    def test_persistence_failure_is_silent_and_injected_store_stays_open(self) -> None:
        self._observe_prompt()
        store = FailingControlStore()

        response = handle_control_binding_hook(
            self._raw(),
            database=self.database,
            clock=lambda: NOW,
            repository_resolver=self.repository_resolver,
            control_store=store,
            control_id_factory=lambda: CONTROL_ID,
        )

        self.assertEqual(DENIED_OUTPUT, response.output)
        self.assertFalse(store.closed)

    def test_invalid_generated_control_ids_are_rejected_before_persistence(self) -> None:
        self._observe_prompt()
        invalid_ids: tuple[object, ...] = (
            "ctl_not-canonical",
            "ctl_0123456789ABCDEF0123456789ABCDEF",
            123,
        )

        for invalid_id in invalid_ids:
            with self.subTest(invalid_id=invalid_id):
                store = RecordingControlStore()
                response = handle_control_binding_hook(
                    self._raw(),
                    database=self.database,
                    clock=lambda: NOW,
                    repository_resolver=self.repository_resolver,
                    control_store=store,
                    control_id_factory=lambda: invalid_id,  # type: ignore[return-value]
                )

                self.assertEqual(DENIED_OUTPUT, response.output)
                self.assertEqual([], store.control_ids)

    def test_handle_hook_closes_its_temporary_store_on_success_and_failure(self) -> None:
        self._observe_prompt()
        for fails in (False, True):
            with self.subTest(fails=fails):
                store = FailingControlStore()
                if not fails:
                    store.create_binding = lambda **_: None
                with patch(
                    "zdecision.agent.hooks.ControlBindingStore.open",
                    return_value=store,
                ):
                    response = handle_hook(
                        self._raw(),
                        database=self.database,
                        clock=lambda: NOW,
                        repository_resolver=self.repository_resolver,
                        control_id_factory=lambda: CONTROL_ID,
                        worker_waker=lambda _: self.fail("must not wake worker"),
                    )

                expected = (
                    DENIED_OUTPUT
                    if fails
                    else {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "allow",
                            "updatedInput": {"control_id": CONTROL_ID},
                        }
                    }
                )
                self.assertEqual(expected, response.output)
                self.assertTrue(store.closed)

    def test_cli_pre_tool_hook_does_not_load_unrelated_runtime_stacks(self) -> None:
        """A render hook must not load MCP, HTTP, or worker runtime stacks."""

        self._observe_prompt()

        guard_directory = self.root / "import-guard"
        guard_directory.mkdir()
        (guard_directory / "sitecustomize.py").write_text(
            """
import builtins

_original_import = builtins.__import__
_blocked_modules = (
    "httpx",
    "mcp",
    "zdecision.agent.central_client",
    "zdecision.agent.launchd",
    "zdecision.agent.mcp_server",
    "zdecision.agent.service",
    "zdecision.agent.worker",
)


def _guarded_import(name, *arguments, **keyword_arguments):
    if name in _blocked_modules or name.startswith(
        tuple(f"{module}." for module in _blocked_modules)
    ):
        raise ImportError(f"blocked import: {name}")
    return _original_import(name, *arguments, **keyword_arguments)


builtins.__import__ = _guarded_import
""".lstrip(),
            "utf-8",
        )
        environment = os.environ.copy()
        existing_python_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(
            value
            for value in (str(guard_directory), existing_python_path)
            if value
        )
        environment["ZDECISION_STATE_DIR"] = str(self.state_root)

        result = subprocess.run(
            [str(REPOSITORY_ROOT / ".venv" / "bin" / "zdecision-agent"), "hook"],
            cwd=self.repository,
            input=json.dumps(self._raw()),
            capture_output=True,
            text=True,
            env=environment,
            timeout=3,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        output = json.loads(result.stdout)
        hook_output = output["hookSpecificOutput"]
        self.assertEqual("PreToolUse", hook_output["hookEventName"])
        self.assertEqual("allow", hook_output["permissionDecision"])
        control_id = hook_output["updatedInput"]["control_id"]
        self.assertRegex(control_id, r"^ctl_[0-9a-f]{32}$")
        self.assertNotEqual(MODEL_CONTROL_ID, control_id)
        binding = self.control_store.get(control_id)
        self.assertIsNotNone(binding)
        self.assertEqual(self.snapshot.repository_id, binding.repository_id)


if __name__ == "__main__":
    unittest.main()
