"""Trusted Hook bindings and active-Turn recall guard behavior."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from dataclasses import replace
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
from zdecision.recall.handoff import (
    RecallApplicationSubmission,
    RecallPreflightClarification,
    RecallPreflightReady,
    RecallPreflightUnavailable,
    RecallShortlist,
    RecalledDecision,
    build_handoff_context,
)
from zdecision.recall.session import RecallIntent, TurnGateResult
from tests.test_recall_handoff_contracts import formal_decision


NOW = datetime(2026, 8, 6, 4, 0, tzinfo=UTC)
ACTIVATE_RECALL_TOOL = "mcp__zdecision_local__show_zdecision_recall_confirmation"
TURN_GATE_TOOL = "mcp__zdecision_local__gate_zdecision_turn"
APPLY_RECALL_DELIVERY_TOOL = (
    "mcp__zdecision_local__apply_zdecision_recall_delivery"
)
DELIVERY_ID = "delivery_" + "d" * 32
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
VALID_INTENT: dict[str, object] = {
    "target_decision_space_ids": ["dsp_" + "1" * 32],
    "explicit_multi_space": False,
    "feature_goal": "Continue the current product work",
    "domain_objects": ["RecallIntent"],
    "repository_relative_paths": ["src/zdecision/agent"],
    "constraints": ["Apply only relevant formal decisions"],
    "exclusions": ["Candidate generation"],
}


def _ready_preflight(*, repository_id: str, repository_display_name: str):
    return RecallPreflightReady(
        repository_id=repository_id,
        repository_display_name=repository_display_name,
        intent=RecallIntent.from_dict(VALID_INTENT),
        target_decision_space_ids=("dsp_" + "1" * 32,),
        target_display_names=("Recall Hook Test",),
        catalog_digest="a" * 64,
        generation=7,
        generation_digest="b" * 64,
        retrieval_profile_digest="c" * 64,
        index_generation=5,
        freshness="ready",
        expires_at="2026-08-06T05:00:00Z",
    )


class PreflightProvider:
    def __init__(self, result: object) -> None:
        self.result = result
        self.preflight_calls = 0
        self.retrieve_calls = 0

    def preflight(self, **_kwargs: object):
        self.preflight_calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def retrieve(self, _preflight: RecallPreflightReady):
        self.retrieve_calls += 1
        raise AssertionError("Hook preflight must not retrieve")


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
        self.plugin_root = self.root / "plugin-cache/zdecision/0.1.0"
        recall_skill = self.plugin_root / "skills/zdecision/SKILL.md"
        recall_skill.parent.mkdir(parents=True)
        recall_skill.write_text("---\nname: zdecision\n---\n", "utf-8")
        (self.plugin_root / ".codex-plugin").mkdir()
        (self.plugin_root / ".codex-plugin/plugin.json").write_text(
            json.dumps(
                {
                    "name": "zdecision",
                    "version": "0.1.0",
                    "skills": "./skills/",
                    "mcpServers": "./.mcp.json",
                }
            ),
            "utf-8",
        )
        (self.plugin_root / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "zdecision-local": {
                            "command": "zdecision-agent",
                            "args": ["mcp"],
                        }
                    }
                }
            ),
            "utf-8",
        )
        environment = patch.dict(
            "os.environ", {"PLUGIN_ROOT": str(self.plugin_root)}, clear=False
        )
        environment.start()
        self.addCleanup(environment.stop)
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

    def _handle(
        self,
        raw: object,
        *,
        now: datetime = NOW,
        recall_provider: object | None = None,
    ):
        return handle_hook(
            raw,
            database=self.database,
            clock=lambda: now,
            repository_resolver=self.resolver,
            worker_waker=lambda _: None,
            control_store=self.control_store,
            control_id_factory=lambda: CONTROL_ID,
            recall_store=self.recall_store,
            recall_provider=recall_provider,
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
        now: datetime = NOW,
        recall_provider: object | None = None,
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
                    "intent": dict(VALID_INTENT),
                }
                if tool_input is None
                else tool_input
            ),
        }
        value.update(extra)
        return self._handle(value, now=now, recall_provider=recall_provider)

    def _activate(self, *, session_id: str = "session-a", turn_id: str = "turn-a"):
        self._prompt(session_id=session_id, turn_id=turn_id)
        return self.recall_store.bind_activation(
            session_id=session_id,
            turn_id=turn_id,
            cwd=str(self.repository),
            binding_id=ACTIVATION_ID,
            now=NOW,
            plugin_root=str(self.plugin_root),
        )

    def _deliver_handoff(self, *, unknown: bool = False):
        self._prompt()
        intent = RecallIntent.from_dict(VALID_INTENT)
        preflight = _ready_preflight(
            repository_id=self.snapshot.repository_id,
            repository_display_name=self.repository.name,
        )
        first = RecalledDecision.create(
            decision_space_id=preflight.target_decision_space_ids[0],
            revision=formal_decision(),
            match_reason="Exact product match",
        )
        second = RecalledDecision.create(
            decision_space_id=preflight.target_decision_space_ids[0],
            revision=replace(
                formal_decision(claim="A second decision remains complete."),
                decision_id="dec_" + "9" * 32,
            ),
            match_reason="Exact capability match",
        )
        shortlist = RecallShortlist.create(
            preflight=preflight,
            items=(first, second),
        )
        attempt = self.recall_store.create_activation_attempt(
            session_id="session-a",
            turn_id="turn-a",
            cwd=str(self.repository),
            repository_id=preflight.repository_id,
            repository_display_name=preflight.repository_display_name,
            attempt_id="activation_" + "a" * 32,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=str(self.plugin_root),
            intent=intent,
            preflight=preflight,
        )
        self.recall_store.attach_activation_card(attempt.attempt_id, ui_digest="a" * 64)
        claim = self.recall_store.begin_delivery(
            attempt_id=attempt.attempt_id,
            delivery_id=DELIVERY_ID,
            claim_token="claim_" + "c" * 32,
            current_ui_digest="a" * 64,
            now=NOW,
            claim_expires_at=NOW + timedelta(seconds=30),
        )
        prepared = self.recall_store.commit_prepared_delivery(
            delivery_id=DELIVERY_ID,
            claim_token=claim.claim_token,
            shortlist=shortlist,
            context_text=build_handoff_context(DELIVERY_ID, preflight, shortlist),
            now=NOW,
        )
        if unknown:
            delivery = self.recall_store.mark_delivery_unknown(
                delivery_id=DELIVERY_ID,
                now=NOW + timedelta(seconds=30),
            )
        else:
            delivery = self.recall_store.ack_delivery(
                delivery_id=DELIVERY_ID,
                context_digest=prepared.context_digest,
                now=NOW,
            )
        items = [
            {
                "decision_id": item.revision.decision_id,
                "revision": item.revision.revision,
                "digest": item.digest,
                "disposition": disposition,
                "reason": "Bounded local reason",
            }
            for item, disposition in zip(
                shortlist.items,
                ("applicable", "not_applicable"),
                strict=True,
            )
        ]
        return preflight, delivery, items

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

    def test_confirmation_render_replaces_model_coordinates_with_host_attempt_only(self) -> None:
        self._prompt()
        provider = PreflightProvider(
            _ready_preflight(
                repository_id=self.snapshot.repository_id,
                repository_display_name=self.repository.name,
            )
        )

        response = self._pre_tool(
            ACTIVATE_RECALL_TOOL,
            tool_input={
                "activation_attempt_id": "model-attempt",
                "intent": dict(VALID_INTENT),
            },
            recall_provider=provider,
        )

        self.assertEqual(
            {"activation_attempt_id": ACTIVATION_ID, "intent": VALID_INTENT},
            response.output.get("hookSpecificOutput", {}).get("updatedInput"),
        )
        self.assertIsNone(self.recall_store.get_session("session-a"))
        attempt = self.recall_store.get_activation_attempt(ACTIVATION_ID)
        self.assertEqual("pending_confirmation", attempt.state)
        self.assertEqual(self.repository.name, attempt.repository_display_name)
        self.assertEqual(VALID_INTENT, attempt.preflight.intent.to_dict())
        self.assert_private_values_absent(response)

    def test_confirmation_ready_preflight_freezes_intent_and_trusted_attempt(self) -> None:
        self._prompt()
        preflight = _ready_preflight(
            repository_id=self.snapshot.repository_id,
            repository_display_name=self.repository.name,
        )
        provider = PreflightProvider(preflight)

        response = self._pre_tool(
            ACTIVATE_RECALL_TOOL,
            tool_input={
                "activation_attempt_id": "model-value",
                "intent": dict(VALID_INTENT),
            },
            recall_provider=provider,
        )

        self.assertEqual("allow", self._decision(response))
        self.assertEqual(
            {
                "activation_attempt_id": ACTIVATION_ID,
                "intent": VALID_INTENT,
            },
            response.output["hookSpecificOutput"]["updatedInput"],
        )
        attempt = self.recall_store.get_activation_attempt(ACTIVATION_ID)
        self.assertEqual(preflight, attempt.preflight)
        self.assertEqual(1, provider.preflight_calls)
        self.assertEqual(0, provider.retrieve_calls)

    def test_confirmation_clarification_denies_without_attempt_or_retrieval(self) -> None:
        self._prompt()
        provider = PreflightProvider(
            RecallPreflightClarification(
                code="ambiguous_target",
                candidate_display_names=("ZStack UI", "ZStack Cloud"),
            )
        )

        response = self._pre_tool(
            ACTIVATE_RECALL_TOOL,
            tool_input={"intent": dict(VALID_INTENT)},
            recall_provider=provider,
        )

        self.assertEqual("deny", self._decision(response))
        reason = response.output["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("ZStack UI", reason)
        self.assertIn("ZStack Cloud", reason)
        self.assertNotIn("ambiguous_target", reason)
        self.assertIsNone(self.recall_store.get_activation_attempt(ACTIVATION_ID))
        self.assertEqual(1, provider.preflight_calls)
        self.assertEqual(0, provider.retrieve_calls)

    def test_confirmation_unavailable_and_provider_failure_are_generic_denials(self) -> None:
        self._prompt()
        default_response = self._pre_tool(
            ACTIVATE_RECALL_TOOL,
            tool_input={"intent": dict(VALID_INTENT)},
        )
        self.assertEqual("deny", self._decision(default_response))
        self.assertIn(
            "Do not retry",
            default_response.output["hookSpecificOutput"][
                "permissionDecisionReason"
            ],
        )
        self.assertIsNone(self.recall_store.get_activation_attempt(ACTIVATION_ID))
        providers = (
            PreflightProvider(
                RecallPreflightUnavailable(code="private_cache_coordinate")
            ),
            PreflightProvider(RuntimeError("private provider failure")),
        )

        for provider in providers:
            with self.subTest(result=type(provider.result).__name__):
                response = self._pre_tool(
                    ACTIVATE_RECALL_TOOL,
                    tool_input={"intent": dict(VALID_INTENT)},
                    recall_provider=provider,
                )
                self.assertEqual("deny", self._decision(response))
                reason = response.output["hookSpecificOutput"][
                    "permissionDecisionReason"
                ]
                self.assertIn("Do not retry", reason)
                self.assertNotIn("private", reason)
                self.assertNotIn(str(self.repository), reason)
                self.assertIsNone(
                    self.recall_store.get_activation_attempt(ACTIVATION_ID)
                )
                self.assertEqual(0, provider.retrieve_calls)

    def test_confirmation_requires_exact_outer_and_seven_field_intent(self) -> None:
        self._prompt()
        preflight = _ready_preflight(
            repository_id=self.snapshot.repository_id,
            repository_display_name=self.repository.name,
        )
        invalid_inputs = (
            {},
            {"activation_attempt_id": "model-value"},
            {"intent": dict(VALID_INTENT), "extra": "not-allowed"},
            {
                "intent": {
                    key: value
                    for key, value in VALID_INTENT.items()
                    if key != "feature_goal"
                }
            },
            {"intent": {**VALID_INTENT, "extra": "not-allowed"}},
            {"intent": {**VALID_INTENT, "explicit_multi_space": "false"}},
        )

        for tool_input in invalid_inputs:
            with self.subTest(tool_input=tool_input):
                provider = PreflightProvider(preflight)
                response = self._pre_tool(
                    ACTIVATE_RECALL_TOOL,
                    tool_input=tool_input,
                    recall_provider=provider,
                )
                self.assertEqual("deny", self._decision(response))
                self.assertEqual(0, provider.preflight_calls)
                self.assertIsNone(
                    self.recall_store.get_activation_attempt(ACTIVATION_ID)
                )

    def test_confirmation_runs_preflight_only_after_trusted_bundle_gate(self) -> None:
        self._prompt()
        provider = PreflightProvider(
            _ready_preflight(
                repository_id=self.snapshot.repository_id,
                repository_display_name=self.repository.name,
            )
        )

        with patch.dict(
            "os.environ", {"PLUGIN_ROOT": str(self.repository)}, clear=False
        ):
            response = self._pre_tool(
                ACTIVATE_RECALL_TOOL,
                tool_input={"intent": dict(VALID_INTENT)},
                recall_provider=provider,
            )

        self.assertEqual("deny", self._decision(response))
        self.assertEqual(0, provider.preflight_calls)
        self.assertIsNone(self.recall_store.get_activation_attempt(ACTIVATION_ID))

        self.database.put_enabled_repository(
            EnabledRepository(self.snapshot.repository_id, False)
        )
        disabled = self._pre_tool(
            ACTIVATE_RECALL_TOOL,
            tool_input={"intent": dict(VALID_INTENT)},
            recall_provider=provider,
        )
        self.assertEqual("deny", self._decision(disabled))
        self.assertEqual(0, provider.preflight_calls)

    def test_confirmation_cross_task_replay_cannot_rebind_frozen_attempt(self) -> None:
        self._prompt()
        preflight = _ready_preflight(
            repository_id=self.snapshot.repository_id,
            repository_display_name=self.repository.name,
        )
        provider = PreflightProvider(preflight)
        first = self._pre_tool(
            ACTIVATE_RECALL_TOOL,
            tool_input={"intent": dict(VALID_INTENT)},
            recall_provider=provider,
        )
        self._prompt(session_id="session-other", turn_id="turn-other")

        replay = self._pre_tool(
            ACTIVATE_RECALL_TOOL,
            session_id="session-other",
            turn_id="turn-other",
            tool_input={
                "activation_attempt_id": ACTIVATION_ID,
                "intent": dict(VALID_INTENT),
            },
            recall_provider=provider,
        )

        self.assertEqual("allow", self._decision(first))
        self.assertEqual("deny", self._decision(replay))
        attempt = self.recall_store.get_activation_attempt(ACTIVATION_ID)
        self.assertEqual("session-a", attempt.session_id)
        self.assert_private_values_absent(replay)

    def test_confirmation_binding_waits_through_a_short_sqlite_writer_lock(
        self,
    ) -> None:
        """This catches the explicit confirmation path using the generic 50ms budget."""

        self._prompt()
        lock_acquired = threading.Event()
        holder_errors: list[BaseException] = []

        def hold_writer_lock() -> None:
            connection = sqlite3.connect(self.database_path, timeout=1.0)
            try:
                connection.execute("BEGIN IMMEDIATE")
                lock_acquired.set()
                time.sleep(0.2)
                connection.rollback()
            except BaseException as error:
                holder_errors.append(error)
                lock_acquired.set()
            finally:
                connection.close()

        holder = threading.Thread(target=hold_writer_lock)
        holder.start()
        self.addCleanup(holder.join, 2.0)
        self.assertTrue(lock_acquired.wait(1.0), "writer lock was not acquired")

        provider = PreflightProvider(
            _ready_preflight(
                repository_id=self.snapshot.repository_id,
                repository_display_name=self.repository.name,
            )
        )
        response = handle_hook(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "session-a",
                "turn_id": "turn-a",
                "cwd": str(self.repository),
                "tool_name": ACTIVATE_RECALL_TOOL,
                "tool_input": {
                    "activation_attempt_id": "model-binding",
                    "intent": dict(VALID_INTENT),
                },
            },
            database=self.database,
            clock=lambda: NOW,
            repository_resolver=self.resolver,
            worker_waker=lambda _: None,
            recall_provider=provider,
            activation_attempt_id_factory=lambda: ACTIVATION_ID,
        )
        holder.join(2.0)

        self.assertEqual([], holder_errors)
        self.assertEqual("allow", self._decision(response))
        self.assertEqual(
            {"activation_attempt_id": ACTIVATION_ID, "intent": VALID_INTENT},
            response.output["hookSpecificOutput"]["updatedInput"],
        )
        verifier = RecallHostStore.open(self.database_path)
        self.addCleanup(verifier.close)
        attempt = verifier.get_activation_attempt(ACTIVATION_ID)
        self.assertIsNotNone(attempt)
        self.assertEqual("pending_confirmation", attempt.state)

    def test_confirmation_render_replay_keeps_the_frozen_attempt(self) -> None:
        """This catches a normal retried render being denied after the clock advances."""

        self._prompt()
        provider = PreflightProvider(
            _ready_preflight(
                repository_id=self.snapshot.repository_id,
                repository_display_name=self.repository.name,
            )
        )
        first = self._pre_tool(
            ACTIVATE_RECALL_TOOL,
            tool_input={"intent": dict(VALID_INTENT)},
            now=NOW,
            recall_provider=provider,
        )
        attempt = self.recall_store.get_activation_attempt(ACTIVATION_ID)

        replay = self._pre_tool(
            ACTIVATE_RECALL_TOOL,
            tool_input={"intent": dict(VALID_INTENT)},
            now=NOW.replace(minute=NOW.minute + 1),
            recall_provider=provider,
        )
        frozen = self.recall_store.get_activation_attempt(ACTIVATION_ID)

        self.assertEqual("allow", self._decision(first))
        self.assertEqual("allow", self._decision(replay))
        self.assertEqual(
            {"activation_attempt_id": ACTIVATION_ID, "intent": VALID_INTENT},
            replay.output["hookSpecificOutput"]["updatedInput"],
        )
        self.assertEqual(attempt.expires_at, frozen.expires_at)
        self.assertIsNone(self.recall_store.get_session("session-a"))

    def test_activation_denies_an_unverified_plugin_root(self) -> None:
        """This catches arbitrary Hook environment paths becoming authority."""

        self._prompt()
        with patch.dict(
            "os.environ", {"PLUGIN_ROOT": str(self.repository)}, clear=False
        ):
            response = self._pre_tool(ACTIVATE_RECALL_TOOL)

        self.assertEqual("deny", self._decision(response))
        self.assertIsNone(self.recall_store.get_session("session-a"))

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
            plugin_root=str(self.plugin_root),
        )
        self.assertEqual("pending", gate.state)

        unselected = self._prompt(session_id="session-other", turn_id="turn-other")
        self.assertEqual({}, unselected.output)
        self.assertIsNone(self.recall_store.get_session("session-other"))

    def test_activating_prompt_creates_application_gate_and_trusted_binding(self) -> None:
        """This catches delivered context failing to bind its next-message apply."""

        preflight, delivery, items = self._deliver_handoff()

        prompt = self._prompt(turn_id="turn-b")

        output = prompt.output["hookSpecificOutput"]
        self.assertIn("apply_zdecision_recall_delivery", output["additionalContext"])
        self.assertLess(len(output["additionalContext"].encode("utf-8")), 1000)
        gate = self.recall_store.get_turn_gate("session-a", "turn-b")
        self.assertEqual("pending", gate.state)
        self.assertEqual(preflight.generation, gate.active_generation)

        bound = self._pre_tool(
            APPLY_RECALL_DELIVERY_TOOL,
            turn_id="turn-b",
            tool_input={
                "turn_gate_id": "model-gate",
                "delivery_id": "delivery_" + "e" * 32,
                "items": items,
            },
        )

        self.assertEqual("allow", self._decision(bound))
        self.assertEqual(
            {
                "turn_gate_id": GATE_ID,
                "delivery_id": delivery.delivery_id,
                "items": items,
            },
            bound.output["hookSpecificOutput"]["updatedInput"],
        )
        self.assert_private_values_absent(bound)

        extra = self._pre_tool(
            APPLY_RECALL_DELIVERY_TOOL,
            turn_id="turn-b",
            tool_input={"items": items, "untrusted": "model-value"},
        )
        self.assertEqual("deny", self._decision(extra))
        self.assertEqual(
            "pending",
            self.recall_store.get_turn_gate("session-a", "turn-b").state,
        )
        self.assert_private_values_absent(extra)

    def test_application_binding_accepts_items_only_and_injects_trusted_coordinates(
        self,
    ) -> None:
        """This catches the public items-only schema being denied by the Hook."""

        _, delivery, items = self._deliver_handoff()
        self._prompt(turn_id="turn-b")

        bound = self._pre_tool(
            APPLY_RECALL_DELIVERY_TOOL,
            turn_id="turn-b",
            tool_input={"items": items},
        )

        self.assertEqual("allow", self._decision(bound))
        self.assertEqual(
            {
                "turn_gate_id": GATE_ID,
                "delivery_id": delivery.delivery_id,
                "items": items,
            },
            bound.output["hookSpecificOutput"]["updatedInput"],
        )
        self.assert_private_values_absent(bound)

    def test_activating_guard_denies_until_exact_application_then_allows(self) -> None:
        """This catches consent or delivery acknowledgement releasing mutation early."""

        _, delivery, items = self._deliver_handoff()
        self._prompt(turn_id="turn-b")
        guarded = ("Bash", "apply_patch", "Edit", "Write", "Agent", "mcp__other__mutate")
        for tool_name in guarded:
            with self.subTest(tool=tool_name, phase="before"):
                response = self._pre_tool(tool_name, turn_id="turn-b")
                self.assertEqual("deny", self._decision(response))
                self.assertTrue(
                    response.output["hookSpecificOutput"]["permissionDecisionReason"]
                )
        for tool_name in (
            "mcp__zdecision_local__decide_zdecision_recall",
            "mcp__zdecision_local__get_zdecision_recall_handoff",
            "mcp__zdecision_local__ack_zdecision_recall_delivery",
        ):
            with self.subTest(tool=tool_name, phase="app-only"):
                self.assertEqual({}, self._pre_tool(tool_name, turn_id="turn-b").output)

        self.recall_store.commit_delivery_application(
            session_id="session-a",
            turn_id="turn-b",
            gate_id=GATE_ID,
            delivery_id=delivery.delivery_id,
            submission=RecallApplicationSubmission.from_dict(
                {"delivery_id": delivery.delivery_id, "items": items}
            ),
            now=NOW,
        )

        for tool_name in guarded:
            with self.subTest(tool=tool_name, phase="after"):
                self.assertEqual({}, self._pre_tool(tool_name, turn_id="turn-b").output)

    def test_all_not_applicable_releases_turn_with_empty_active_set(self) -> None:
        """This catches an empty applicable set being treated as an uncommitted gate."""

        _, delivery, items = self._deliver_handoff()
        self._prompt(turn_id="turn-b")
        all_not_applicable = [
            {**item, "disposition": "not_applicable"} for item in items
        ]
        self.recall_store.commit_delivery_application(
            session_id="session-a",
            turn_id="turn-b",
            gate_id=GATE_ID,
            delivery_id=delivery.delivery_id,
            submission=RecallApplicationSubmission.from_dict(
                {"delivery_id": delivery.delivery_id, "items": all_not_applicable}
            ),
            now=NOW,
        )

        self.assertEqual((), self.recall_store.list_active_items("session-a"))
        self.assertEqual({}, self._pre_tool("Bash", turn_id="turn-b").output)

    def test_conflicting_or_uncertain_application_keeps_mutation_denied(self) -> None:
        """This catches a committed blocked classification releasing affected work."""

        _, delivery, items = self._deliver_handoff()
        self._prompt(turn_id="turn-b")
        blocked_items = [
            {**items[0], "disposition": "conflicting"},
            {**items[1], "disposition": "uncertain"},
        ]
        committed = self.recall_store.commit_delivery_application(
            session_id="session-a",
            turn_id="turn-b",
            gate_id=GATE_ID,
            delivery_id=delivery.delivery_id,
            submission=RecallApplicationSubmission.from_dict(
                {"delivery_id": delivery.delivery_id, "items": blocked_items}
            ),
            now=NOW,
        )

        self.assertEqual("blocked", committed.state)
        self.assertEqual("blocked", self.recall_store.get_session("session-a").state)
        for tool_name in ("Bash", "apply_patch", "mcp__other__mutate"):
            self.assertEqual(
                "deny", self._decision(self._pre_tool(tool_name, turn_id="turn-b"))
            )

    def test_unknown_delivery_binds_but_unacknowledged_and_stale_do_not(self) -> None:
        """This catches recovery expanding beyond the one approved unknown state."""

        _, _, items = self._deliver_handoff(unknown=True)
        self._prompt(turn_id="turn-b")
        bound = self._pre_tool(
            APPLY_RECALL_DELIVERY_TOOL,
            turn_id="turn-b",
            tool_input={
                "turn_gate_id": "model-gate",
                "delivery_id": "delivery_" + "e" * 32,
                "items": items,
            },
        )
        self.assertEqual("allow", self._decision(bound))

        with self.recall_store._connection:  # noqa: SLF001 - stale gate fixture
            self.recall_store._connection.execute(
                "UPDATE recall_turn_gates SET active_generation = 99 WHERE gate_id = ?",
                (GATE_ID,),
            )
        stale = self._pre_tool(
            APPLY_RECALL_DELIVERY_TOOL,
            turn_id="turn-b",
            tool_input={
                "turn_gate_id": "model-gate",
                "delivery_id": "delivery_" + "e" * 32,
                "items": items,
            },
        )
        self.assertEqual("deny", self._decision(stale))

    def test_unacknowledged_delivery_creates_no_application_gate(self) -> None:
        """This catches a delivery claim being treated as accepted model context."""

        self._deliver_handoff()
        with self.recall_store._connection:  # noqa: SLF001 - unacked state fixture
            self.recall_store._connection.execute(
                """
                UPDATE recall_deliveries
                SET state = 'delivery_claimed', claim_token = ?, claim_expires_at = ?
                WHERE delivery_id = ?
                """,
                (
                    "claim_" + "e" * 32,
                    (NOW + timedelta(seconds=30)).isoformat(),
                    DELIVERY_ID,
                ),
            )

        prompt = self._prompt(turn_id="turn-b")

        envelope = json.loads(prompt.output["hookSpecificOutput"]["additionalContext"])
        self.assertEqual("ZDECISION_RECALL_BLOCKED", envelope["marker"])
        self.assertIsNone(self.recall_store.get_turn_gate("session-a", "turn-b"))
        self.assertEqual(
            "deny", self._decision(self._pre_tool("Bash", turn_id="turn-b"))
        )

    def test_turn_gate_preserves_intent_and_replaces_model_coordinates(self) -> None:
        self._activate()
        self._prompt(turn_id="turn-b")

        response = self._pre_tool(TURN_GATE_TOOL, turn_id="turn-b")

        self.assertEqual("allow", self._decision(response))
        self.assertEqual(
            {
                "turn_gate_id": GATE_ID,
                "intent": {
                    "target_decision_space_ids": ["dsp_" + "1" * 32],
                    "explicit_multi_space": False,
                    "feature_goal": "Continue the current product work",
                    "domain_objects": ["RecallIntent"],
                    "repository_relative_paths": ["src/zdecision/agent"],
                    "constraints": ["Apply only relevant formal decisions"],
                    "exclusions": ["Candidate generation"],
                },
            },
            response.output["hookSpecificOutput"]["updatedInput"],
        )
        self.assert_private_values_absent(response)

    def test_turn_gate_without_intent_is_denied_and_remains_pending(self) -> None:
        """This catches a trusted Gate ID being issued without semantic intent."""

        self._activate()
        self._prompt(turn_id="turn-b")

        response = self._pre_tool(
            TURN_GATE_TOOL,
            turn_id="turn-b",
            tool_input={"turn_gate_id": "model-gate"},
        )

        self.assertEqual("deny", self._decision(response))
        self.assertEqual(
            "pending",
            self.recall_store.get_turn_gate("session-a", "turn-b").state,
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
            {
                "turn_gate_id": GATE_ID_C_REBASED,
                "intent": VALID_INTENT,
            },
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
        for response in (activation_response, gate_response):
            hook_output = response.output["hookSpecificOutput"]
            self.assertEqual("PreToolUse", hook_output["hookEventName"])
            self.assertNotIn("updatedInput", hook_output)
            reason = hook_output.get(
                "permissionDecisionReason"
            )
            self.assertIsInstance(reason, str)
            self.assertTrue(reason)
            self.assertIn("Do not retry", reason)
            self.assertNotIn("store unavailable", reason)
        self.assertEqual(3, len(open_store.call_args_list))
        self.assertLessEqual(
            open_store.call_args_list[0].kwargs["timeout_seconds"], 0.1
        )
        for call in open_store.call_args_list[1:]:
            self.assertGreaterEqual(call.kwargs["timeout_seconds"], 0.2)
            self.assertLessEqual(call.kwargs["timeout_seconds"], 1.0)

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
