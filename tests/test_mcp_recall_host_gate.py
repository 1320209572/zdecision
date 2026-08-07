"""Explicit Recall MCP gate and live-only host-probe tests."""

from __future__ import annotations

import gc
import io
import json
import os
import tempfile
import unittest
import warnings
from datetime import UTC, datetime
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
from zdecision.central.decision_spaces import EnabledRepository
from zdecision.recall.session import HostProbeEnvelope, RecallIntent, TurnGateResult


NOW = datetime(2026, 8, 7, 3, 0, tzinfo=UTC)
PRIVATE_SESSION = "native-session-private"
PRIVATE_TURN = "native-turn-private"
ACTIVATION_BINDING = "activation-binding"
TURN_GATE = "turn-gate"
REPOSITORY_ID = "repo_" + "2" * 32


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
        )

    def tools(
        self,
        provider=None,
        *,
        live_acceptance: bool = True,
    ) -> RecallMcpTools:
        return RecallMcpTools(
            host_store=self.store,
            provider=provider or StaticProvider(_probe()),
            cwd=self.cwd,
            live_acceptance=live_acceptance,
        )

    def seed_pending_turn(self) -> None:
        self.bind_activation()
        self.store.begin_turn_gate(
            session_id=PRIVATE_SESSION,
            turn_id=PRIVATE_TURN,
            context_epoch=0,
            intent_epoch=0,
            active_generation=None,
            gate_id=TURN_GATE,
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
            provider, live_acceptance=False
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

    async def test_mcp_composition_registers_exact_recall_tools_without_app_ui(self) -> None:
        """This catches missing, open-world, or UI-bound Recall tool registration."""

        local = mcp_server.LocalMcpTools(database=self.database, cwd=self.cwd)
        server = mcp_server.create_mcp_server(local, self.tools())
        resources = await server.list_resources()
        tools = {tool.name: tool for tool in await server.list_tools()}

        self.assertEqual(7, len(tools))
        self.assertEqual(
            {"activate_zdecision_recall", "gate_zdecision_turn"},
            set(tools) - {
                "zdecision_status",
                "show_zdecision_update",
                "start_zdecision_candidate_refresh",
                "get_zdecision_candidate_refresh",
                "open_zdecision_dashboard",
            },
        )
        self.assertEqual(1, len(resources))
        for name in ("activate_zdecision_recall", "gate_zdecision_turn"):
            tool = tools[name]
            self.assertTrue(tool.annotations.idempotentHint)
            self.assertFalse(tool.annotations.openWorldHint)
            self.assertFalse(tool.annotations.destructiveHint)
            self.assertNotIn("ui", tool.meta or {})
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
