"""Model-visible Recall delivery application MCP contract tests."""

from __future__ import annotations

import tempfile
import unittest
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zdecision.agent import mcp_server
from zdecision.agent.db import AgentDatabase
from zdecision.agent.mcp_server import LocalMcpTools
from zdecision.agent.recall_host_state import RecallHostStore
from zdecision.agent.recall_mcp import ReadinessRecallGateProvider, RecallMcpTools
from zdecision.recall.handoff import (
    RecallShortlist,
    RecalledDecision,
    build_handoff_context,
)

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
        recall = RecallMcpTools(
            host_store=self.store,
            provider=ReadinessRecallGateProvider(),
            cwd=self.cwd,
            clock=lambda: NOW,
        )
        self.server = mcp_server.create_mcp_server(local, recall)

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


if __name__ == "__main__":
    unittest.main()
