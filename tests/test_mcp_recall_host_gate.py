"""Explicit Recall MCP gate and live-only host-probe tests."""

from __future__ import annotations

import gc
import io
import json
import os
import sqlite3
import tempfile
import unittest
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from zdecision.agent import cli, mcp_server
from zdecision.agent.db import AgentDatabase
from zdecision.agent.recall_host_state import RecallHostStore
from zdecision.agent.recall_mcp import (
    LiveHostProbeProvider,
    ReadinessRecallGateProvider,
    RecallMcpTools,
    host_probe_path,
    prepare_host_probe,
)
from zdecision.agent.events import RepositorySnapshot
from zdecision.app_server.models import (
    ActiveTurnEvidence,
    SelectedSkill,
    ThreadIdentity,
    TurnItemEvidence,
)
from zdecision.central.decision_spaces import EnabledRepository
from zdecision.recall.session import HostProbeEnvelope, RecallIntent, TurnGateResult


NOW = datetime(2026, 8, 7, 3, 0, tzinfo=UTC)
PRIVATE_SESSION = "native-session-private"
PRIVATE_TURN = "native-turn-private"
ACTIVATION_BINDING = "activation-binding"
TURN_GATE = "turn-gate"
REPOSITORY_ID = "repo_" + "2" * 32
_DEFAULT_EVIDENCE_FACTORY = object()


def _intent(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "target_decision_space_ids": ["dsp_" + "1" * 32],
        "explicit_multi_space": False,
        "feature_goal": "Add a bounded host gate",
        "domain_objects": ["RecallIntent"],
        "repository_relative_paths": ["src/zdecision/agent"],
        "constraints": ["Do not expose native coordinates"],
        "exclusions": ["Candidate generation"],
    }
    value.update(overrides)
    return value


def _probe(name: str = "probe-fixture") -> HostProbeEnvelope:
    return HostProbeEnvelope(
        probe_id=name,
        marker="host_gate_fixture_not_formal",
        instruction="Use only this bounded host-gate fixture.",
    )


class StaticProvider:
    def __init__(self, probe: HostProbeEnvelope | None = None) -> None:
        self.probe = probe
        self.activation_calls = 0
        self.gate_calls = 0

    def activate(self, intent: RecallIntent) -> TurnGateResult:
        self.activation_calls += 1
        return TurnGateResult(
            disposition="retrieve" if self.probe is not None else "blocked",
            intent_digest=intent.digest,
            context_epoch=0,
            intent_epoch=1,
            probe=self.probe,
        )

    def gate(self, previous, intent: RecallIntent) -> TurnGateResult:
        self.gate_calls += 1
        return TurnGateResult(
            disposition="retrieve" if self.probe is not None else "blocked",
            intent_digest=intent.digest,
            context_epoch=previous.context_epoch,
            intent_epoch=previous.intent_epoch + 1,
            probe=self.probe,
        )


class StaticEvidenceGateway:
    def __init__(self, evidence: ActiveTurnEvidence | BaseException) -> None:
        self.evidence = evidence
        self.requests: list[tuple[str, str]] = []
        self.closed = False

    def read_active_turn_evidence(
        self, thread_id: str, turn_id: str
    ) -> ActiveTurnEvidence:
        self.requests.append((thread_id, turn_id))
        if isinstance(self.evidence, BaseException):
            raise self.evidence
        return self.evidence

    def close(self) -> None:
        self.closed = True


class _Stdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None


class RecallMcpToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.cwd = str(self.root / "enabled-repository")
        Path(self.cwd).mkdir()
        self.recall_skill_path = (
            self.root
            / "installed/zdecision/skills/zdecision/SKILL.md"
        )
        self.recall_skill_path.parent.mkdir(parents=True)
        self.recall_skill_path.write_text("---\nname: zdecision\n---\n", "utf-8")
        self.installed_plugin_root = self.recall_skill_path.parents[2]
        (self.installed_plugin_root / ".codex-plugin").mkdir()
        (self.installed_plugin_root / ".codex-plugin/plugin.json").write_text(
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
        (self.installed_plugin_root / ".mcp.json").write_text(
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
        self.database_path = self.root / "agent" / "zdecision.sqlite3"
        self.database = AgentDatabase.open(self.database_path)
        self.addCleanup(self.database.close)
        self.store = RecallHostStore.open(self.database_path)
        self.addCleanup(self.store.close)

    def bind_activation(self) -> None:
        self.store.bind_activation(
            session_id=PRIVATE_SESSION,
            turn_id=PRIVATE_TURN,
            cwd=self.cwd,
            binding_id=ACTIVATION_BINDING,
            now=NOW,
            plugin_root=str(self.installed_plugin_root),
        )

    def active_evidence(
        self,
        *,
        thread_id: str = PRIVATE_SESSION,
        turn_id: str = PRIVATE_TURN,
        cwd: str | None = None,
        session_tree_id: str | None = None,
        forked_from_id: str | None = None,
        selected_path: Path | None = None,
        ordered_items: tuple[TurnItemEvidence, ...] | None = None,
        activation_operation_id: str = ACTIVATION_BINDING,
        gate_operation_id: str = TURN_GATE,
    ) -> ActiveTurnEvidence:
        return ActiveTurnEvidence(
            thread=ThreadIdentity(
                thread_id=thread_id,
                session_tree_id=session_tree_id or thread_id,
                forked_from_id=forked_from_id,
                cwd=cwd or self.cwd,
                ephemeral=False,
            ),
            turn_id=turn_id,
            selected_skills=(
                SelectedSkill(
                    selection_type="skill",
                    name="zdecision",
                    path=str((selected_path or self.recall_skill_path).resolve()),
                ),
            ),
            ordered_items=(
                (
                    TurnItemEvidence("hookPrompt", "hook-current"),
                    TurnItemEvidence(
                        "mcpToolCall",
                        "activation-current",
                        tool_name="activate_zdecision_recall",
                        operation_id=activation_operation_id,
                    ),
                    TurnItemEvidence(
                        "mcpToolCall",
                        "gate-current",
                        tool_name="gate_zdecision_turn",
                        operation_id=gate_operation_id,
                    ),
                )
                if ordered_items is None
                else ordered_items
            ),
        )

    def tools(
        self,
        provider=None,
        *,
        live_acceptance: bool = True,
        evidence_gateway_factory: object = _DEFAULT_EVIDENCE_FACTORY,
    ) -> RecallMcpTools:
        if evidence_gateway_factory is _DEFAULT_EVIDENCE_FACTORY:
            evidence_gateway_factory = lambda: StaticEvidenceGateway(
                self.active_evidence()
            )
        return RecallMcpTools(
            host_store=self.store,
            provider=provider or StaticProvider(_probe()),
            cwd=self.cwd,
            live_acceptance=live_acceptance,
            evidence_gateway_factory=evidence_gateway_factory,
            recall_skill_path=self.recall_skill_path,
            clock=lambda: NOW,
        )

    def confirmation_attempt(self) -> str:
        attempt = self.store.create_activation_attempt(
            session_id=PRIVATE_SESSION,
            turn_id=PRIVATE_TURN,
            cwd=self.cwd,
            repository_id=REPOSITORY_ID,
            repository_display_name="repository",
            attempt_id="activation_" + "3" * 32,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=str(self.installed_plugin_root),
        )
        return attempt.attempt_id

    def test_confirmation_is_bounded_idempotent_and_never_calls_provider(self) -> None:
        """This catches a confirmation click invoking Recall retrieval or leaking host data."""

        attempt_id = self.confirmation_attempt()
        provider = StaticProvider(_probe())
        tools = self.tools(provider)

        rendered = tools.show_recall_confirmation(
            activation_attempt_id=attempt_id, ui_digest="a" * 64
        )
        declined = tools.decide_recall_confirmation(
            activation_attempt_id=attempt_id,
            action="decline",
            current_ui_digest="a" * 64,
        )
        replay = tools.decide_recall_confirmation(
            activation_attempt_id=attempt_id,
            action="decline",
            current_ui_digest="a" * 64,
        )

        self.assertEqual("pending_confirmation", rendered["state"])
        self.assertEqual("declined", declined["state"])
        self.assertEqual(declined, replay)
        self.assertEqual(0, provider.activation_calls)
        self.assertEqual(0, provider.gate_calls)
        self.assertIsNone(self.store.get_session(PRIVATE_SESSION))
        self.assert_private_absent(rendered)

    def test_confirmation_enable_creates_empty_consent_without_provider(self) -> None:
        """This catches an enable click deriving an intent or starting retrieval."""

        attempt_id = self.confirmation_attempt()
        provider = StaticProvider(_probe())
        tools = self.tools(provider)
        tools.show_recall_confirmation(
            activation_attempt_id=attempt_id, ui_digest="a" * 64
        )

        enabled = tools.decide_recall_confirmation(
            activation_attempt_id=attempt_id,
            action="enable",
            current_ui_digest="a" * 64,
        )
        session = self.store.get_session(PRIVATE_SESSION)

        self.assertEqual("committed", enabled["state"])
        self.assertEqual("active", session.state)
        self.assertEqual(0, session.intent_epoch)
        self.assertIsNone(session.active_intent_digest)
        self.assertIsNone(session.active_set_digest)
        self.assertEqual(0, provider.activation_calls)
        self.assertEqual(0, provider.gate_calls)

    def test_confirmation_rejects_wrong_card_and_conflicting_choice(self) -> None:
        """This catches a stale card or second click authorizing a new consent."""

        attempt_id = self.confirmation_attempt()
        provider = StaticProvider(_probe())
        tools = self.tools(provider)
        tools.show_recall_confirmation(
            activation_attempt_id=attempt_id, ui_digest="a" * 64
        )

        wrong_card = tools.decide_recall_confirmation(
            activation_attempt_id=attempt_id,
            action="enable",
            current_ui_digest="b" * 64,
        )
        declined = tools.decide_recall_confirmation(
            activation_attempt_id=attempt_id,
            action="decline",
            current_ui_digest="a" * 64,
        )
        conflicting = tools.decide_recall_confirmation(
            activation_attempt_id=attempt_id,
            action="enable",
            current_ui_digest="a" * 64,
        )

        self.assertEqual({"state": "blocked", "code": "invalid_confirmation"}, wrong_card)
        self.assertEqual("declined", declined["state"])
        self.assertEqual({"state": "blocked", "code": "invalid_confirmation"}, conflicting)
        self.assertIsNone(self.store.get_session(PRIVATE_SESSION))
        self.assertEqual(0, provider.activation_calls)
        self.assertEqual(0, provider.gate_calls)

    def test_confirmation_failure_paths_never_authorize(self) -> None:
        """This catches expiry, CWD/bundle mismatch, or SQLite failure granting consent."""

        expired = self.store.create_activation_attempt(
            session_id=PRIVATE_SESSION,
            turn_id=PRIVATE_TURN,
            cwd=self.cwd,
            repository_id=REPOSITORY_ID,
            repository_display_name="repository",
            attempt_id="activation_" + "c" * 32,
            now=NOW - timedelta(minutes=16),
            expires_at=NOW - timedelta(minutes=1),
            plugin_root=str(self.installed_plugin_root),
        )
        self.store.attach_activation_card(expired.attempt_id, ui_digest="a" * 64)
        provider = StaticProvider(_probe())
        tools = self.tools(provider)
        wrong_cwd_tools = RecallMcpTools(
            host_store=self.store,
            provider=provider,
            cwd="/tmp/other-repository",
            clock=lambda: NOW,
        )

        expired_result = tools.decide_recall_confirmation(
            activation_attempt_id=expired.attempt_id,
            action="enable",
            current_ui_digest="a" * 64,
        )
        wrong_cwd = wrong_cwd_tools.show_recall_confirmation(
            activation_attempt_id=expired.attempt_id, ui_digest="a" * 64
        )
        bundle_attempt = self.store.create_activation_attempt(
            session_id="bundle-session",
            turn_id="bundle-turn",
            cwd=self.cwd,
            repository_id=REPOSITORY_ID,
            repository_display_name="repository",
            attempt_id="activation_" + "d" * 32,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=str(self.installed_plugin_root),
        )
        self.recall_skill_path.write_text("tampered", "utf-8")
        wrong_bundle = tools.show_recall_confirmation(
            activation_attempt_id=bundle_attempt.attempt_id, ui_digest="a" * 64
        )
        with patch.object(
            self.store, "get_activation_attempt", side_effect=sqlite3.OperationalError()
        ):
            unavailable = tools.show_recall_confirmation(
                activation_attempt_id=expired.attempt_id, ui_digest="a" * 64
            )

        blocked = {"state": "blocked", "code": "invalid_confirmation"}
        self.assertEqual(blocked, expired_result)
        self.assertEqual(blocked, wrong_cwd)
        self.assertEqual(blocked, wrong_bundle)
        self.assertEqual(blocked, unavailable)
        self.assertEqual("failed", self.store.get_activation_attempt(expired.attempt_id).state)
        self.assertIsNone(self.store.get_session(PRIVATE_SESSION))
        self.assertIsNone(self.store.get_session("bundle-session"))
        self.assertEqual(0, provider.activation_calls)
        self.assertEqual(0, provider.gate_calls)

    def seed_pending_turn(self) -> None:
        self.bind_activation()
        self.store.begin_turn_gate(
            session_id=PRIVATE_SESSION,
            turn_id=PRIVATE_TURN,
            context_epoch=0,
            intent_epoch=0,
            active_generation=None,
            gate_id=TURN_GATE,
            plugin_root=str(self.installed_plugin_root),
        )

    def assert_private_absent(self, value: object) -> None:
        encoded = json.dumps(value, sort_keys=True).lower()
        for forbidden in (
            PRIVATE_SESSION,
            PRIVATE_TURN,
            self.cwd.lower(),
            "session_id",
            "turn_id",
            "cwd",
            "product",
            "generation",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_activation_schema_is_exact_and_never_exposes_native_coordinates(self) -> None:
        """This catches successful activation leaking host-owned identifiers."""

        self.bind_activation()
        result = self.tools().activate_zdecision_recall(
            activation_binding_id=ACTIVATION_BINDING,
            intent=_intent(),
        )

        self.assertEqual(
            {
                "state": "active",
                "receipt": "host_probe_applied",
                "probe": {
                    "probe_id": "probe-fixture",
                    "marker": "host_gate_fixture_not_formal",
                    "instruction": "Use only this bounded host-gate fixture.",
                },
            },
            result,
        )
        self.assert_private_absent(result)
        session = self.store.get_session(PRIVATE_SESSION)
        self.assertIsNotNone(session.active_set_digest)
        self.assertEqual(PRIVATE_TURN, session.last_gate_turn_id)

    def test_production_activation_remains_native_selection_unproven(self) -> None:
        """This catches a model tool call being treated as native selection proof."""

        self.bind_activation()
        provider = StaticProvider(_probe())

        result = self.tools(
            provider,
            live_acceptance=False,
            evidence_gateway_factory=None,
        ).activate_zdecision_recall(
            activation_binding_id=ACTIVATION_BINDING,
            intent=_intent(),
        )

        self.assertEqual(
            {"state": "blocked", "code": "native_selection_unproven"},
            result,
        )
        self.assertEqual(0, provider.activation_calls)
        self.assertIsNone(self.store.get_session(PRIVATE_SESSION).active_set_digest)

    def test_activation_reads_exact_hook_thread_and_turn_before_provider(self) -> None:
        """This catches session-tree or model text guesses authorizing activation."""

        self.bind_activation()
        provider = StaticProvider(_probe())
        gateway = StaticEvidenceGateway(self.active_evidence())
        tools = RecallMcpTools(
            host_store=self.store,
            provider=provider,
            cwd=self.cwd,
            live_acceptance=True,
            evidence_gateway_factory=lambda: gateway,
            recall_skill_path=self.recall_skill_path,
        )

        result = tools.activate_zdecision_recall(
            activation_binding_id=ACTIVATION_BINDING,
            intent=_intent(),
        )

        self.assertEqual("active", result["state"])
        self.assertEqual([(PRIVATE_SESSION, PRIVATE_TURN)], gateway.requests)
        self.assertTrue(gateway.closed)
        self.assertEqual(1, provider.activation_calls)

    def test_activation_enforces_first_answer_barrier_before_provider(self) -> None:
        """This catches missing, late, stale, or duplicate current activation items."""

        self.bind_activation()
        cases = (
            (),
            (
                TurnItemEvidence(
                    "mcpToolCall",
                    "activation-current",
                    tool_name="activate_zdecision_recall",
                    operation_id=ACTIVATION_BINDING,
                ),
                TurnItemEvidence("hookPrompt", "hook-late"),
            ),
            (
                TurnItemEvidence("hookPrompt", "hook-current"),
                TurnItemEvidence("agentMessage", "answer-before-gate"),
                TurnItemEvidence(
                    "mcpToolCall",
                    "activation-current",
                    tool_name="activate_zdecision_recall",
                    operation_id=ACTIVATION_BINDING,
                ),
            ),
            (
                TurnItemEvidence("hookPrompt", "hook-current"),
                TurnItemEvidence(
                    "mcpToolCall",
                    "activation-old",
                    tool_name="activate_zdecision_recall",
                    operation_id="old-activation-binding",
                ),
            ),
            (
                TurnItemEvidence("hookPrompt", "hook-current"),
                TurnItemEvidence(
                    "mcpToolCall",
                    "activation-one",
                    tool_name="activate_zdecision_recall",
                    operation_id=ACTIVATION_BINDING,
                ),
                TurnItemEvidence(
                    "mcpToolCall",
                    "activation-two",
                    tool_name="activate_zdecision_recall",
                    operation_id=ACTIVATION_BINDING,
                ),
            ),
        )

        for ordered_items in cases:
            with self.subTest(ordered_items=ordered_items):
                provider = StaticProvider(_probe())
                gateway = StaticEvidenceGateway(
                    self.active_evidence(ordered_items=ordered_items)
                )
                result = self.tools(
                    provider,
                    evidence_gateway_factory=lambda gateway=gateway: gateway,
                ).activate_zdecision_recall(
                    activation_binding_id=ACTIVATION_BINDING,
                    intent=_intent(),
                )
                self.assertEqual(
                    {"state": "blocked", "code": "native_selection_unproven"},
                    result,
                )
                self.assertEqual(0, provider.activation_calls)

    def test_context_compaction_is_not_substantive_barrier_output(self) -> None:
        """This catches compaction alone being mistaken for an answer or mutation."""

        self.bind_activation()
        provider = StaticProvider(_probe())
        evidence = self.active_evidence(
            ordered_items=(
                TurnItemEvidence("contextCompaction", "compact-before"),
                TurnItemEvidence("hookPrompt", "hook-current"),
                TurnItemEvidence(
                    "mcpToolCall",
                    "activation-current",
                    tool_name="activate_zdecision_recall",
                    operation_id=ACTIVATION_BINDING,
                ),
            )
        )

        result = self.tools(
            provider,
            evidence_gateway_factory=lambda: StaticEvidenceGateway(evidence),
        ).activate_zdecision_recall(
            activation_binding_id=ACTIVATION_BINDING,
            intent=_intent(),
        )

        self.assertEqual("active", result["state"])
        self.assertEqual(1, provider.activation_calls)

    def test_installed_skill_path_accepts_only_the_runtime_bound_copy(self) -> None:
        """This catches the source checkout masquerading as the installed Skill."""

        self.bind_activation()
        source_skill = (
            Path(mcp_server.__file__).resolve().parents[3]
            / "plugins/zdecision/skills/zdecision/SKILL.md"
        )
        self.assertNotEqual(source_skill.resolve(), self.recall_skill_path.resolve())
        provider = StaticProvider(_probe())
        bound_tools = RecallMcpTools(
            host_store=self.store,
            provider=provider,
            cwd=self.cwd,
            live_acceptance=True,
            evidence_gateway_factory=lambda: StaticEvidenceGateway(
                self.active_evidence(selected_path=source_skill)
            ),
            recall_skill_path=None,
        )
        rejected = bound_tools.activate_zdecision_recall(
            activation_binding_id=ACTIVATION_BINDING,
            intent=_intent(),
        )
        bound_tools.evidence_gateway_factory = lambda: StaticEvidenceGateway(
            self.active_evidence(selected_path=self.recall_skill_path)
        )
        accepted = bound_tools.activate_zdecision_recall(
            activation_binding_id=ACTIVATION_BINDING,
            intent=_intent(),
        )

        self.assertEqual(
            {"state": "blocked", "code": "native_selection_unproven"},
            rejected,
        )
        self.assertEqual("active", accepted["state"])
        self.assertEqual(1, provider.activation_calls)

    def test_tampered_bound_plugin_fails_closed_before_provider(self) -> None:
        """This catches a once-bound plugin root bypassing later file tampering."""

        self.bind_activation()
        self.recall_skill_path.write_text(
            "---\nname: zdecision\n---\nTAMPERED\n", "utf-8"
        )
        provider = StaticProvider(_probe())
        result = RecallMcpTools(
            host_store=self.store,
            provider=provider,
            cwd=self.cwd,
            evidence_gateway_factory=lambda: StaticEvidenceGateway(
                self.active_evidence()
            ),
            recall_skill_path=None,
        ).activate_zdecision_recall(
            activation_binding_id=ACTIVATION_BINDING,
            intent=_intent(),
        )

        self.assertEqual(
            {"state": "blocked", "code": "native_selection_unproven"}, result
        )
        self.assertEqual(0, provider.activation_calls)

    def test_unbound_plugin_root_fails_closed_before_provider(self) -> None:
        """This catches legacy or non-Hook bindings gaining installed-path authority."""

        self.store.bind_activation(
            session_id=PRIVATE_SESSION,
            turn_id=PRIVATE_TURN,
            cwd=self.cwd,
            binding_id=ACTIVATION_BINDING,
            now=NOW,
        )
        provider = StaticProvider(_probe())
        result = RecallMcpTools(
            host_store=self.store,
            provider=provider,
            cwd=self.cwd,
            evidence_gateway_factory=lambda: StaticEvidenceGateway(
                self.active_evidence()
            ),
            recall_skill_path=None,
        ).activate_zdecision_recall(
            activation_binding_id=ACTIVATION_BINDING,
            intent=_intent(),
        )

        self.assertEqual(
            {"state": "blocked", "code": "native_selection_unproven"}, result
        )
        self.assertEqual(0, provider.activation_calls)

    def test_activation_rejects_unproven_turn_path_or_cwd_evidence(self) -> None:
        """This catches Prompt text, another Skill, or nearby identity authorizing recall."""

        self.bind_activation()
        provider = StaticProvider(_probe())
        other_skill = self.root / "installed/other/SKILL.md"
        other_skill.parent.mkdir(parents=True)
        other_skill.write_text("---\nname: other\n---\n", "utf-8")
        exact = self.active_evidence()
        cases: tuple[ActiveTurnEvidence | BaseException, ...] = (
            ActiveTurnEvidence(
                thread=exact.thread,
                turn_id=exact.turn_id,
                selected_skills=(),
                ordered_items=(),
            ),
            self.active_evidence(selected_path=other_skill),
            self.active_evidence(cwd=str(self.root)),
            self.active_evidence(turn_id="different-turn"),
            self.active_evidence(thread_id="different-thread"),
            RuntimeError("controlled app-server unavailable"),
        )

        for evidence in cases:
            with self.subTest(evidence=evidence):
                gateway = StaticEvidenceGateway(evidence)
                result = self.tools(
                    provider,
                    evidence_gateway_factory=lambda gateway=gateway: gateway,
                ).activate_zdecision_recall(
                    activation_binding_id=ACTIVATION_BINDING,
                    intent=_intent(),
                )
                self.assertEqual(
                    {"state": "blocked", "code": "native_selection_unproven"},
                    result,
                )
                self.assertTrue(gateway.closed)
        self.assertEqual(0, provider.activation_calls)

    def test_fork_activation_uses_child_thread_not_session_tree_id(self) -> None:
        """This catches child session provenance being used as Hook identity."""

        root_thread = "root-thread"
        child_thread = "child-thread"
        child_turn = "child-turn"
        binding_id = "child-activation-binding"
        self.store.bind_activation(
            session_id=child_thread,
            turn_id=child_turn,
            cwd=self.cwd,
            binding_id=binding_id,
            now=NOW,
        )
        evidence = self.active_evidence(
            thread_id=child_thread,
            turn_id=child_turn,
            session_tree_id=root_thread,
            forked_from_id=root_thread,
            activation_operation_id=binding_id,
        )
        gateway = StaticEvidenceGateway(evidence)

        result = self.tools(
            evidence_gateway_factory=lambda: gateway
        ).activate_zdecision_recall(
            activation_binding_id=binding_id,
            intent=_intent(),
        )

        self.assertEqual("active", result["state"])
        self.assertEqual([(child_thread, child_turn)], gateway.requests)

    def test_missing_binding_and_invalid_or_oversized_intent_are_bounded(self) -> None:
        """This catches invalid model input reaching a provider or durable active set."""

        provider = StaticProvider(_probe())
        tools = self.tools(provider)
        self.assertEqual(
            {"state": "blocked", "code": "invalid_binding"},
            tools.activate_zdecision_recall(
                activation_binding_id="unknown-binding", intent=_intent()
            ),
        )
        self.bind_activation()
        for value in (
            _intent(session_id=PRIVATE_SESSION),
            _intent(feature_goal="x" * 2_001),
        ):
            with self.subTest(value=list(value)):
                self.assertEqual(
                    {"state": "blocked", "code": "invalid_intent"},
                    tools.activate_zdecision_recall(
                        activation_binding_id=ACTIVATION_BINDING,
                        intent=value,
                    ),
                )
        self.assertEqual(0, provider.activation_calls)

    def test_activation_replay_and_response_loss_reconcile_without_provider_rerun(self) -> None:
        """This catches response loss consuming a probe twice or changing its receipt."""

        self.bind_activation()
        provider = StaticProvider(_probe())
        first = self.tools(provider).activate_zdecision_recall(
            activation_binding_id=ACTIVATION_BINDING, intent=_intent()
        )
        replay = self.tools(provider).activate_zdecision_recall(
            activation_binding_id=ACTIVATION_BINDING, intent=_intent()
        )

        self.assertEqual(first, replay)
        self.assertEqual(1, provider.activation_calls)

    def test_receipt_operations_close_every_ephemeral_database_connection(self) -> None:
        """This catches receipt transactions leaking SQLite handles until GC."""

        self.bind_activation()
        gc.collect()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            self.tools().activate_zdecision_recall(
                activation_binding_id=ACTIVATION_BINDING,
                intent=_intent(),
            )
            gc.collect()

        resource_warnings = [
            warning
            for warning in caught
            if issubclass(warning.category, ResourceWarning)
            and "sqlite3.Connection" in str(warning.message)
        ]
        self.assertEqual([], resource_warnings)

    def test_activation_binding_cannot_be_replayed_with_changed_intent(self) -> None:
        """This catches one trusted activation authorizing a different model intent."""

        self.bind_activation()
        tools = self.tools()
        tools.activate_zdecision_recall(
            activation_binding_id=ACTIVATION_BINDING, intent=_intent()
        )

        result = tools.activate_zdecision_recall(
            activation_binding_id=ACTIVATION_BINDING,
            intent=_intent(feature_goal="A different goal"),
        )

        self.assertEqual(
            {"state": "blocked", "code": "binding_replayed"}, result
        )

    def test_gate_commits_before_response_and_replays_without_provider_rerun(self) -> None:
        """This catches returning applicable context before its exact gate is durable."""

        self.seed_pending_turn()
        provider = StaticProvider(_probe("probe-turn"))
        tools = self.tools(provider)

        first = tools.gate_zdecision_turn(turn_gate_id=TURN_GATE, intent=_intent())
        committed = self.store.require_committed_gate(PRIVATE_SESSION, PRIVATE_TURN)
        replay = self.tools(provider).gate_zdecision_turn(
            turn_gate_id=TURN_GATE, intent=_intent()
        )

        self.assertEqual("committed", committed.state)
        self.assertEqual(first, replay)
        self.assertEqual(1, provider.gate_calls)
        self.assert_private_absent(first)

    def test_gate_enforces_current_turn_barrier_before_provider(self) -> None:
        """This catches unreadable, missing, late, stale, or duplicate gate items."""

        self.seed_pending_turn()
        cases: tuple[ActiveTurnEvidence | BaseException, ...] = (
            RuntimeError("controlled app-server unavailable"),
            self.active_evidence(ordered_items=()),
            self.active_evidence(
                ordered_items=(
                    TurnItemEvidence(
                        "mcpToolCall",
                        "gate-current",
                        tool_name="gate_zdecision_turn",
                        operation_id=TURN_GATE,
                    ),
                    TurnItemEvidence("hookPrompt", "hook-late"),
                )
            ),
            self.active_evidence(
                ordered_items=(
                    TurnItemEvidence("hookPrompt", "hook-current"),
                    TurnItemEvidence("commandExecution", "command-before-gate"),
                    TurnItemEvidence(
                        "mcpToolCall",
                        "gate-current",
                        tool_name="gate_zdecision_turn",
                        operation_id=TURN_GATE,
                    ),
                )
            ),
            self.active_evidence(
                ordered_items=(
                    TurnItemEvidence("hookPrompt", "hook-current"),
                    TurnItemEvidence(
                        "mcpToolCall",
                        "gate-old",
                        tool_name="gate_zdecision_turn",
                        operation_id="old-turn-gate",
                    ),
                )
            ),
            self.active_evidence(
                ordered_items=(
                    TurnItemEvidence("hookPrompt", "hook-current"),
                    TurnItemEvidence(
                        "mcpToolCall",
                        "gate-one",
                        tool_name="gate_zdecision_turn",
                        operation_id=TURN_GATE,
                    ),
                    TurnItemEvidence(
                        "mcpToolCall",
                        "gate-two",
                        tool_name="gate_zdecision_turn",
                        operation_id=TURN_GATE,
                    ),
                )
            ),
        )

        for evidence in cases:
            with self.subTest(evidence=evidence):
                provider = StaticProvider(_probe("probe-turn"))
                result = self.tools(
                    provider,
                    evidence_gateway_factory=(
                        lambda evidence=evidence: StaticEvidenceGateway(evidence)
                    ),
                ).gate_zdecision_turn(turn_gate_id=TURN_GATE, intent=_intent())

                self.assertEqual(
                    {"state": "blocked", "code": "host_gate_unavailable"},
                    result,
                )
                self.assertEqual(0, provider.gate_calls)

    def test_gate_uses_the_exact_hook_bound_plugin_bundle(self) -> None:
        """This catches production gate calls depending on MCP cwd or environment."""

        self.seed_pending_turn()
        provider = StaticProvider(_probe("probe-turn"))
        result = RecallMcpTools(
            host_store=self.store,
            provider=provider,
            cwd=self.cwd,
            evidence_gateway_factory=lambda: StaticEvidenceGateway(
                self.active_evidence()
            ),
            recall_skill_path=None,
        ).gate_zdecision_turn(turn_gate_id=TURN_GATE, intent=_intent())

        self.assertEqual("active", result["state"])
        self.assertEqual(1, provider.gate_calls)

    def test_gate_reconciles_crash_after_commit_before_receipt_ack(self) -> None:
        """This catches a crash window rerunning the provider after durable commit."""

        self.seed_pending_turn()
        provider = StaticProvider(_probe("probe-crash"))
        first = self.tools(provider).gate_zdecision_turn(
            turn_gate_id=TURN_GATE,
            intent=_intent(),
        )
        with self.store._connection:
            self.store._connection.execute(
                """
                UPDATE recall_mcp_receipts SET state = 'prepared'
                WHERE binding_kind = 'turn' AND binding_id = ?
                """,
                (TURN_GATE,),
            )

        replay = self.tools(provider).gate_zdecision_turn(
            turn_gate_id=TURN_GATE,
            intent=_intent(),
        )

        self.assertEqual(first, replay)
        self.assertEqual(1, provider.gate_calls)
        state = self.store._connection.execute(
            """
            SELECT state FROM recall_mcp_receipts
            WHERE binding_kind = 'turn' AND binding_id = ?
            """,
            (TURN_GATE,),
        ).fetchone()["state"]
        self.assertEqual("applied", state)

    def test_corrupt_receipt_response_is_bounded_and_never_returned(self) -> None:
        """This catches private receipt corruption escaping through a replay."""

        self.seed_pending_turn()
        tools = self.tools()
        tools.gate_zdecision_turn(turn_gate_id=TURN_GATE, intent=_intent())
        with self.store._connection:
            self.store._connection.execute(
                """
                UPDATE recall_mcp_receipts SET response_json = ?
                WHERE binding_kind = 'turn' AND binding_id = ?
                """,
                (
                    json.dumps({"state": "active", "session_id": PRIVATE_SESSION}),
                    TURN_GATE,
                ),
            )

        replay = tools.gate_zdecision_turn(
            turn_gate_id=TURN_GATE,
            intent=_intent(),
        )

        self.assertEqual(
            {"state": "blocked", "code": "host_gate_unavailable"}, replay
        )
        self.assert_private_absent(replay)

    def test_gate_rejects_unknown_replayed_and_cross_turn_bindings(self) -> None:
        """This catches a prior Turn gate being reused after a newer Turn is bound."""

        self.seed_pending_turn()
        tools = self.tools()
        tools.gate_zdecision_turn(turn_gate_id=TURN_GATE, intent=_intent())
        session = self.store.get_session(PRIVATE_SESSION)
        self.store.begin_turn_gate(
            session_id=PRIVATE_SESSION,
            turn_id="native-turn-new-private",
            context_epoch=session.context_epoch,
            intent_epoch=session.intent_epoch,
            active_generation=None,
            gate_id="turn-gate-new",
        )

        self.assertEqual(
            {"state": "blocked", "code": "invalid_binding"},
            tools.gate_zdecision_turn(turn_gate_id=TURN_GATE, intent=_intent()),
        )
        self.assertEqual(
            {"state": "blocked", "code": "invalid_binding"},
            tools.gate_zdecision_turn(
                turn_gate_id="unknown-turn-gate", intent=_intent()
            ),
        )

    def test_live_gate_claim_prevents_interleaved_blocked_loser(self) -> None:
        """This catches a loser freezing blocked after the winner reads the probe."""

        self.seed_pending_turn()
        prepare_host_probe(self.database_path, self.cwd, "probe-interleaved")
        loser = self.tools(
            LiveHostProbeProvider(self.database_path, self.cwd)
        )

        class InterleavingProvider(LiveHostProbeProvider):
            loser_response: dict[str, object] | None = None

            def gate(self, previous, intent: RecallIntent) -> TurnGateResult:
                winner = super().gate(previous, intent)
                self.loser_response = loser.gate_zdecision_turn(
                    turn_gate_id=TURN_GATE,
                    intent=intent.to_dict(),
                )
                return winner

        provider = InterleavingProvider(self.database_path, self.cwd)
        winner_response = self.tools(provider).gate_zdecision_turn(
            turn_gate_id=TURN_GATE,
            intent=_intent(),
        )

        self.assertEqual("active", winner_response["state"])
        self.assertEqual(
            {"state": "blocked", "code": "host_gate_busy"},
            provider.loser_response,
        )
        self.assertEqual(
            "committed",
            self.store.get_turn_gate(PRIVATE_SESSION, PRIVATE_TURN).state,
        )
        self.assertFalse(host_probe_path(self.database_path, self.cwd).exists())

    def test_live_probe_survives_crash_after_read_and_retry_applies_it(self) -> None:
        """This catches a provider crash permanently consuming its probe file."""

        self.seed_pending_turn()
        prepare_host_probe(self.database_path, self.cwd, "probe-read-crash")

        class CrashAfterReadProvider(LiveHostProbeProvider):
            crash = True

            def gate(self, previous, intent: RecallIntent) -> TurnGateResult:
                result = super().gate(previous, intent)
                if self.crash:
                    self.crash = False
                    raise RuntimeError("bounded fixture crash")
                return result

        provider = CrashAfterReadProvider(self.database_path, self.cwd)
        tools = self.tools(provider)
        first = tools.gate_zdecision_turn(turn_gate_id=TURN_GATE, intent=_intent())
        replay = tools.gate_zdecision_turn(turn_gate_id=TURN_GATE, intent=_intent())

        self.assertEqual(
            {"state": "blocked", "code": "host_gate_unavailable"}, first
        )
        self.assertEqual("active", replay["state"])
        self.assertEqual(
            "committed",
            self.store.get_turn_gate(PRIVATE_SESSION, PRIVATE_TURN).state,
        )

    def test_live_probe_ack_loss_is_cleaned_up_by_receipt_replay(self) -> None:
        """This catches a post-commit crash leaving a reusable live probe."""

        self.seed_pending_turn()
        prepare_host_probe(self.database_path, self.cwd, "probe-ack-crash")

        class AckCrashProvider(LiveHostProbeProvider):
            acknowledgements = 0

            def acknowledge(self, probe: HostProbeEnvelope) -> None:
                self.acknowledgements += 1
                if self.acknowledgements == 1:
                    raise OSError("bounded fixture ack crash")
                super().acknowledge(probe)

        provider = AckCrashProvider(self.database_path, self.cwd)
        tools = self.tools(provider)
        first = tools.gate_zdecision_turn(turn_gate_id=TURN_GATE, intent=_intent())
        path = host_probe_path(self.database_path, self.cwd)
        self.assertTrue(path.exists())

        replay = tools.gate_zdecision_turn(turn_gate_id=TURN_GATE, intent=_intent())

        self.assertEqual(first, replay)
        self.assertEqual(2, provider.acknowledgements)
        self.assertFalse(path.exists())

    def test_blocked_provider_freezes_gate_without_replacing_active_set(self) -> None:
        """This catches readiness-only production accidentally claiming retrieval."""

        self.seed_pending_turn()
        tools = self.tools(ReadinessRecallGateProvider())

        result = tools.gate_zdecision_turn(
            turn_gate_id=TURN_GATE, intent=_intent()
        )

        self.assertEqual({"state": "blocked", "code": "host_gate_only"}, result)
        self.assertEqual(
            "blocked",
            self.store.get_turn_gate(PRIVATE_SESSION, PRIVATE_TURN).state,
        )
        self.assertIsNone(self.store.get_session(PRIVATE_SESSION).active_set_digest)

    async def test_mcp_composition_registers_confirmation_and_turn_gate_only(self) -> None:
        """This catches a model-visible decision action or legacy activation registration."""

        local = mcp_server.LocalMcpTools(database=self.database, cwd=self.cwd)
        server = mcp_server.create_mcp_server(local, self.tools())
        resources = await server.list_resources()
        tools = {tool.name: tool for tool in await server.list_tools()}

        self.assertEqual(8, len(tools))
        self.assertEqual(
            {
                "show_zdecision_recall_confirmation",
                "decide_zdecision_recall",
                "gate_zdecision_turn",
            },
            set(tools) - {
                "zdecision_status",
                "show_zdecision_update",
                "start_zdecision_candidate_refresh",
                "get_zdecision_candidate_refresh",
                "open_zdecision_dashboard",
            },
        )
        self.assertEqual(2, len(resources))
        self.assertNotIn("activate_zdecision_recall", tools)
        self.assertEqual(
            ["model", "app"],
            tools["show_zdecision_recall_confirmation"].meta["ui"]["visibility"],
        )
        self.assertEqual(
            ["app"], tools["decide_zdecision_recall"].meta["ui"]["visibility"]
        )
        for name in (
            "show_zdecision_recall_confirmation",
            "decide_zdecision_recall",
            "gate_zdecision_turn",
        ):
            tool = tools[name]
            self.assertTrue(tool.annotations.idempotentHint)
            self.assertFalse(tool.annotations.openWorldHint)
            self.assertFalse(tool.annotations.destructiveHint)
            self.assertFalse(tool.inputSchema.get("additionalProperties", True))
            forbidden = {"session_id", "turn_id", "cwd", "product", "generation"}
            self.assertTrue(forbidden.isdisjoint(tool.inputSchema["properties"]))

    def test_production_run_mcp_always_injects_registered_recall_tools(self) -> None:
        """This catches production silently falling back to Candidate-only MCP."""

        class Server:
            transport = None

            def run(self, *, transport: str) -> None:
                self.transport = transport

        captured: list[object] = []
        server = Server()

        def create(local_tools, recall_tools=None):
            captured.extend((local_tools, recall_tools))
            return server

        with patch.object(mcp_server, "create_mcp_server", side_effect=create):
            mcp_server.run_mcp(
                database_path=self.database_path,
                config_locator_path=self.root / "missing-config.json",
                cwd=self.cwd,
            )

        self.assertIsInstance(captured[0], mcp_server.LocalMcpTools)
        self.assertIsInstance(captured[1], RecallMcpTools)
        self.assertIsNotNone(captured[1].evidence_gateway_factory)
        self.assertIsNone(captured[1].recall_skill_path)
        self.assertEqual("stdio", server.transport)


class RecallHostProbeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.cwd = self.root / "enabled-repository"
        self.cwd.mkdir()
        self.environ = {
            "ZDECISION_LIVE_ACCEPTANCE": "1",
            "ZDECISION_STATE_DIR": str(self.root / "state"),
        }
        self.state_path = cli.database_path(self.environ)
        database = AgentDatabase.open(self.state_path)
        database.put_enabled_repository(EnabledRepository(REPOSITORY_ID, True))
        database.close()
        self.snapshot = RepositorySnapshot(
            repository_id=REPOSITORY_ID,
            worktree_root=str(self.cwd),
            branch="main",
            head_commit="a" * 40,
        )

    def run_cli(self, *arguments: str) -> tuple[int, dict[str, object]]:
        stdout = _Stdout()
        with (
            patch.dict(os.environ, self.environ, clear=True),
            patch("sys.stdout", stdout),
            patch(
                "zdecision.agent.repository.RepositoryResolver.resolve",
                return_value=self.snapshot,
            ),
        ):
            code = cli.main(list(arguments))
        return code, json.loads(stdout.buffer.getvalue())

    def test_command_exists_only_for_explicit_live_acceptance(self) -> None:
        """This catches the fixture command becoming a normal production surface."""

        with patch.dict(os.environ, {}, clear=True):
            with (
                patch("sys.stderr", io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                cli.build_parser().parse_args(["recall-host-gate", "clear"])
        with patch.dict(os.environ, self.environ, clear=True):
            parsed = cli.build_parser().parse_args(["recall-host-gate", "clear"])
        self.assertEqual("recall-host-gate", parsed.command)
        self.assertEqual("clear", parsed.recall_host_gate_action)

    def test_prepare_writes_only_bounded_probe_and_clear_removes_it(self) -> None:
        """This catches the live fixture writing Candidate, Review, or Registry state."""

        code, prepared = self.run_cli(
            "recall-host-gate", "prepare", "--cwd", str(self.cwd)
        )
        path = host_probe_path(self.state_path, str(self.cwd.resolve()))
        document = json.loads(path.read_text("utf-8"))

        self.assertEqual(0, code)
        self.assertEqual({"prepared": True}, prepared)
        self.assertEqual(
            {"probe_id", "marker", "instruction"}, set(document)
        )
        self.assertEqual("host_gate_fixture_not_formal", document["marker"])
        self.assertFalse(any(self.root.rglob("*candidate*")))
        self.assertFalse(any(self.root.rglob("*review*")))
        self.assertFalse(any(self.root.rglob("decision-registry")))

        code, cleared = self.run_cli("recall-host-gate", "clear")
        self.assertEqual(0, code)
        self.assertEqual({"cleared": True}, cleared)
        self.assertFalse(path.exists())

    def test_live_provider_read_is_recoverable_until_tool_ack(self) -> None:
        """This catches provider read deleting a fixture before durable ownership."""

        self.run_cli("recall-host-gate", "prepare", "--cwd", str(self.cwd))
        provider = LiveHostProbeProvider(self.state_path, str(self.cwd.resolve()))
        intent = RecallIntent.from_dict(_intent())

        first = provider.activate(intent)
        second = provider.activate(intent)

        self.assertEqual("host_gate_fixture_not_formal", first.probe.marker)
        self.assertEqual(first, second)
        self.assertTrue(
            host_probe_path(self.state_path, str(self.cwd.resolve())).exists()
        )


if __name__ == "__main__":
    unittest.main()
