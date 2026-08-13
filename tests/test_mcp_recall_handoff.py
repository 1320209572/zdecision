"""Model-visible Recall delivery application MCP contract tests."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from zdecision.agent import cli, mcp_server
from zdecision.agent.db import AgentDatabase
from zdecision.agent.mcp_server import LocalMcpTools
from zdecision.agent.recall_handoff import RecallHandoffService
from zdecision.agent.recall_host_state import RecallHostStore
from zdecision.agent.recall_plugin_identity import RecallPluginIdentity
from zdecision.agent.recall_mcp import RecallMcpTools
from zdecision.app_server.gateway import AppServerGateway
from zdecision.recall.handoff import (
    RecallShortlist,
    RecalledDecision,
    build_handoff_context,
)
from zdecision.recall.provider import UnavailableRecallProvider

from tests.test_recall_handoff_contracts import (
    formal_decision,
    ready_preflight,
    valid_intent,
)


NOW = datetime(2026, 8, 11, 4, 0, tzinfo=UTC)
ATTEMPT_ID = "activation_" + "a" * 32
DELIVERY_ID = "delivery_" + "d" * 32
GATE_ID = "gate-application"


class RecallHandoffMcpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        self.cwd = str(root / "repository")
        Path(self.cwd).mkdir()
        database_path = root / "state.sqlite3"
        self.database = AgentDatabase.open(database_path)
        self.addCleanup(self.database.close)
        self.store = RecallHostStore.open(database_path)
        self.addCleanup(self.store.close)
        local = LocalMcpTools(database=self.database, cwd=self.cwd)
        self.recall = RecallMcpTools(
            host_store=self.store,
            cwd=self.cwd,
            clock=lambda: NOW,
        )
        self.server = mcp_server.create_mcp_server(local, self.recall)

    def _prepare_application(self, *, unknown: bool = False):
        intent = valid_intent()
        preflight = replace(
            ready_preflight(intent=intent),
            expires_at="2026-08-11T05:00:00Z",
        )
        first = RecalledDecision.create(
            decision_space_id=preflight.target_decision_space_ids[0],
            revision=formal_decision(),
            match_reason="Exact product match",
        )
        second = RecalledDecision.create(
            decision_space_id=preflight.target_decision_space_ids[0],
            revision=replace(
                formal_decision(claim="A second application item is complete."),
                decision_id="dec_" + "9" * 32,
            ),
            match_reason="Exact capability match",
        )
        shortlist = RecallShortlist.create(preflight=preflight, items=(first, second))
        self.store.create_activation_attempt(
            session_id="private-session",
            turn_id="delivery-turn",
            cwd=self.cwd,
            repository_id=preflight.repository_id,
            repository_display_name=preflight.repository_display_name,
            attempt_id=ATTEMPT_ID,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
            intent=intent,
            preflight=preflight,
        )
        self.store.attach_activation_card(ATTEMPT_ID, ui_digest="a" * 64)
        claim = self.store.begin_delivery(
            attempt_id=ATTEMPT_ID,
            delivery_id=DELIVERY_ID,
            claim_token="claim_" + "c" * 32,
            current_ui_digest="a" * 64,
            now=NOW,
            claim_expires_at=NOW + timedelta(seconds=30),
        )
        prepared = self.store.commit_prepared_delivery(
            delivery_id=DELIVERY_ID,
            claim_token=claim.claim_token,
            shortlist=shortlist,
            context_text=build_handoff_context(DELIVERY_ID, preflight, shortlist),
            now=NOW,
        )
        if unknown:
            self.store.mark_delivery_unknown(
                delivery_id=DELIVERY_ID,
                now=NOW + timedelta(seconds=30),
            )
        else:
            self.store.ack_delivery(
                delivery_id=DELIVERY_ID,
                context_digest=prepared.context_digest,
                now=NOW,
            )
        self.store.begin_turn_gate(
            session_id="private-session",
            turn_id="application-turn",
            context_epoch=0,
            intent_epoch=0,
            active_generation=preflight.generation,
            gate_id=GATE_ID,
        )
        return shortlist

    @staticmethod
    def _items(shortlist, *dispositions: str):
        return [
            {
                "decision_id": item.revision.decision_id,
                "revision": item.revision.revision,
                "digest": item.digest,
                "disposition": disposition,
                "reason": "Bounded local reason",
            }
            for item, disposition in zip(shortlist.items, dispositions, strict=True)
        ]

    async def test_application_tool_has_closed_model_visible_schema(self) -> None:
        """This catches host coordinates becoming required model authority."""

        tools = {item.name: item for item in await self.server.list_tools()}
        tool = tools["apply_zdecision_recall_delivery"]
        schema = tool.inputSchema

        self.assertEqual(["items"], schema["required"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            {"items", "turn_gate_id", "delivery_id"},
            set(schema["properties"]),
        )
        self.assertEqual(["model", "app"], tool.meta["ui"]["visibility"])
        item_schema = schema["properties"]["items"]["items"]
        if "$ref" in item_schema:
            item_schema = schema["$defs"][item_schema["$ref"].rsplit("/", 1)[-1]]
        fields = {"decision_id", "revision", "digest", "disposition", "reason"}
        self.assertEqual(fields, set(item_schema["properties"]))
        self.assertEqual(fields, set(item_schema["required"]))
        self.assertFalse(item_schema["additionalProperties"])
        self.assertEqual(
            ["applicable", "conflicting", "not_applicable", "uncertain"],
            sorted(item_schema["properties"]["disposition"]["enum"]),
        )

        result = await self.server.call_tool(
            "apply_zdecision_recall_delivery",
            {"items": []},
        )

        self.assertTrue(result.isError)
        self.assertEqual(
            {"state": "blocked", "code": "invalid_application"},
            result.structuredContent,
        )

    async def test_turn_gate_schema_uses_strict_intent_and_optional_host_coordinate(
        self,
    ) -> None:
        """This catches the model being required to invent a trusted gate ID."""

        tools = {item.name: item for item in await self.server.list_tools()}
        schema = tools["gate_zdecision_turn"].inputSchema

        self.assertEqual(["intent"], schema["required"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            {"intent", "turn_gate_id", "explicit_refresh"},
            set(schema["properties"]),
        )

    async def test_turn_gate_rejects_nested_extra_fields_and_boolean_coercion(
        self,
    ) -> None:
        """This catches adapter coercion weakening the strict intent contract."""

        for intent in (
            {**valid_intent().to_dict(), "unexpected": "model-coordinate"},
            {**valid_intent().to_dict(), "explicit_multi_space": 0},
        ):
            with self.subTest(intent=intent):
                with self.assertRaises(ToolError):
                    await self.server.call_tool(
                        "gate_zdecision_turn",
                        {"intent": intent},
                    )

    async def test_same_intent_gate_reuses_without_app_server(self) -> None:
        """This catches the new gate falling back to active-Turn evidence."""

        shortlist = self._prepare_application()
        applied = self.recall.apply_recall_delivery(
            turn_gate_id=GATE_ID,
            delivery_id=DELIVERY_ID,
            items=self._items(shortlist, "applicable", "not_applicable"),
        )
        self.assertEqual("application_committed", applied["state"])
        self.store.begin_turn_gate(
            session_id="private-session",
            turn_id="same-turn",
            context_epoch=0,
            intent_epoch=1,
            active_generation=None,
            gate_id="gate-same",
        )

        class NoRetrievalProvider:
            retrieve_calls = 0

            def preflight(self, **_kwargs):
                raise AssertionError("same intent must not preflight")

            def retrieve(self, _preflight):
                self.retrieve_calls += 1
                raise AssertionError("same intent must not retrieve")

        provider = NoRetrievalProvider()
        self.recall.handoff_service.provider = provider

        with patch.object(
            AppServerGateway,
            "connect",
            side_effect=AssertionError("Recall must not connect an App Server"),
        ):
            result = await self.server.call_tool(
                "gate_zdecision_turn",
                {
                    "turn_gate_id": "gate-same",
                    "intent": valid_intent().to_dict(),
                },
            )
            replay = await self.server.call_tool(
                "gate_zdecision_turn",
                {
                    "turn_gate_id": "gate-same",
                    "intent": valid_intent().to_dict(),
                },
            )

        self.assertFalse(result.isError)
        self.assertEqual("reuse", result.structuredContent["state"])
        self.assertEqual(
            "committed",
            self.store.get_turn_gate("private-session", "same-turn").state,
        )
        self.assertEqual(result, replay)
        self.assertEqual(0, provider.retrieve_calls)

    async def test_changed_intent_gate_returns_typed_handoff_without_app_server(
        self,
    ) -> None:
        """This catches changed intent returning opaque bytes or host proof calls."""

        shortlist = self._prepare_application()
        self.recall.apply_recall_delivery(
            turn_gate_id=GATE_ID,
            delivery_id=DELIVERY_ID,
            items=self._items(shortlist, "applicable", "not_applicable"),
        )
        changed = valid_intent().from_dict(
            {**valid_intent().to_dict(), "feature_goal": "Changed MCP feature"}
        )
        preflight = replace(
            self.store.get_delivery(DELIVERY_ID).preflight,
            intent=changed,
        )
        decision = RecalledDecision.create(
            decision_space_id=preflight.target_decision_space_ids[0],
            revision=replace(
                formal_decision(claim="The changed MCP feature uses this decision."),
                decision_id="dec_" + "8" * 32,
            ),
            match_reason="Changed MCP match",
        )
        changed_shortlist = RecallShortlist.create(
            preflight=preflight, items=(decision,)
        )

        class Provider:
            retrieve_calls = 0

            def preflight(self, **_kwargs):
                return preflight

            def retrieve(self, _preflight):
                self.retrieve_calls += 1
                return changed_shortlist

        provider = Provider()
        self.recall.handoff_service = RecallHandoffService(
            store=self.store,
            provider=provider,
            clock=lambda: NOW,
            delivery_id_factory=lambda _: "delivery_" + "f" * 32,
            claim_token_factory=lambda: "claim_" + "f" * 32,
        )
        self.store.begin_turn_gate(
            session_id="private-session",
            turn_id="changed-turn",
            context_epoch=0,
            intent_epoch=1,
            active_generation=None,
            gate_id="gate-changed",
        )

        with patch.object(
            AppServerGateway,
            "connect",
            side_effect=AssertionError("Recall must not connect an App Server"),
        ):
            result = await self.server.call_tool(
                "gate_zdecision_turn",
                {
                    "turn_gate_id": "gate-changed",
                    "intent": changed.to_dict(),
                },
            )

        self.assertFalse(result.isError)
        self.assertEqual("retrieve", result.structuredContent["state"])
        self.assertEqual([decision.to_dict()], result.structuredContent["decisions"])
        self.assertEqual("ZDECISION_RECALL_HANDOFF", json.loads(result.content[0].text)["marker"])
        self.assertEqual(1, provider.retrieve_calls)

        replay = await self.server.call_tool(
            "gate_zdecision_turn",
            {
                "turn_gate_id": "gate-changed",
                "intent": changed.to_dict(),
            },
        )
        self.assertFalse(replay.isError)
        self.assertEqual(result.structuredContent, replay.structuredContent)
        self.assertEqual(result.content[0].text, replay.content[0].text)
        self.assertEqual(1, provider.retrieve_calls)

    async def test_exact_application_commits_safe_model_visible_summary(self) -> None:
        """This catches MCP bypassing the Task 2 atomic application transaction."""

        shortlist = self._prepare_application()
        items = self._items(shortlist, "applicable", "not_applicable")

        result = await self.server.call_tool(
            "apply_zdecision_recall_delivery",
            {
                "turn_gate_id": GATE_ID,
                "delivery_id": DELIVERY_ID,
                "items": items,
            },
        )

        self.assertFalse(result.isError)
        self.assertEqual("application_committed", result.structuredContent["state"])
        self.assertEqual(1, result.structuredContent["intent_epoch"])
        self.assertEqual(
            {
                "applicable": 1,
                "not_applicable": 1,
                "conflicting": 0,
                "uncertain": 0,
            },
            result.structuredContent["disposition_counts"],
        )
        self.assertEqual(
            ["Recall handoff tests", "Recall handoff tests"],
            result.structuredContent["scope_titles"],
        )
        self.assertEqual({}, result.meta)
        self.assertEqual(1, len(self.store.list_active_items("private-session")))
        model_visible = json.dumps(
            {
                "content": [item.model_dump() for item in result.content],
                "structuredContent": result.structuredContent,
            },
            sort_keys=True,
        )
        for private in (
            "private-session",
            "application-turn",
            GATE_ID,
            DELIVERY_ID,
            self.cwd,
            shortlist.items[0].digest,
        ):
            self.assertNotIn(private, model_visible)

    async def test_unknown_delivery_can_commit_but_wrong_binding_and_items_cannot(
        self,
    ) -> None:
        """This catches application recovery accepting any state or coordinates."""

        shortlist = self._prepare_application(unknown=True)
        items = self._items(shortlist, "applicable", "not_applicable")
        for arguments in (
            {
                "turn_gate_id": "gate-wrong",
                "delivery_id": DELIVERY_ID,
                "items": items,
            },
            {
                "turn_gate_id": GATE_ID,
                "delivery_id": "delivery_" + "e" * 32,
                "items": items,
            },
            {
                "turn_gate_id": GATE_ID,
                "delivery_id": DELIVERY_ID,
                "items": items[:1],
            },
            {
                "turn_gate_id": GATE_ID,
                "delivery_id": DELIVERY_ID,
                "items": [{**items[0], "digest": "0" * 64}, items[1]],
            },
        ):
            with self.subTest(arguments=arguments):
                rejected = await self.server.call_tool(
                    "apply_zdecision_recall_delivery", arguments
                )
                self.assertTrue(rejected.isError)
                self.assertEqual(
                    {"state": "blocked", "code": "invalid_application"},
                    rejected.structuredContent,
                )
                self.assertEqual("delivery_unknown", self.store.get_delivery(DELIVERY_ID).state)

        applied = await self.server.call_tool(
            "apply_zdecision_recall_delivery",
            {"turn_gate_id": GATE_ID, "delivery_id": DELIVERY_ID, "items": items},
        )
        self.assertFalse(applied.isError)
        self.assertEqual("application_committed", applied.structuredContent["state"])

    async def test_conflict_and_uncertainty_commit_but_report_blocked_safely(self) -> None:
        """This catches a blocked application being mistaken for invalid input."""

        shortlist = self._prepare_application()
        items = self._items(shortlist, "conflicting", "uncertain")

        result = await self.server.call_tool(
            "apply_zdecision_recall_delivery",
            {"turn_gate_id": GATE_ID, "delivery_id": DELIVERY_ID, "items": items},
        )

        self.assertFalse(result.isError)
        self.assertEqual("blocked", result.structuredContent["state"])
        self.assertEqual(
            1, result.structuredContent["disposition_counts"]["conflicting"]
        )
        self.assertEqual(
            1, result.structuredContent["disposition_counts"]["uncertain"]
        )
        self.assertEqual("blocked", self.store.get_session("private-session").state)

    async def test_older_turn_gate_cannot_apply_after_a_newer_gate_exists(self) -> None:
        """This catches cross-Turn replay of an otherwise exact delivery binding."""

        shortlist = self._prepare_application()
        self.store.begin_turn_gate(
            session_id="private-session",
            turn_id="newer-turn",
            context_epoch=0,
            intent_epoch=0,
            active_generation=1,
            gate_id="gate-newer",
        )
        items = self._items(shortlist, "applicable", "not_applicable")

        result = await self.server.call_tool(
            "apply_zdecision_recall_delivery",
            {"turn_gate_id": GATE_ID, "delivery_id": DELIVERY_ID, "items": items},
        )

        self.assertTrue(result.isError)
        self.assertEqual(
            {"state": "blocked", "code": "invalid_application"},
            result.structuredContent,
        )
        self.assertEqual("host_delivered", self.store.get_delivery(DELIVERY_ID).state)


class RecallProductionWiringTests(unittest.TestCase):
    def test_create_mcp_server_uses_the_injected_identity_key(self) -> None:
        """This catches a disposable composition retaining the production server key."""

        identity = RecallPluginIdentity(
            plugin_name="recall-gate",
            mcp_server_key="recall-gate-local",
            mcp_command="python",
            mcp_args=("launcher.py", "mcp"),
            hook_command="python launcher.py hook",
            recall_skill_relative_path="skills/recall-gate/SKILL.md",
        )
        with patch("mcp.server.fastmcp.FastMCP") as fast_mcp:
            mcp_server.create_mcp_server(
                object(), recall_identity=identity
            )
        fast_mcp.assert_called_once_with("recall-gate-local")

    def test_run_mcp_uses_unavailable_provider_without_recall_app_server(self) -> None:
        """This catches production Recall reconnecting the obsolete App Server proof."""

        class Server:
            transport: str | None = None

            def run(self, *, transport: str) -> None:
                self.transport = transport

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        database_path = root / "state.sqlite3"
        repository = root / "repository"
        repository.mkdir()
        captured: list[object] = []
        server = Server()

        def create(local_tools, recall_tools):
            captured.extend((local_tools, recall_tools))
            return server

        with (
            patch.object(
                AppServerGateway,
                "connect",
                side_effect=AssertionError("Recall must not connect an App Server"),
            ) as connect,
            patch.object(mcp_server, "create_mcp_server", side_effect=create),
        ):
            mcp_server.run_mcp(
                database_path=database_path,
                config_locator_path=root / "missing-config.json",
                recall_demo_config_path=root / "missing-recall-demo.json",
                cwd=str(repository),
            )

        recall_tools = captured[1]
        provider = getattr(
            recall_tools,
            "provider",
            recall_tools.handoff_service.provider,
        )
        self.assertIsInstance(provider, UnavailableRecallProvider)
        connect.assert_not_called()
        self.assertEqual("stdio", server.transport)

    def test_cli_rejects_removed_recall_host_gate_in_every_environment(self) -> None:
        """This catches the live-acceptance variable reviving the obsolete CLI."""

        for environment in ({}, {"ZDECISION_LIVE_ACCEPTANCE": "1"}):
            with self.subTest(environment=environment):
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch("sys.stderr", io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    cli.build_parser().parse_args(["recall-host-gate", "clear"])


if __name__ == "__main__":
    unittest.main()
