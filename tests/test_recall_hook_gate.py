"""Trusted Hook bindings and active-Turn recall guard behavior."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from zdecision.agent.control_bindings import ControlBindingStore
from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import TestRepositoryMapping
from zdecision.agent.hooks import CONTROL_BINDING_TOOL, handle_hook
from zdecision.agent.recall_host_state import RecallGateConflict, RecallHostStore
from zdecision.agent.repository import RepositoryResolver
from zdecision.central.decision_spaces import EnabledRepository
from zdecision.ids import product_id
from zdecision.recall.session import TurnGateResult


NOW = datetime(2026, 8, 6, 4, 0, tzinfo=UTC)
ACTIVATE_RECALL_TOOL = "mcp__zdecision_local__activate_zdecision_recall"
TURN_GATE_TOOL = "mcp__zdecision_local__gate_zdecision_turn"
ACTIVATION_ID = "activation-hook-bound"
GATE_ID = "gate-hook-bound"
GATE_ID_C = "gate-hook-bound-c"
GATE_ID_C_REBASED = "gate-hook-bound-c-rebased"
CONTROL_ID = "ctl_0123456789abcdef0123456789abcdef"
PRIVATE_SENTINELS = (
    "RAW-PROMPT-SECRET",
    "TRANSCRIPT-PATH-SECRET",
    "SOURCE-SECRET",
    "DIFF-SECRET",
)
_DEFAULT_CWD = object()


def _result(*, context_epoch: int = 0, intent_epoch: int = 1) -> TurnGateResult:
    return TurnGateResult(
        disposition="retrieve",
        intent_digest="intent-a",
        context_epoch=context_epoch,
        intent_epoch=intent_epoch,
        probe=None,
    )


class RecallHookGateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
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
        self.database_path = self.root / "state" / "zdecision.sqlite3"
        self.database = AgentDatabase.open(self.database_path)
        self.addCleanup(self.database.close)
        self.recall_store = RecallHostStore.open(self.database_path)
        self.addCleanup(self.recall_store.close)
        self.control_store = ControlBindingStore.open(self.database_path)
        self.addCleanup(self.control_store.close)
        self.resolver = RepositoryResolver(timeout_seconds=0.5)
        self.snapshot = self.resolver.resolve(self.repository)
        self.assertIsNotNone(self.snapshot)
        self.database.put_test_repository_mapping(
            TestRepositoryMapping(
                repository_id=self.snapshot.repository_id,
                product_id=product_id("Recall Hook Test"),
                product_name="Recall Hook Test",
                enabled=True,
            )
        )
        self.database.put_enabled_repository(
            EnabledRepository(self.snapshot.repository_id, True)
        )

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.repository,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _handle(self, raw: object):
        return handle_hook(
            raw,
            database=self.database,
            clock=lambda: NOW,
            repository_resolver=self.resolver,
            worker_waker=lambda _: None,
            control_store=self.control_store,
            control_id_factory=lambda: CONTROL_ID,
            recall_store=self.recall_store,
            activation_binding_id_factory=lambda: ACTIVATION_ID,
            turn_gate_id_factory=(
                lambda _session_id, turn_id, context_epoch, *_: (
                    GATE_ID
                    if turn_id == "turn-b"
                    else (
                        GATE_ID_C_REBASED
                        if context_epoch == 1
                        else GATE_ID_C
                    )
                )
            ),
        )

    def _prompt(
        self,
        *,
        session_id: str = "session-a",
        turn_id: str = "turn-a",
        prompt: str = "RAW-PROMPT-SECRET",
    ):
        return self._handle(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session_id,
                "turn_id": turn_id,
                "cwd": str(self.repository),
                "prompt": prompt,
                "transcript_path": "TRANSCRIPT-PATH-SECRET",
                "source": "SOURCE-SECRET",
                "diff": "DIFF-SECRET",
            }
        )

    def _pre_tool(
        self,
        tool_name: str,
        *,
        session_id: object = "session-a",
        turn_id: object = "turn-a",
        cwd: object = _DEFAULT_CWD,
        tool_input: object | None = None,
        **extra: object,
    ):
        value: dict[str, object] = {
            "hook_event_name": "PreToolUse",
            "session_id": session_id,
            "turn_id": turn_id,
            "cwd": str(self.repository) if cwd is _DEFAULT_CWD else cwd,
            "tool_name": tool_name,
            "tool_input": (
                {
                    "activation_binding_id": "model-binding",
                    "turn_gate_id": "model-gate",
                    "session_id": "model-session",
                    "turn_id": "model-turn",
                    "cwd": "/model/cwd",
                }
                if tool_input is None
                else tool_input
            ),
        }
        value.update(extra)
        return self._handle(value)

    def _activate(self, *, session_id: str = "session-a", turn_id: str = "turn-a"):
        self._prompt(session_id=session_id, turn_id=turn_id)
        response = self._pre_tool(
            ACTIVATE_RECALL_TOOL, session_id=session_id, turn_id=turn_id
        )
        self.assertEqual("allow", self._decision(response))
        return response

    @staticmethod
    def _decision(response) -> object:
        return response.output.get("hookSpecificOutput", {}).get(
            "permissionDecision"
        )

    def assert_private_values_absent(self, response) -> None:
        encoded = json.dumps(response.output, sort_keys=True)
        for sentinel in PRIVATE_SENTINELS:
            self.assertNotIn(sentinel, encoded)
        self.assertNotIn(str(self.repository), encoded)
        for identifier in (
            "session-a",
            "session-other",
            "turn-a",
            "turn-b",
            "turn-c",
            "turn-other",
            "turn-wrong",
        ):
            self.assertNotIn(identifier, encoded)

    def test_candidate_render_keeps_its_control_id_rewrite(self) -> None:
        self._prompt()

        response = self._pre_tool(CONTROL_BINDING_TOOL)

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

    def test_activation_rejects_untrusted_host_coordinates_and_repository(self) -> None:
        self._prompt()
        cases = (
            ("missing session", {"session_id": None}),
            ("missing turn", {"turn_id": None}),
            ("missing cwd", {"cwd": None}),
            ("relative cwd", {"cwd": "relative/path"}),
            ("subagent", {"agent_id": "agent-child"}),
        )
        for name, overrides in cases:
            with self.subTest(name=name):
                response = self._pre_tool(ACTIVATE_RECALL_TOOL, **overrides)
                self.assertEqual("deny", self._decision(response))
                self.assert_private_values_absent(response)

        self.database.put_enabled_repository(
            EnabledRepository(self.snapshot.repository_id, False)
        )
        response = self._pre_tool(ACTIVATE_RECALL_TOOL)
        self.assertEqual("deny", self._decision(response))

    def test_activation_replaces_model_coordinates_with_host_binding_only(self) -> None:
        self._prompt()

        response = self._pre_tool(ACTIVATE_RECALL_TOOL)

        self.assertEqual(
            {"activation_binding_id": ACTIVATION_ID},
            response.output.get("hookSpecificOutput", {}).get("updatedInput"),
        )
        self.assertEqual("active", self.recall_store.get_session("session-a").state)
        self.assert_private_values_absent(response)

    def test_active_prompt_creates_pending_gate_and_bounded_instruction(self) -> None:
        self._activate()

        response = self._prompt(turn_id="turn-b")

        output = response.output["hookSpecificOutput"]
        self.assertEqual("UserPromptSubmit", output["hookEventName"])
        self.assertIn("gate_zdecision_turn", output["additionalContext"])
        self.assertLess(len(output["additionalContext"].encode("utf-8")), 1000)
        self.assert_private_values_absent(response)
        gate = self.recall_store.begin_turn_gate(
            session_id="session-a",
            turn_id="turn-b",
            context_epoch=0,
            intent_epoch=0,
            active_generation=None,
            gate_id=GATE_ID,
        )
        self.assertEqual("pending", gate.state)

        unselected = self._prompt(session_id="session-other", turn_id="turn-other")
        self.assertEqual({}, unselected.output)
        self.assertIsNone(self.recall_store.get_session("session-other"))

    def test_turn_gate_replaces_model_coordinates_with_bound_gate_only(self) -> None:
        self._activate()
        self._prompt(turn_id="turn-b")

        response = self._pre_tool(TURN_GATE_TOOL, turn_id="turn-b")

        self.assertEqual("allow", self._decision(response))
        self.assertEqual(
            {"turn_gate_id": GATE_ID},
            response.output["hookSpecificOutput"]["updatedInput"],
        )
        self.assert_private_values_absent(response)

    def test_mutations_wait_for_exact_committed_active_turn_gate(self) -> None:
        self._activate()
        self._prompt(turn_id="turn-b")
        guarded_tools = (
            "Bash",
            "apply_patch",
            "Edit",
            "Write",
            "Agent",
            "mcp__other__mutate",
        )
        for tool_name in guarded_tools:
            with self.subTest(tool_name=tool_name, state="pending"):
                self.assertEqual(
                    "deny",
                    self._decision(self._pre_tool(tool_name, turn_id="turn-b")),
                )

        self.recall_store.commit_turn_gate(
            session_id="session-a",
            turn_id="turn-b",
            gate_id=GATE_ID,
            result=_result(),
            active_set_digest="set-a",
        )
        for tool_name in guarded_tools:
            with self.subTest(tool_name=tool_name, state="committed"):
                self.assertEqual({}, self._pre_tool(tool_name, turn_id="turn-b").output)

    def test_unselected_and_bypassed_sessions_keep_fail_open_tools(self) -> None:
        self._prompt(session_id="session-other", turn_id="turn-other")
        self.assertEqual(
            {},
            self._pre_tool(
                "Bash", session_id="session-other", turn_id="turn-other"
            ).output,
        )

        self._activate()
        with self.recall_store._connection:
            self.recall_store._connection.execute(
                "UPDATE recall_sessions SET state = 'bypassed' WHERE session_id = ?",
                ("session-a",),
            )
        self.assertEqual({}, self._pre_tool("Bash").output)

    def test_malformed_replayed_and_cross_turn_bindings_fail_closed(self) -> None:
        self._activate()
        self._prompt(turn_id="turn-b")

        self.assertEqual(
            "deny", self._decision(self._pre_tool(TURN_GATE_TOOL, turn_id=None))
        )
        self.assertEqual(
            "deny",
            self._decision(self._pre_tool(TURN_GATE_TOOL, turn_id="turn-a")),
        )
        self.assertEqual(
            "deny", self._decision(self._pre_tool("Bash", turn_id="turn-a"))
        )

    def test_blocked_gate_keeps_active_turn_mutations_denied(self) -> None:
        self._activate()
        self._prompt(turn_id="turn-b")
        blocked = TurnGateResult(
            disposition="blocked",
            intent_digest="intent-a",
            context_epoch=0,
            intent_epoch=0,
            probe=None,
        )
        self.recall_store.commit_turn_gate(
            session_id="session-a",
            turn_id="turn-b",
            gate_id=GATE_ID,
            result=blocked,
            active_set_digest="set-a",
        )

        self.assertEqual(
            "deny", self._decision(self._pre_tool("Bash", turn_id="turn-b"))
        )

    def _commit_active_set(self) -> None:
        self._activate()
        self._prompt(turn_id="turn-b")
        self.recall_store.commit_turn_gate(
            session_id="session-a",
            turn_id="turn-b",
            gate_id=GATE_ID,
            result=_result(),
            active_set_digest="set-a",
        )

    def _compact_event(
        self,
        name: str,
        *,
        turn_id: str = "turn-b",
        trigger: str = "manual",
    ):
        return self._handle(
            {
                "hook_event_name": name,
                "session_id": "session-a",
                "turn_id": turn_id,
                "cwd": str(self.repository),
                "trigger": trigger,
                "prompt": "RAW-PROMPT-SECRET",
            }
        )

    def _session_start(self, source: str, *, turn_id: str = "turn-b"):
        return self._handle(
            {
                "hook_event_name": "SessionStart",
                "session_id": "session-a",
                "turn_id": turn_id,
                "cwd": str(self.repository),
                "source": source,
                "transcript_path": "TRANSCRIPT-PATH-SECRET",
            }
        )

    def test_compact_and_clear_restore_one_typed_replay_stable_envelope(self) -> None:
        self._commit_active_set()
        self._compact_event("PreCompact", trigger="auto")
        self._compact_event("PostCompact", trigger="auto")

        first = self._session_start("compact")
        replay = self._session_start("compact")

        self.assertEqual(first.output, replay.output)
        envelope = json.loads(
            first.output["hookSpecificOutput"]["additionalContext"]
        )
        self.assertEqual("ZDECISION_RECALL_RESTORATION", envelope["marker"])
        self.assertEqual("set-a", envelope["active_set_digest"])
        self.assertEqual(1, envelope["context_epoch"])
        self.assertEqual(1, self.recall_store.get_session("session-a").context_epoch)
        self.assert_private_values_absent(first)

        self._prompt(turn_id="turn-c")
        self.recall_store.commit_turn_gate(
            session_id="session-a",
            turn_id="turn-c",
            gate_id=GATE_ID_C_REBASED,
            result=_result(context_epoch=1),
            active_set_digest="set-a",
        )
        cleared = self._session_start("clear", turn_id="turn-c")
        clear_replay = self._session_start("clear", turn_id="turn-c")
        self.assertEqual(cleared.output, clear_replay.output)
        self.assertEqual(2, self.recall_store.get_session("session-a").context_epoch)

    def test_unmatched_compaction_token_fails_closed_without_epoch_advance(self) -> None:
        self._commit_active_set()
        self._compact_event("PreCompact")
        self._compact_event("PostCompact")

        response = self._session_start("compact", turn_id="turn-wrong")

        envelope = json.loads(
            response.output["hookSpecificOutput"]["additionalContext"]
        )
        self.assertEqual("ZDECISION_RECALL_BLOCKED", envelope["marker"])
        self.assertEqual(0, self.recall_store.get_session("session-a").context_epoch)
        self.assert_private_values_absent(response)

    def test_compact_atomically_rebases_pending_gate_for_the_same_open_turn(self) -> None:
        self._commit_active_set()
        self._prompt(turn_id="turn-c")
        self._compact_event("PreCompact", turn_id="turn-c")
        self._compact_event("PostCompact", turn_id="turn-c")

        restoration = self._session_start("compact", turn_id="turn-c")
        gate_binding = self._pre_tool(TURN_GATE_TOOL, turn_id="turn-c")

        envelope = json.loads(
            restoration.output["hookSpecificOutput"]["additionalContext"]
        )
        self.assertEqual(1, envelope["context_epoch"])
        self.assertEqual("allow", self._decision(gate_binding))
        self.assertEqual(
            {"turn_gate_id": GATE_ID_C_REBASED},
            gate_binding.output["hookSpecificOutput"]["updatedInput"],
        )
        with self.assertRaises(RecallGateConflict):
            self.recall_store.commit_turn_gate(
                session_id="session-a",
                turn_id="turn-c",
                gate_id=GATE_ID_C,
                result=_result(context_epoch=1, intent_epoch=1),
                active_set_digest="set-a",
            )
        self.recall_store.commit_turn_gate(
            session_id="session-a",
            turn_id="turn-c",
            gate_id=GATE_ID_C_REBASED,
            result=_result(context_epoch=1, intent_epoch=1),
            active_set_digest="set-a",
        )
        self.assertEqual({}, self._pre_tool("Bash", turn_id="turn-c").output)

    def test_unrelated_store_failures_fail_open_but_recall_bindings_fail_closed(
        self,
    ) -> None:
        self._prompt()
        unrelated = {
            "hook_event_name": "PreToolUse",
            "session_id": "session-unknown",
            "turn_id": "turn-unknown",
            "cwd": str(self.repository),
            "tool_name": "Bash",
            "tool_input": {"command": "true"},
        }
        activation = {
            **unrelated,
            "session_id": "session-a",
            "turn_id": "turn-a",
            "tool_name": ACTIVATE_RECALL_TOOL,
            "tool_input": {},
        }
        gate = {**activation, "tool_name": TURN_GATE_TOOL}

        with patch(
            "zdecision.agent.hooks.RecallHostStore.open",
            side_effect=RuntimeError("store unavailable"),
        ) as open_store:
            unrelated_response = handle_hook(
                unrelated,
                database=self.database,
                clock=lambda: NOW,
                repository_resolver=self.resolver,
                worker_waker=lambda _: None,
            )
            activation_response = handle_hook(
                activation,
                database=self.database,
                clock=lambda: NOW,
                repository_resolver=self.resolver,
                worker_waker=lambda _: None,
            )
            gate_response = handle_hook(
                gate,
                database=self.database,
                clock=lambda: NOW,
                repository_resolver=self.resolver,
                worker_waker=lambda _: None,
            )

        class FailingReadStore:
            def get_session(self, _session_id: str):
                raise RuntimeError("read unavailable")

        read_failure = handle_hook(
            unrelated,
            database=self.database,
            clock=lambda: NOW,
            repository_resolver=self.resolver,
            worker_waker=lambda _: None,
            recall_store=FailingReadStore(),  # type: ignore[arg-type]
        )

        self.assertEqual({}, unrelated_response.output)
        self.assertEqual({}, read_failure.output)
        self.assertEqual("deny", self._decision(activation_response))
        self.assertEqual("deny", self._decision(gate_response))
        for call in open_store.call_args_list:
            self.assertLessEqual(call.kwargs["timeout_seconds"], 0.1)

    def test_startup_resume_and_session_end_preserve_candidate_lifecycle(self) -> None:
        self._activate()
        initial_count = self.database.count_events()

        startup = self._session_start("startup")
        self.assertEqual(0, self.recall_store.get_session("session-a").context_epoch)
        ended = self._handle(
            {
                "hook_event_name": "SessionEnd",
                "session_id": "session-a",
                "cwd": str(self.repository),
                "reason": "other",
                "prompt": "RAW-PROMPT-SECRET",
            }
        )
        self.assertTrue(ended.event_id)
        self.assertEqual("dormant", self.recall_store.get_session("session-a").state)
        resumed = self._session_start("resume")
        self.assertEqual("activating", self.recall_store.get_session("session-a").state)
        self.assertEqual(0, self.recall_store.get_session("session-a").context_epoch)
        self.assertEqual("deny", self._decision(self._pre_tool("Bash")))
        self.assertEqual(initial_count + 3, self.database.count_events())
        for response in (startup, ended, resumed):
            self.assert_private_values_absent(response)


if __name__ == "__main__":
    unittest.main()
