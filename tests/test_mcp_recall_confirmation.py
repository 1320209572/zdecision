from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mcp.server.fastmcp.exceptions import ToolError

from zdecision.agent import mcp_server
from zdecision.agent.db import AgentDatabase
from zdecision.agent.mcp_server import LocalMcpTools
from zdecision.agent.recall_handoff import RecallHandoffService
from zdecision.agent.recall_host_state import RecallHostStore
from zdecision.agent.recall_mcp import RecallMcpTools
from zdecision.recall.handoff import (
    RecallPreflightReady,
    RecallShortlist,
    RecalledDecision,
)
from zdecision.recall.session import RecallIntent

from tests.test_recall_handoff_contracts import formal_decision


NOW = datetime(2026, 8, 9, 4, 0, tzinfo=UTC)
ATTEMPT_ID = "activation_" + "1" * 32
REPOSITORY_ID = "repo_" + "2" * 32
REPOSITORY_NAME = "zdecision"
WIDGET_URI = "ui://zdecision/recall-confirmation-v1.html"
WIDGET_MIME_TYPE = "text/html;profile=mcp-app"
WIDGET_PATH = (
    Path(mcp_server.__file__).resolve().parent
    / "static"
    / "recall-confirmation-v1.html"
)
VALID_INTENT: dict[str, object] = {
    "target_decision_space_ids": ["dsp_" + "3" * 32],
    "explicit_multi_space": False,
    "feature_goal": "Implement the Recall confirmation preflight",
    "domain_objects": ["RecallIntent", "ConfirmationAttempt"],
    "repository_relative_paths": ["src/zdecision/agent"],
    "constraints": ["Use only trusted local state"],
    "exclusions": ["Decision retrieval"],
}

DELIVERY_ID = "delivery_" + "4" * 32


class _McpRecallProvider:
    def __init__(self, shortlist: RecallShortlist) -> None:
        self.shortlist = shortlist
        self.retrieve_calls = 0

    def retrieve(self, preflight):
        self.retrieve_calls += 1
        return self.shortlist


def _ready_preflight(intent: RecallIntent) -> RecallPreflightReady:
    return RecallPreflightReady(
        repository_id=REPOSITORY_ID,
        repository_display_name=REPOSITORY_NAME,
        intent=intent,
        target_decision_space_ids=("dsp_" + "3" * 32,),
        target_display_names=("ZDecision",),
        catalog_digest="a" * 64,
        generation=4,
        generation_digest="b" * 64,
        retrieval_profile_digest="c" * 64,
        index_generation=3,
        freshness="degraded",
        expires_at="2026-08-09T05:00:00Z",
    )


class RecallConfirmationMcpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.cwd = str(self.root / "enabled-repository")
        Path(self.cwd).mkdir()
        self.database_path = self.root / "agent" / "zdecision.sqlite3"
        self.database = AgentDatabase.open(self.database_path)
        self.addCleanup(self.database.close)
        self.store = RecallHostStore.open(self.database_path)
        self.addCleanup(self.store.close)
        self.intent = RecallIntent.from_dict(VALID_INTENT)
        self.preflight = _ready_preflight(self.intent)
        self.store.create_activation_attempt(
            session_id="private-session",
            turn_id="private-turn",
            cwd=self.cwd,
            repository_id=REPOSITORY_ID,
            repository_display_name=REPOSITORY_NAME,
            attempt_id=ATTEMPT_ID,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
            intent=self.intent,
            preflight=self.preflight,
        )
        local = LocalMcpTools(database=self.database, cwd=self.cwd)
        recall = RecallMcpTools(
            host_store=self.store,
            cwd=self.cwd,
            clock=lambda: NOW,
        )
        self.server = mcp_server.create_mcp_server(local, recall)

    async def test_registers_bordered_resource_and_closed_visibility_tools(
        self,
    ) -> None:
        resources = {
            str(item.uri): item for item in await self.server.list_resources()
        }
        tools = {item.name: item for item in await self.server.list_tools()}

        self.assertIn(WIDGET_URI, resources)
        resource = resources[WIDGET_URI]
        self.assertEqual(WIDGET_MIME_TYPE, resource.mimeType)
        self.assertTrue(resource.meta["ui"]["prefersBorder"])
        self.assertEqual(
            {"connectDomains": [], "resourceDomains": []},
            resource.meta["ui"]["csp"],
        )
        self.assertEqual(
            ["model", "app"],
            tools["show_zdecision_recall_confirmation"].meta["ui"][
                "visibility"
            ],
        )
        self.assertEqual(
            ["app"],
            tools["decide_zdecision_recall"].meta["ui"]["visibility"],
        )
        self.assertEqual(
            ["app"],
            tools["get_zdecision_recall_handoff"].meta["ui"]["visibility"],
        )
        self.assertEqual(
            ["app"],
            tools["ack_zdecision_recall_delivery"].meta["ui"]["visibility"],
        )
        self.assertNotIn("activate_zdecision_recall", tools)

        render_schema = tools[
            "show_zdecision_recall_confirmation"
        ].inputSchema
        decision_schema = tools["decide_zdecision_recall"].inputSchema
        status_schema = tools["get_zdecision_recall_handoff"].inputSchema
        ack_schema = tools["ack_zdecision_recall_delivery"].inputSchema
        self.assertTrue(
            tools["show_zdecision_recall_confirmation"].annotations.readOnlyHint
        )
        self.assertEqual(
            {"activation_attempt_id", "intent"},
            set(render_schema["properties"]),
        )
        self.assertNotIn(
            "activation_attempt_id", render_schema.get("required", [])
        )
        self.assertIn("intent", render_schema.get("required", []))
        intent_schema = render_schema["$defs"]["RecallIntentInput"]
        self.assertEqual(set(VALID_INTENT), set(intent_schema["properties"]))
        self.assertEqual(set(VALID_INTENT), set(intent_schema["required"]))
        self.assertFalse(intent_schema.get("additionalProperties", True))
        self.assertEqual(
            {"activation_attempt_id", "action"},
            set(decision_schema["properties"]),
        )
        self.assertEqual(
            ["enable", "decline"],
            decision_schema["properties"]["action"]["enum"],
        )
        self.assertFalse(render_schema.get("additionalProperties", True))
        self.assertFalse(decision_schema.get("additionalProperties", True))
        self.assertTrue(
            tools["get_zdecision_recall_handoff"].annotations.readOnlyHint
        )
        self.assertFalse(
            tools["ack_zdecision_recall_delivery"].annotations.readOnlyHint
        )
        self.assertEqual(
            {"activation_attempt_id"}, set(status_schema["properties"])
        )
        self.assertEqual(
            ["activation_attempt_id"], status_schema.get("required", [])
        )
        self.assertEqual(
            {"activation_attempt_id", "delivery_id", "context_digest"},
            set(ack_schema["properties"]),
        )
        self.assertEqual(
            {"activation_attempt_id", "delivery_id", "context_digest"},
            set(ack_schema.get("required", [])),
        )
        self.assertFalse(status_schema.get("additionalProperties", True))
        self.assertFalse(ack_schema.get("additionalProperties", True))

    async def test_render_without_hook_binding_fails_closed_after_schema_validation(
        self,
    ) -> None:
        """This catches MCP validation preventing the trusted Hook from binding."""

        result = await self.server.call_tool(
            "show_zdecision_recall_confirmation",
            {"intent": dict(VALID_INTENT)},
        )

        self.assertTrue(result.isError)
        self.assertEqual(
            {"state": "blocked", "code": "invalid_confirmation"},
            result.structuredContent,
        )

    async def test_render_binds_exact_html_digest_and_keeps_identity_in_meta(
        self,
    ) -> None:
        result = await self.server.call_tool(
            "show_zdecision_recall_confirmation",
            {
                "activation_attempt_id": ATTEMPT_ID,
                "intent": dict(VALID_INTENT),
            },
        )

        self.assertFalse(result.isError)
        self.assertEqual(
            {"state": "pending_confirmation"}, result.structuredContent
        )
        self.assertEqual(
            ATTEMPT_ID,
            result.meta["zdecision/activation_attempt_id"],
        )
        self.assertEqual(
            REPOSITORY_NAME,
            result.meta["zdecision/repository_display_name"],
        )
        self.assertEqual(
            ["ZDecision"], result.meta["zdecision/target_display_names"]
        )
        self.assertEqual("degraded", result.meta["zdecision/freshness"])
        self.assertEqual(
            {
                "zdecision/activation_attempt_id",
                "zdecision/repository_display_name",
                "zdecision/target_display_names",
                "zdecision/freshness",
            },
            set(result.meta),
        )
        model_visible = json.dumps(
            {
                "content": [item.model_dump() for item in result.content],
                "structuredContent": result.structuredContent,
            },
            sort_keys=True,
        )
        for private in (ATTEMPT_ID, REPOSITORY_ID, REPOSITORY_NAME):
            self.assertNotIn(private, model_visible)
        expected_digest = hashlib.sha256(WIDGET_PATH.read_bytes()).hexdigest()
        self.assertEqual(
            expected_digest,
            self.store.get_activation_attempt(ATTEMPT_ID).ui_digest,
        )

    async def test_missing_trusted_attempt_is_an_unambiguous_tool_error(
        self,
    ) -> None:
        """This catches a blocked confirmation masquerading as a ready card."""

        result = await self.server.call_tool(
            "show_zdecision_recall_confirmation",
            {
                "activation_attempt_id": "activation_" + "9" * 32,
                "intent": dict(VALID_INTENT),
            },
        )

        self.assertTrue(result.isError)
        self.assertEqual(
            {"state": "blocked", "code": "invalid_confirmation"},
            result.structuredContent,
        )
        model_text = " ".join(item.text for item in result.content)
        self.assertIn("unavailable", model_text.lower())
        self.assertIn("Do not retry or guess", model_text)
        self.assertNotIn("confirmation is ready", model_text.lower())
        self.assertEqual({}, result.meta)

    async def test_decline_uses_the_same_card_digest_and_remains_app_only(
        self,
    ) -> None:
        await self.server.call_tool(
            "show_zdecision_recall_confirmation",
            {
                "activation_attempt_id": ATTEMPT_ID,
                "intent": dict(VALID_INTENT),
            },
        )

        result = await self.server.call_tool(
            "decide_zdecision_recall",
            {"activation_attempt_id": ATTEMPT_ID, "action": "decline"},
        )

        self.assertFalse(result.isError)
        self.assertEqual({"state": "declined"}, result.structuredContent)
        self.assertEqual(
            ATTEMPT_ID,
            result.meta["zdecision/activation_attempt_id"],
        )
        self.assertEqual(
            {
                "zdecision/activation_attempt_id",
                "zdecision/repository_display_name",
                "zdecision/target_display_names",
                "zdecision/freshness",
            },
            set(result.meta),
        )
        self.assertEqual(
            REPOSITORY_NAME,
            result.meta["zdecision/repository_display_name"],
        )
        self.assertIsNone(self.store.get_session("private-session"))

    async def test_v1_enable_delegates_to_one_private_frozen_delivery(self) -> None:
        """This catches MCP committing consent without handoff preparation."""

        await self.server.call_tool(
            "show_zdecision_recall_confirmation",
            {
                "activation_attempt_id": ATTEMPT_ID,
                "intent": dict(VALID_INTENT),
            },
        )
        item = RecalledDecision.create(
            decision_space_id=self.preflight.target_decision_space_ids[0],
            revision=formal_decision(),
            match_reason="Exact product match",
        )
        provider = _McpRecallProvider(
            RecallShortlist.create(preflight=self.preflight, items=(item,))
        )
        service = RecallHandoffService(
            store=self.store,
            provider=provider,
            clock=lambda: NOW,
            delivery_id_factory=lambda _: DELIVERY_ID,
            claim_token_factory=lambda: "claim_" + "5" * 32,
        )
        local = LocalMcpTools(database=self.database, cwd=self.cwd)
        recall = RecallMcpTools(
            host_store=self.store,
            handoff_service=service,
            cwd=self.cwd,
            clock=lambda: NOW,
        )
        server = mcp_server.create_mcp_server(local, recall)

        result = await server.call_tool(
            "decide_zdecision_recall",
            {"activation_attempt_id": ATTEMPT_ID, "action": "enable"},
        )

        self.assertFalse(result.isError)
        self.assertEqual({"state": "delivery_claimed"}, result.structuredContent)
        self.assertEqual(1, provider.retrieve_calls)
        self.assertEqual(
            {
                "zdecision/activation_attempt_id",
                "zdecision/repository_display_name",
                "zdecision/target_display_names",
                "zdecision/freshness",
                "zdecision/delivery_id",
                "zdecision/context_text",
                "zdecision/snapshot_digest",
                "zdecision/context_digest",
            },
            set(result.meta),
        )
        self.assertEqual(DELIVERY_ID, result.meta["zdecision/delivery_id"])
        self.assertEqual("activating", self.store.get_session("private-session").state)
        model_visible = json.dumps(
            {
                "content": [content.model_dump() for content in result.content],
                "structuredContent": result.structuredContent,
            },
            sort_keys=True,
        )
        for private in (
            ATTEMPT_ID,
            DELIVERY_ID,
            result.meta["zdecision/context_text"],
            result.meta["zdecision/snapshot_digest"],
            result.meta["zdecision/context_digest"],
        ):
            self.assertNotIn(private, model_visible)

        replay = await server.call_tool(
            "decide_zdecision_recall",
            {"activation_attempt_id": ATTEMPT_ID, "action": "enable"},
        )
        self.assertFalse(replay.isError)
        self.assertEqual(
            {"state": "delivery_claimed", "code": "delivery_in_progress"},
            replay.structuredContent,
        )
        self.assertEqual(1, provider.retrieve_calls)
        self.assertEqual(DELIVERY_ID, replay.meta["zdecision/delivery_id"])
        self.assertEqual(
            result.meta["zdecision/context_digest"],
            replay.meta["zdecision/context_digest"],
        )
        self.assertNotIn("zdecision/context_text", replay.meta)

    async def test_default_provider_fails_closed_after_v1_consent(self) -> None:
        """This catches production fabricating Recall data before Gates B and C."""

        await self.server.call_tool(
            "show_zdecision_recall_confirmation",
            {
                "activation_attempt_id": ATTEMPT_ID,
                "intent": dict(VALID_INTENT),
            },
        )

        result = await self.server.call_tool(
            "decide_zdecision_recall",
            {"activation_attempt_id": ATTEMPT_ID, "action": "enable"},
        )

        self.assertTrue(result.isError)
        self.assertEqual(
            {"state": "blocked", "code": "delivery_prepare_failed"},
            result.structuredContent,
        )
        self.assertNotIn("zdecision/context_text", result.meta)
        self.assertEqual("preparing", self.store.delivery_for_attempt(ATTEMPT_ID).state)
        self.assertEqual("activating", self.store.get_session("private-session").state)

    async def test_status_and_ack_tools_are_private_exact_and_recoverable(self) -> None:
        """This catches app recovery mutating state or accepting a wrong tuple."""

        item = RecalledDecision.create(
            decision_space_id=self.preflight.target_decision_space_ids[0],
            revision=formal_decision(),
            match_reason="Exact product match",
        )
        provider = _McpRecallProvider(
            RecallShortlist.create(preflight=self.preflight, items=(item,))
        )
        service = RecallHandoffService(
            store=self.store,
            provider=provider,
            clock=lambda: NOW,
            delivery_id_factory=lambda _: DELIVERY_ID,
            claim_token_factory=lambda: "claim_" + "5" * 32,
        )
        local = LocalMcpTools(database=self.database, cwd=self.cwd)
        recall = RecallMcpTools(
            host_store=self.store,
            handoff_service=service,
            cwd=self.cwd,
            clock=lambda: NOW,
        )
        server = mcp_server.create_mcp_server(local, recall)
        await server.call_tool(
            "show_zdecision_recall_confirmation",
            {
                "activation_attempt_id": ATTEMPT_ID,
                "intent": dict(VALID_INTENT),
            },
        )

        pending = await server.call_tool(
            "get_zdecision_recall_handoff",
            {"activation_attempt_id": ATTEMPT_ID},
        )
        self.assertEqual({"state": "pending_confirmation"}, pending.structuredContent)
        self.assertIsNone(self.store.delivery_for_attempt(ATTEMPT_ID))

        claimed = await server.call_tool(
            "decide_zdecision_recall",
            {"activation_attempt_id": ATTEMPT_ID, "action": "enable"},
        )
        recovered_claim = await server.call_tool(
            "get_zdecision_recall_handoff",
            {"activation_attempt_id": ATTEMPT_ID},
        )
        self.assertEqual(
            {"state": "delivery_claimed", "code": "delivery_in_progress"},
            recovered_claim.structuredContent,
        )
        self.assertNotIn("zdecision/context_text", recovered_claim.meta)

        exact_digest = claimed.meta["zdecision/context_digest"]
        invalid_arguments = (
            {
                "activation_attempt_id": "activation_" + "9" * 32,
                "delivery_id": DELIVERY_ID,
                "context_digest": exact_digest,
            },
            {
                "activation_attempt_id": ATTEMPT_ID,
                "delivery_id": "delivery_" + "9" * 32,
                "context_digest": exact_digest,
            },
            {
                "activation_attempt_id": ATTEMPT_ID,
                "delivery_id": DELIVERY_ID,
                "context_digest": "9" * 64,
            },
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                rejected = await server.call_tool(
                    "ack_zdecision_recall_delivery", arguments
                )
                self.assertTrue(rejected.isError)
                self.assertEqual(
                    {"state": "blocked", "code": "invalid_delivery"},
                    rejected.structuredContent,
                )
                self.assertEqual(
                    "delivery_claimed", self.store.get_delivery(DELIVERY_ID).state
                )

        acknowledged = await server.call_tool(
            "ack_zdecision_recall_delivery",
            {
                "activation_attempt_id": ATTEMPT_ID,
                "delivery_id": DELIVERY_ID,
                "context_digest": exact_digest,
            },
        )
        self.assertFalse(acknowledged.isError)
        self.assertEqual({"state": "host_delivered"}, acknowledged.structuredContent)
        self.assertEqual(ATTEMPT_ID, acknowledged.meta["zdecision/activation_attempt_id"])
        self.assertEqual(DELIVERY_ID, acknowledged.meta["zdecision/delivery_id"])
        self.assertEqual(exact_digest, acknowledged.meta["zdecision/context_digest"])
        self.assertNotIn("zdecision/context_text", acknowledged.meta)

        reopened = await server.call_tool(
            "get_zdecision_recall_handoff",
            {"activation_attempt_id": ATTEMPT_ID},
        )
        self.assertEqual({"state": "host_delivered"}, reopened.structuredContent)
        self.assertEqual(acknowledged.meta, reopened.meta)
        model_visible = json.dumps(
            {
                "content": [item.model_dump() for item in reopened.content],
                "structuredContent": reopened.structuredContent,
            },
            sort_keys=True,
        )
        for private in (ATTEMPT_ID, DELIVERY_ID, exact_digest):
            self.assertNotIn(private, model_visible)

    async def test_large_valid_frozen_context_reaches_app_meta_unchanged(self) -> None:
        """This catches successful delivery state silently losing context bytes."""

        large_intent = RecallIntent.from_dict(
            {
                "target_decision_space_ids": ["dsp_" + "3" * 32],
                "explicit_multi_space": False,
                "feature_goal": "目" * 2_000,
                "domain_objects": ["域" * 512],
                "repository_relative_paths": ["src/" + "p" * 500],
                "constraints": ["约" * 512],
                "exclusions": [],
            }
        )
        preflight = _ready_preflight(large_intent)
        items = tuple(
            RecalledDecision.create(
                decision_space_id=preflight.target_decision_space_ids[0],
                revision=replace(
                    formal_decision(),
                    decision_id="dec_" + f"{index:032x}",
                ),
                match_reason="理" * 2_000,
            )
            for index in range(8)
        )
        shortlist = RecallShortlist.create(preflight=preflight, items=items)
        provider = _McpRecallProvider(shortlist)
        large_attempt_id = "activation_" + "6" * 32
        self.store.create_activation_attempt(
            session_id="large-session",
            turn_id="large-turn",
            cwd=self.cwd,
            repository_id=REPOSITORY_ID,
            repository_display_name=REPOSITORY_NAME,
            attempt_id=large_attempt_id,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
            intent=large_intent,
            preflight=preflight,
        )
        service = RecallHandoffService(
            store=self.store,
            provider=provider,
            clock=lambda: NOW,
            delivery_id_factory=lambda _: DELIVERY_ID,
            claim_token_factory=lambda: "claim_" + "7" * 32,
        )
        local = LocalMcpTools(database=self.database, cwd=self.cwd)
        recall = RecallMcpTools(
            host_store=self.store,
            handoff_service=service,
            cwd=self.cwd,
            clock=lambda: NOW,
        )
        server = mcp_server.create_mcp_server(local, recall)
        await server.call_tool(
            "show_zdecision_recall_confirmation",
            {
                "activation_attempt_id": large_attempt_id,
                "intent": large_intent.to_dict(),
            },
        )

        result = await server.call_tool(
            "decide_zdecision_recall",
            {"activation_attempt_id": large_attempt_id, "action": "enable"},
        )

        frozen = self.store.get_delivery(DELIVERY_ID)
        self.assertGreater(len(frozen.context_text.encode("utf-8")), 65_536)
        self.assertFalse(result.isError)
        self.assertEqual({"state": "delivery_claimed"}, result.structuredContent)
        self.assertIn("zdecision/context_text", result.meta)
        self.assertEqual(frozen.context_text, result.meta["zdecision/context_text"])
        model_visible = json.dumps(
            {
                "content": [content.model_dump() for content in result.content],
                "structuredContent": result.structuredContent,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertNotIn(frozen.context_text, model_visible)

    async def test_legacy_attempt_cannot_authorize_recall(self) -> None:
        """This catches an old confirmation row authorizing the v1 workflow."""

        legacy_attempt_id = "activation_" + "8" * 32
        self.store.create_activation_attempt(
            session_id="legacy-session",
            turn_id="legacy-turn",
            cwd=self.cwd,
            repository_id=REPOSITORY_ID,
            repository_display_name=REPOSITORY_NAME,
            attempt_id=legacy_attempt_id,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
        )
        self.store.attach_activation_card(
            legacy_attempt_id,
            ui_digest=hashlib.sha256(WIDGET_PATH.read_bytes()).hexdigest(),
        )

        result = await self.server.call_tool(
            "decide_zdecision_recall",
            {"activation_attempt_id": legacy_attempt_id, "action": "enable"},
        )

        self.assertTrue(result.isError)
        self.assertEqual(
            {"state": "blocked", "code": "invalid_confirmation"},
            result.structuredContent,
        )
        self.assertIsNone(self.store.get_session("legacy-session"))
        self.assertEqual(
            "pending_confirmation",
            self.store.get_activation_attempt(legacy_attempt_id).state,
        )
        self.assertIsNone(self.store.delivery_for_attempt(legacy_attempt_id))

    async def test_render_rejects_mismatched_intent_and_legacy_attempt(self) -> None:
        mismatched = {**VALID_INTENT, "feature_goal": "A substituted intent"}
        legacy_attempt_id = "activation_" + "8" * 32
        self.store.create_activation_attempt(
            session_id="legacy-session",
            turn_id="legacy-turn",
            cwd=self.cwd,
            repository_id=REPOSITORY_ID,
            repository_display_name=REPOSITORY_NAME,
            attempt_id=legacy_attempt_id,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
        )

        for attempt_id, intent in (
            (ATTEMPT_ID, mismatched),
            (legacy_attempt_id, VALID_INTENT),
        ):
            with self.subTest(attempt_id=attempt_id):
                result = await self.server.call_tool(
                    "show_zdecision_recall_confirmation",
                    {
                        "activation_attempt_id": attempt_id,
                        "intent": dict(intent),
                    },
                )
                self.assertTrue(result.isError)
                self.assertEqual(
                    {"state": "blocked", "code": "invalid_confirmation"},
                    result.structuredContent,
                )

    async def test_tools_reject_host_coordinates_and_untrusted_flags(
        self,
    ) -> None:
        registered = {item.name for item in await self.server.list_tools()}
        self.assertIn("show_zdecision_recall_confirmation", registered)
        self.assertIn("decide_zdecision_recall", registered)
        forbidden = {
            "session_id": "private-session",
            "cwd": self.cwd,
            "repository_id": REPOSITORY_ID,
            "confirmed": True,
        }
        for name, arguments in (
            (
                "show_zdecision_recall_confirmation",
                {
                    "activation_attempt_id": ATTEMPT_ID,
                    "intent": dict(VALID_INTENT),
                },
            ),
            (
                "decide_zdecision_recall",
                {"activation_attempt_id": ATTEMPT_ID, "action": "decline"},
            ),
        ):
            for field, value in forbidden.items():
                with self.subTest(tool=name, field=field):
                    with self.assertRaises(ToolError):
                        await self.server.call_tool(
                            name, {**arguments, field: value}
                        )


class RecallConfirmationCardTests(unittest.TestCase):
    def test_card_contains_only_the_two_confirmation_buttons(self) -> None:
        self.assertTrue(WIDGET_PATH.is_file(), f"missing card: {WIDGET_PATH}")
        html = WIDGET_PATH.read_text("utf-8")

        self.assertEqual(2, html.count("<button"))
        self.assertIn("启用本任务决策召回", html)
        self.assertIn("暂不启用", html)
        self.assertIn("当前任务期间", html)
        lowered = html.lower()
        for forbidden in (
            "session_id",
            "turn_id",
            "cwd",
            "/users/",
            "innerhtml",
            "<link",
            "linear-gradient",
            "radial-gradient",
            "animation:",
            "inter,",
            "arial,",
            "roboto,",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)

    def test_load_remount_duplicate_result_and_timeout_never_auto_enable(
        self,
    ) -> None:
        output = self._run_card(
            """
  const first = await mount();
  check(first.toolCalls().length === 0, "initialization called a tool");
  first.deliverRender();
  first.deliverRender();
  await flush();
  check(first.toolCalls().length === 0, "pending mount called a tool");

  const second = await mount();
  second.deliverRender();
  await flush();
  check(second.toolCalls().length === 0, "pending remount called a tool");

  const click = second.elements.enable.dispatch("click");
  const calls = second.toolCalls();
  check(calls.length === 1, "one click did not make exactly one decision call");
  check(
    JSON.stringify(calls[0].params) === JSON.stringify({
      name: "decide_zdecision_recall",
      arguments: { activation_attempt_id: attemptId, action: "enable" },
    }),
    "enable click sent an unexpected payload",
  );
  second.takeTimer(5000)();
  await click;
  await flush();
  second.runAllTimers();
  await flush();
  check(second.toolCalls().length === 1, "timeout automatically retried enable");
  process.stdout.write("no-auto-enable-ok");
""",
        )
        self.assertEqual("no-auto-enable-ok", output)

    def test_enable_delivers_one_complete_snapshot_then_acks_without_message(self) -> None:
        output = self._run_card(
            """
  const widget = await mount();
  widget.deliverRender();
  await flush();
  const click = widget.elements.enable.dispatch("click");
  const decision = widget.toolCalls()[0];
  widget.respond(decision, deliveryClaimedResult());
  await waitFor(() => widget.contextUpdates()[0], "missing context update");
  const updates = widget.contextUpdates();
  check(updates.length === 1, "missing single context update");
  check(
    JSON.stringify(updates[0].params) === JSON.stringify({
      content: [{ type: "text", text: contextText }],
    }),
    "context update did not contain the complete snapshot",
  );
  check(widget.messages().length === 0, "ui/message must not be used");
  widget.respond(updates[0], {});
  await waitFor(() => widget.toolCalls()[1], "missing delivery ack");
  const calls = widget.toolCalls();
  check(calls.length === 2, "ack did not follow context update");
  check(
    calls[1].params?.name === "ack_zdecision_recall_delivery",
    "ack did not follow context update",
  );
  check(
    JSON.stringify(calls[1].params?.arguments) === JSON.stringify({
      activation_attempt_id: attemptId,
      delivery_id: deliveryId,
      context_digest: contextDigest,
    }),
    "ack tuple changed",
  );
  widget.respond(calls[1], acknowledgedResult());
  await click;
  await flush();
  check(widget.contextUpdates().length === 1, "context update repeated");
  check(widget.messages().length === 0, "ui/message must not be used");
  check(
    widget.elements.status.textContent ===
      "决策已交付。请保留此附件并发送下一条原生消息；应用完成前不会修改代码。",
    "card omitted the exact next-native-message instruction",
  );
  process.stdout.write("one-update-no-message-ok");
""",
        )
        self.assertEqual("one-update-no-message-ok", output)

    def test_missing_or_malformed_host_capabilities_are_unsupported(self) -> None:
        output = self._run_card(
            """
  const cases = [
    {},
    { serverTools: true, updateModelContext: { text: {} } },
    { serverTools: {}, updateModelContext: true },
    { serverTools: {}, updateModelContext: { text: true } },
    { serverTools: {}, updateModelContext: {} },
  ];
  for (const capabilities of cases) {
    const widget = await mount(capabilities);
    widget.deliverRender();
    await flush();
    check(widget.elements.enable.disabled, "unsupported host enabled delivery");
    await widget.elements.enable.dispatch("click");
    await flush();
    check(widget.toolCalls().length === 0, "unsupported host called a tool");
    check(widget.contextUpdates().length === 0, "unsupported host updated context");
    check(widget.messages().length === 0, "unsupported host sent ui/message");
  }
  process.stdout.write("capability-closed-ok");
""",
        )
        self.assertEqual("capability-closed-ok", output)

    def test_remount_is_read_only_and_explicit_retry_reuses_exact_bytes(self) -> None:
        output = self._run_card(
            """
  const remount = await mount();
  remount.deliverRender("committed");
  await flush();
  const recover = remount.toolCalls()[0];
  check(recover.params?.name === "get_zdecision_recall_handoff", "wrong recovery tool");
  check(
    JSON.stringify(recover.params?.arguments) === JSON.stringify({
      activation_attempt_id: attemptId,
    }),
    "recovery arguments changed",
  );
  remount.respond(recover, deliveryStateResult("delivery_unknown"));
  await flush();
  check(remount.toolCalls().length === 1, "remount mutated state");
  check(remount.contextUpdates().length === 0, "remount updated context");
  check(remount.messages().length === 0, "remount sent ui/message");
  check(remount.elements.enable.textContent === "重新交付", "retry label missing");
  check(!remount.elements.enable.disabled, "explicit retry remained disabled");

  const click = remount.elements.enable.dispatch("click");
  const retry = remount.toolCalls()[1];
  check(retry.params?.name === "decide_zdecision_recall", "retry used wrong tool");
  remount.respond(retry, deliveryClaimedResult());
  const update = await waitFor(
    () => remount.contextUpdates()[0],
    "missing retry context update",
  );
  check(update.params?.content?.[0]?.text === contextText, "retry bytes changed");
  remount.respond(update, {});
  const ack = await waitFor(() => remount.toolCalls()[2], "missing retry ack");
  check(ack.params?.arguments?.delivery_id === deliveryId, "retry delivery changed");
  check(
    ack.params?.arguments?.context_digest === contextDigest,
    "retry context digest changed",
  );
  remount.respond(ack, acknowledgedResult());
  await click;
  await flush();
  check(remount.contextUpdates().length === 1, "explicit retry updated more than once");
  check(remount.messages().length === 0, "explicit retry sent ui/message");

  const delivered = await mount();
  delivered.deliverRender("committed");
  await flush();
  const deliveredStatus = delivered.toolCalls()[0];
  delivered.respond(deliveredStatus, deliveryStateResult("host_delivered"));
  await flush();
  check(delivered.toolCalls().length === 1, "delivered remount mutated state");
  check(delivered.contextUpdates().length === 0, "delivered remount repeated context");
  check(delivered.elements.enable.disabled, "delivered remount enabled retry");
  check(delivered.elements["card-state"].textContent === "已交付", "delivery not restored");
  process.stdout.write("remount-retry-ok");
""",
        )
        self.assertEqual("remount-retry-ok", output)

    def test_update_and_ack_failures_never_ack_or_resend_automatically(self) -> None:
        output = self._run_card(
            """
  const updateFailed = await mount();
  updateFailed.deliverRender();
  await flush();
  const failedClick = updateFailed.elements.enable.dispatch("click");
  updateFailed.respond(updateFailed.toolCalls()[0], deliveryClaimedResult());
  const failedUpdate = await waitFor(
    () => updateFailed.contextUpdates()[0],
    "missing rejected context update",
  );
  updateFailed.reject(failedUpdate, { code: -32603, message: "rejected" });
  await failedClick;
  await flush();
  updateFailed.runAllTimers();
  await flush();
  check(updateFailed.toolCalls().length === 1, "failed update sent ack or retry");
  check(updateFailed.contextUpdates().length === 1, "failed update retried");
  check(updateFailed.messages().length === 0, "failed update sent ui/message");

  const updateTimedOut = await mount();
  updateTimedOut.deliverRender();
  await flush();
  const timedOutClick = updateTimedOut.elements.enable.dispatch("click");
  updateTimedOut.respond(updateTimedOut.toolCalls()[0], deliveryClaimedResult());
  await waitFor(
    () => updateTimedOut.contextUpdates()[0],
    "missing timed out context update",
  );
  updateTimedOut.takeTimer(5000)();
  await timedOutClick;
  await flush();
  check(updateTimedOut.toolCalls().length === 1, "update timeout sent ack or retry");
  check(updateTimedOut.contextUpdates().length === 1, "update timeout resent context");
  check(updateTimedOut.messages().length === 0, "update timeout sent ui/message");

  const ackUnknown = await mount();
  ackUnknown.deliverRender();
  await flush();
  const unknownClick = ackUnknown.elements.enable.dispatch("click");
  ackUnknown.respond(ackUnknown.toolCalls()[0], deliveryClaimedResult());
  const unknownUpdate = await waitFor(
    () => ackUnknown.contextUpdates()[0],
    "missing ack-timeout context update",
  );
  ackUnknown.respond(unknownUpdate, {});
  await waitFor(() => ackUnknown.toolCalls()[1], "missing timed out ack");
  check(
    ackUnknown.toolCalls()[1].params?.name === "ack_zdecision_recall_delivery",
    "ack was not attempted once",
  );
  ackUnknown.takeTimer(5000)();
  await unknownClick;
  await flush();
  ackUnknown.runAllTimers();
  await flush();
  check(ackUnknown.toolCalls().length === 2, "ack timeout resent a tool call");
  check(ackUnknown.contextUpdates().length === 1, "ack timeout resent context");
  check(ackUnknown.messages().length === 0, "ack timeout sent ui/message");
  check(
    ackUnknown.elements["card-state"].textContent === "交付状态未知",
    "ack timeout did not display unknown",
  );

  const ackRejected = await mount();
  ackRejected.deliverRender();
  await flush();
  const rejectedClick = ackRejected.elements.enable.dispatch("click");
  ackRejected.respond(ackRejected.toolCalls()[0], deliveryClaimedResult());
  const rejectedUpdate = await waitFor(
    () => ackRejected.contextUpdates()[0],
    "missing ack-rejected context update",
  );
  ackRejected.respond(rejectedUpdate, {});
  await waitFor(() => ackRejected.toolCalls()[1], "missing rejected ack");
  ackRejected.reject(
    ackRejected.toolCalls()[1],
    { code: -32603, message: "ack rejected" },
  );
  await rejectedClick;
  await flush();
  check(ackRejected.toolCalls().length === 2, "ack rejection resent a tool call");
  check(ackRejected.contextUpdates().length === 1, "ack rejection resent context");
  check(ackRejected.messages().length === 0, "ack rejection sent ui/message");
  check(
    ackRejected.elements["card-state"].textContent === "交付状态未知",
    "ack rejection did not display unknown",
  );

  const unusableAckResults = [
    {
      content: [{ type: "text", text: "bounded" }],
      structuredContent: { state: "blocked", code: "invalid_delivery" },
      _meta: {},
      isError: true,
    },
    {
      content: [{ type: "text", text: "bounded" }],
      structuredContent: { state: "host_delivered" },
      _meta: { "zdecision/activation_attempt_id": attemptId },
    },
  ];
  for (const unusable of unusableAckResults) {
    const widget = await mount();
    widget.deliverRender();
    await flush();
    const click = widget.elements.enable.dispatch("click");
    widget.respond(widget.toolCalls()[0], deliveryClaimedResult());
    const update = await waitFor(
      () => widget.contextUpdates()[0],
      "missing unusable-ack context update",
    );
    widget.respond(update, {});
    await waitFor(() => widget.toolCalls()[1], "missing unusable ack");
    widget.respond(widget.toolCalls()[1], unusable);
    await click;
    await flush();
    check(widget.toolCalls().length === 2, "unusable ack resent a tool call");
    check(widget.contextUpdates().length === 1, "unusable ack resent context");
    check(widget.messages().length === 0, "unusable ack sent ui/message");
    check(
      widget.elements["card-state"].textContent === "交付状态未知",
      "unusable ack did not display unknown",
    );
  }
  process.stdout.write("failure-no-resend-ok");
""",
        )
        self.assertEqual("failure-no-resend-ok", output)

    def test_explicit_retry_rejects_substituted_delivery_or_digests(self) -> None:
        output = self._run_card(
            """
  const mutations = [
    ["zdecision/delivery_id", "delivery_99999999999999999999999999999999"],
    ["zdecision/snapshot_digest", "9".repeat(64)],
    ["zdecision/context_digest", "9".repeat(64)],
    ["zdecision/context_text", "substituted Recall context bytes"],
  ];
  for (const [key, replacement] of mutations) {
    const widget = await mount();
    widget.deliverRender("committed");
    await flush();
    widget.respond(widget.toolCalls()[0], deliveryStateResult("delivery_unknown"));
    await flush();
    const click = widget.elements.enable.dispatch("click");
    const substituted = deliveryClaimedResult();
    substituted._meta[key] = replacement;
    widget.respond(widget.toolCalls()[1], substituted);
    await flush();
    check(widget.contextUpdates().length === 0, `${key} substitution updated context`);
    await click;
    check(widget.toolCalls().length === 2, `${key} substitution sent ack`);
    check(widget.messages().length === 0, `${key} substitution sent ui/message`);
    check(
      widget.elements["card-state"].textContent === "无法确认",
      `${key} substitution did not fail closed`,
    );
  }

  const cryptoUnavailable = await mount(undefined, null);
  cryptoUnavailable.deliverRender();
  await flush();
  const cryptoClick = cryptoUnavailable.elements.enable.dispatch("click");
  cryptoUnavailable.respond(
    cryptoUnavailable.toolCalls()[0],
    deliveryClaimedResult(),
  );
  await cryptoClick;
  await flush();
  check(cryptoUnavailable.contextUpdates().length === 0, "crypto failure updated context");
  check(cryptoUnavailable.toolCalls().length === 1, "crypto failure sent ack");
  check(cryptoUnavailable.messages().length === 0, "crypto failure sent ui/message");
  process.stdout.write("retry-substitution-closed-ok");
""",
        )
        self.assertEqual("retry-substitution-closed-ok", output)

    def test_decline_never_enables_or_requests_recall_continuation(self) -> None:
        output = self._run_card(
            """
  const widget = await mount();
  widget.deliverRender();
  await flush();
  const click = widget.elements.decline.dispatch("click");
  const decisions = widget.toolCalls();
  check(decisions.length === 1, "decline did not make one decision call");
  check(
    decisions[0].params?.arguments?.action === "decline",
    "decline sent an enable action",
  );
  widget.respond(decisions[0], result("declined"));
  await click;
  await flush();
  widget.deliverDeclinedNotification();
  await flush();
  check(
    widget.toolCalls().every((call) => call.params?.arguments?.action !== "enable"),
    "decline emitted enable",
  );
  check(widget.messages().length === 0, "decline requested Recall continuation");
  check(widget.contextUpdates().length === 0, "decline updated model context");
  check(
    widget.toolCalls().every(
      (call) => call.params?.name !== "ack_zdecision_recall_delivery",
    ),
    "decline sent a delivery acknowledgement",
  );
  process.stdout.write("decline-is-terminal-ok");
""",
        )
        self.assertEqual("decline-is-terminal-ok", output)

    def test_decision_results_require_the_current_attempt_for_both_actions(
        self,
    ) -> None:
        output = self._run_card(
            """
  const cases = [
    { action: "enable", state: "delivery_claimed", button: "enable" },
    { action: "decline", state: "declined", button: "decline" },
  ];
  const mutations = [
    { label: "missing", responseAttempt: null, includeAttempt: false },
    {
      label: "mismatch",
      responseAttempt: "activation_99999999999999999999999999999999",
      includeAttempt: true,
    },
  ];

  for (const item of cases) {
    for (const mutation of mutations) {
      const widget = await mount();
      widget.deliverRender();
      await flush();
      const click = widget.elements[item.button].dispatch("click");
      const decision = widget.toolCalls()[0];
      const response = item.action === "enable"
        ? deliveryClaimedResult()
        : result(item.state);
      if (mutation.includeAttempt) {
        response._meta["zdecision/activation_attempt_id"] = mutation.responseAttempt;
      } else {
        delete response._meta["zdecision/activation_attempt_id"];
      }
      widget.respond(decision, response);
      await click;
      await flush();
      check(
        widget.elements["card-state"].textContent === "无法确认",
        `${item.action}/${mutation.label} displayed terminal success`,
      );
      check(
        widget.messages().length === 0,
        `${item.action}/${mutation.label} requested continuation`,
      );
    }
  }
  process.stdout.write("attempt-bound-decisions-ok");
""",
        )
        self.assertEqual("attempt-bound-decisions-ok", output)

    def _run_card(self, scenario: str) -> str:
        self.assertTrue(WIDGET_PATH.is_file(), f"missing card: {WIDGET_PATH}")
        html = WIDGET_PATH.read_text("utf-8")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        harness = r"""
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");
const { TextEncoder } = require("node:util");
const shippedScript = __SHIPPED_SCRIPT__;
const attemptId = "activation_11111111111111111111111111111111";
const repositoryName = "zdecision";
const deliveryId = "delivery_44444444444444444444444444444444";
const snapshotDigest = "5".repeat(64);
const contextDigest = "93b7dd65fec8ecb7149dce23056ec66b64cab469f5f5e5072de73300bd999076";
const contextText = "ZDecision Recall handoff snapshot\ncomplete bytes";

function check(condition, message) {
  if (!condition) throw new Error(message);
}

async function flush() {
  for (let index = 0; index < 6; index += 1) {
    await Promise.resolve();
    await new Promise((resolve) => setImmediate(resolve));
  }
}

async function waitFor(read, message) {
  for (let index = 0; index < 100; index += 1) {
    const value = read();
    if (value !== undefined) return value;
    await new Promise((resolve) => setImmediate(resolve));
  }
  throw new Error(message);
}

function result(state, responseAttempt = attemptId, includeAttempt = true) {
  const meta = {
    "zdecision/repository_display_name": repositoryName,
  };
  if (includeAttempt) {
    meta["zdecision/activation_attempt_id"] = responseAttempt;
  }
  return {
    content: [{ type: "text", text: "bounded" }],
    structuredContent: { state },
    _meta: meta,
  };
}

function deliveryClaimedResult() {
  return {
    content: [{ type: "text", text: "bounded" }],
    structuredContent: { state: "delivery_claimed" },
    _meta: {
      "zdecision/activation_attempt_id": attemptId,
      "zdecision/repository_display_name": repositoryName,
      "zdecision/delivery_id": deliveryId,
      "zdecision/snapshot_digest": snapshotDigest,
      "zdecision/context_digest": contextDigest,
      "zdecision/context_text": contextText,
    },
  };
}

function acknowledgedResult() {
  return {
    content: [{ type: "text", text: "bounded" }],
    structuredContent: { state: "host_delivered" },
    _meta: {
      "zdecision/activation_attempt_id": attemptId,
      "zdecision/repository_display_name": repositoryName,
      "zdecision/delivery_id": deliveryId,
      "zdecision/snapshot_digest": snapshotDigest,
      "zdecision/context_digest": contextDigest,
    },
  };
}

function deliveryStateResult(state) {
  const value = acknowledgedResult();
  value.structuredContent.state = state;
  return value;
}

async function mount(capabilities = {
  serverTools: {}, updateModelContext: { text: {} },
}, hostCrypto = webcrypto) {
  const outbound = [];
  const timers = new Map();
  let nextTimerId = 1;
  let messageHandler = null;

  class Element {
    constructor() {
      this.disabled = false;
      this.hidden = false;
      this.textContent = "";
      this.listeners = new Map();
    }

    addEventListener(name, listener) {
      const listeners = this.listeners.get(name) || [];
      listeners.push(listener);
      this.listeners.set(name, listeners);
    }

    dispatch(name) {
      return Promise.all(
        (this.listeners.get(name) || []).map((listener) => listener()),
      );
    }
  }

  const elements = Object.fromEntries(
    ["enable", "decline", "repository", "status", "card-state"].map(
      (id) => [id, new Element()],
    ),
  );
  const host = { postMessage(message) { outbound.push(message); } };
  const sandbox = {
    document: { getElementById: (id) => elements[id] },
    window: {
      parent: host,
      crypto: hostCrypto,
      addEventListener(name, listener) {
        if (name === "message") messageHandler = listener;
      },
    },
    setTimeout(callback, delay) {
      const id = nextTimerId++;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    TextEncoder,
  };
  vm.runInNewContext(shippedScript, sandbox);

  function deliver(message, source = host) {
    messageHandler({ source, data: message });
  }

  function respond(call, response) {
    deliver({ jsonrpc: "2.0", id: call.id, result: response });
  }

  function reject(call, error) {
    deliver({ jsonrpc: "2.0", id: call.id, error });
  }

  function toolCalls() {
    return outbound.filter((message) => message.method === "tools/call");
  }

  function messages() {
    return outbound.filter((message) => message.method === "ui/message");
  }

  function contextUpdates() {
    return outbound.filter(
      (message) => message.method === "ui/update-model-context",
    );
  }

  function takeTimer(delay) {
    const entry = [...timers.entries()].find(([, timer]) => timer.delay === delay);
    check(entry, `missing ${delay}ms timer`);
    timers.delete(entry[0]);
    return entry[1].callback;
  }

  function runAllTimers() {
    const callbacks = [...timers.values()].map((timer) => timer.callback);
    timers.clear();
    for (const callback of callbacks) callback();
  }

  function deliverRender(state = "pending_confirmation") {
    deliver({
      jsonrpc: "2.0",
      method: "ui/notifications/tool-result",
      params: result(state),
    });
  }

  function deliverCommittedNotification() {
    deliver({
      jsonrpc: "2.0",
      method: "ui/notifications/tool-result",
      params: result("committed"),
    });
  }

  function deliverDeclinedNotification() {
    deliver({
      jsonrpc: "2.0",
      method: "ui/notifications/tool-result",
      params: result("declined"),
    });
  }

  const initialize = outbound.find(
    (message) => message.method === "ui/initialize",
  );
  check(initialize, "card did not initialize the MCP Apps bridge");
  deliver({
    jsonrpc: "2.0",
    id: initialize.id,
    result: { hostCapabilities: capabilities },
  });
  await flush();
  return {
    deliver,
    deliverRender,
    deliverCommittedNotification,
    deliverDeclinedNotification,
    elements,
    contextUpdates,
    messages,
    outbound,
    reject,
    respond,
    runAllTimers,
    takeTimer,
    toolCalls,
  };
}

(async () => {
__SCENARIO__
})().catch((error) => {
  process.stderr.write(error.stack || String(error));
  process.exitCode = 1;
});
"""
        harness = harness.replace("__SHIPPED_SCRIPT__", json.dumps(script))
        harness = harness.replace("__SCENARIO__", scenario)
        completed = subprocess.run(
            ["node", "-e", harness],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(
            0,
            completed.returncode,
            completed.stderr or completed.stdout,
        )
        return completed.stdout


if __name__ == "__main__":
    unittest.main()
