"""Tests for the durable, one-shot Recall elicitation probe state."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import unittest
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcp import types
from mcp.server.fastmcp import Context
from mcp.shared.context import RequestContext
from mcp.shared.memory import create_connected_server_and_client_session

from zdecision.jsonio import canonical_json_bytes

from tests.recall_elicitation_probe import (
    ELICITATION_MESSAGE,
    EmptyConfirmation,
    ProbeConflict,
    ProbeReceiptStore,
    _run_probe,
    build_probe_server,
    request_digest,
    supports_form_elicitation,
)


NOW = datetime(2026, 8, 9, 3, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=1)


class ProbeReceiptStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "probe.sqlite3"
        self.store = ProbeReceiptStore.open(self.database_path)
        self.addCleanup(self.store.close)

    def test_arm_claim_accept_is_one_shot(self) -> None:
        """This catches a claimed prompt that accepts more than once."""

        self.store.arm("accept", now=NOW)
        pending = self.store.claim_armed(request_digest="a" * 64, now=NOW)
        self.assertEqual((pending.state, pending.prompt_count), ("pending", 1))
        accepted = self.store.complete("accept", state="accept", now=NOW)
        self.assertEqual(
            (accepted.state, accepted.prompt_count, accepted.completion_count),
            ("accept", 1, 1),
        )
        self.assertEqual(
            self.store.current(),
            accepted,
        )

    def test_decline_and_cancel_are_terminal_non_accepting_results(self) -> None:
        """This catches declining or cancelling without completing the receipt."""

        for case_id, state in (("decline", "decline"), ("cancel", "cancel")):
            self.store.arm(case_id, now=NOW)
            self.store.claim_armed(request_digest="b" * 64, now=NOW)
            receipt = self.store.complete(case_id, state=state, now=NOW)
            self.assertEqual(receipt.state, state)
            self.assertEqual(receipt.completion_count, 1)

    def test_restart_recovers_pending_as_transport_lost_without_reprompt(self) -> None:
        """This catches a restart re-arming an already-shown prompt."""

        self.store.arm("restart", now=NOW)
        self.store.claim_armed(request_digest="c" * 64, now=NOW)
        self.store.close()
        reopened = ProbeReceiptStore.open(self.database_path)
        self.addCleanup(reopened.close)
        recovered = reopened.recover_pending(now=LATER)
        self.assertEqual([item.state for item in recovered], ["transport_lost"])
        self.assertEqual(reopened.receipt("restart").prompt_count, 1)
        with self.assertRaises(ProbeConflict):
            reopened.arm("restart", now=LATER)

    def test_report_and_database_exclude_private_sentinels(self) -> None:
        """This catches a report or receipt database retaining prompt-source data."""

        sentinel = "PRIVATE_PROMPT_SOURCE_DIFF_DECISION_SENTINEL"
        self.store.arm("accept", now=NOW)
        report = canonical_json_bytes(self.store.report())
        self.assertNotIn(sentinel.encode(), report)
        self.assertNotIn(sentinel.encode(), self.database_path.read_bytes())

    def test_rejects_invalid_case_state_digest_and_naive_time(self) -> None:
        """This catches malformed input entering durable probe state."""

        with self.assertRaises(ValueError):
            self.store.arm("wrong", now=NOW)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.store.arm("accept", now=NOW.replace(tzinfo=None))

        self.store.arm("accept", now=NOW)
        with self.assertRaises(ValueError):
            self.store.claim_armed(request_digest="A" * 64, now=NOW)
        with self.assertRaises(ValueError):
            self.store.claim_armed(request_digest="a" * 63, now=NOW)
        with self.assertRaises(ValueError):
            self.store.claim_armed(request_digest="a" * 64, now=NOW.replace(tzinfo=None))
        self.store.claim_armed(request_digest="a" * 64, now=NOW)
        with self.assertRaises(ValueError):
            self.store.complete("accept", state="pending", now=NOW)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.store.complete("accept", state="wrong", now=NOW)  # type: ignore[arg-type]

    def test_normalizes_aware_timestamps_to_utc_z(self) -> None:
        """This catches durable receipts retaining a non-UTC clock representation."""

        timestamp = datetime.fromisoformat("2026-08-09T11:00:00+08:00")

        receipt = self.store.arm("accept", now=timestamp)

        self.assertEqual(receipt.updated_at, "2026-08-09T03:00:00Z")

    def test_refuses_two_simultaneously_armed_cases(self) -> None:
        """This catches a second case being armed while another one is active."""

        armed = self.store.arm("accept", now=NOW)

        with self.assertRaises(ProbeConflict):
            self.store.arm("decline", now=LATER)

        self.assertEqual(self.store.current(), armed)
        self.assertIsNone(self.store.receipt("decline"))

    def test_refuses_different_request_replay_for_pending_case(self) -> None:
        """This catches a pending prompt being claimed by a different request."""

        self.store.arm("accept", now=NOW)
        first = self.store.claim_armed(request_digest="a" * 64, now=NOW)

        with self.assertRaises(ProbeConflict):
            self.store.claim_armed(request_digest="b" * 64, now=LATER)

        self.assertEqual(self.store.current(), first)

    def test_arm_moves_current_marker_without_changing_terminal_receipt(self) -> None:
        """This catches arming a new case mutating an earlier terminal receipt."""

        self.store.arm("accept", now=NOW)
        self.store.claim_armed(request_digest="a" * 64, now=NOW)
        accepted = self.store.complete("accept", state="accept", now=NOW)

        armed = self.store.arm("decline", now=LATER)

        self.assertEqual(self.store.receipt("accept"), accepted)
        self.assertEqual(self.store.current(), armed)

    def test_unavailable_finishes_armed_case_without_prompt(self) -> None:
        """This catches unavailable capability recording a displayed prompt."""

        self.store.arm("capability_unavailable", now=NOW)
        unavailable = self.store.mark_armed_unavailable(now=LATER)

        self.assertEqual(
            (unavailable.state, unavailable.prompt_count, unavailable.completion_count),
            ("unavailable", 0, 0),
        )
        self.assertEqual(self.store.current(), unavailable)

    def test_completion_requires_pending_and_cannot_repeat(self) -> None:
        """This catches completing before a claim or completing the same case twice."""

        self.store.arm("accept", now=NOW)
        with self.assertRaises(ProbeConflict):
            self.store.complete("accept", state="accept", now=NOW)

        self.store.claim_armed(request_digest="a" * 64, now=NOW)
        self.store.complete("accept", state="accept", now=LATER)
        with self.assertRaises(ProbeConflict):
            self.store.complete("accept", state="accept", now=LATER)

    def test_non_client_completion_states_do_not_count_as_actions(self) -> None:
        """This catches failure outcomes being counted as user completions."""

        for case_id, state in (
            ("accept", "unavailable"),
            ("decline", "failed"),
            ("cancel", "transport_lost"),
        ):
            self.store.arm(case_id, now=NOW)
            self.store.claim_armed(request_digest="d" * 64, now=NOW)
            receipt = self.store.complete(case_id, state=state, now=LATER)
            self.assertEqual(receipt.completion_count, 0)

    def test_recovered_transport_lost_case_cannot_later_accept(self) -> None:
        """This catches a recovered transport failure being treated as pending."""

        self.store.arm("restart", now=NOW)
        self.store.claim_armed(request_digest="c" * 64, now=NOW)
        self.store.recover_pending(now=LATER)

        with self.assertRaises(ProbeConflict):
            self.store.complete("restart", state="accept", now=LATER)


class RecallElicitationProtocolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "probe.sqlite3"
        self.store = ProbeReceiptStore.open(self.database_path)
        self.addCleanup(self.store.close)
        previous_logging_disable = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, previous_logging_disable)

    async def _call_server(self, elicitation_callback) -> dict[str, object]:
        server = build_probe_server(self.database_path)
        async with create_connected_server_and_client_session(
            server,
            elicitation_callback=elicitation_callback,
            raise_exceptions=True,
        ) as session:
            result = await session.call_tool("probe_zdecision_elicitation", {})
        return json.loads(result.content[0].text)

    async def _call_with_action(self, action: str) -> dict[str, object]:
        seen: list[types.ElicitRequestParams] = []

        async def elicitation_callback(context, params):
            seen.append(params)
            return types.ElicitResult(
                action=action,
                content={} if action == "accept" else None,
            )

        result = await self._call_server(elicitation_callback)
        self.assertEqual(len(seen), 1)
        return result

    def test_empty_confirmation_schema_is_closed_and_has_no_properties(self):
        schema = EmptyConfirmation.model_json_schema()
        self.assertEqual(schema["properties"], {})
        self.assertIs(schema["additionalProperties"], False)

    def test_capability_detection_requires_declared_form_support(self):
        form_context = SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(
                    capabilities=SimpleNamespace(
                        elicitation=SimpleNamespace(form=object())
                    )
                )
            )
        )
        url_only_context = SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(
                    capabilities=SimpleNamespace(
                        elicitation=SimpleNamespace(form=None)
                    )
                )
            )
        )
        absent_context = SimpleNamespace(
            session=SimpleNamespace(client_params=None)
        )

        self.assertTrue(supports_form_elicitation(form_context))
        self.assertFalse(supports_form_elicitation(url_only_context))
        self.assertFalse(supports_form_elicitation(absent_context))

    def test_request_digest_is_stable_bounded_and_domain_separated(self):
        first = request_digest("tool-request-17")

        self.assertRegex(first, r"[0-9a-f]{64}\Z")
        self.assertEqual(first, request_digest("tool-request-17"))
        self.assertNotEqual(first, request_digest("tool-request-18"))
        self.assertNotIn("tool-request-17", first)

    async def test_accept_decline_and_cancel_remain_distinct(self):
        for case_id, action in (("accept", "accept"), ("decline", "decline"), ("cancel", "cancel")):
            self.store.arm(case_id, now=NOW)
            response = await self._call_with_action(action)
            self.assertEqual(response["action"], action)
            self.assertEqual(response["authorized"], action == "accept")

    async def test_client_without_form_capability_returns_unavailable_without_eliciting(self):
        self.store.arm("capability_unavailable", now=NOW)
        server = build_probe_server(self.database_path)
        async with create_connected_server_and_client_session(
            server, raise_exceptions=True
        ) as session:
            result = await session.call_tool("probe_zdecision_elicitation", {})
        response = json.loads(result.content[0].text)
        self.assertEqual(response["action"], "unavailable")
        receipt = self.store.receipt("capability_unavailable")
        self.assertEqual((receipt.state, receipt.prompt_count), ("unavailable", 0))

    async def test_terminal_replay_returns_one_receipt_without_second_elicitation(self):
        self.store.arm("accept", now=NOW)
        calls = 0

        async def accept_callback(context, params):
            nonlocal calls
            calls += 1
            return types.ElicitResult(action="accept", content={})

        first = await self._call_server(accept_callback)
        first_digest = self.store.receipt("accept").request_digest
        replay = await self._call_server(accept_callback)
        self.assertEqual((first["action"], replay["action"]), ("accept", "accept"))
        self.assertEqual((first["replayed"], replay["replayed"]), (False, True))
        self.assertEqual(calls, 1)
        receipt = self.store.receipt("accept")
        self.assertEqual((receipt.prompt_count, receipt.completion_count), (1, 1))
        self.assertEqual(receipt.request_digest, first_digest)

    async def test_context_elicit_relates_response_to_originating_tool_request(self):
        session = SimpleNamespace(
            elicit_form=AsyncMock(
                return_value=types.ElicitResult(action="decline", content=None)
            )
        )
        request_context = RequestContext(
            request_id="tool-request-17",
            meta=None,
            session=session,
            lifespan_context=None,
        )
        context = Context(request_context=request_context)
        result = await context.elicit(
            message=ELICITATION_MESSAGE,
            schema=EmptyConfirmation,
        )
        self.assertEqual(result.action, "decline")
        self.assertEqual(
            session.elicit_form.await_args.kwargs["related_request_id"],
            "tool-request-17",
        )

    async def test_callback_receives_exact_message_and_closed_empty_schema(self):
        seen: list[types.ElicitRequestParams] = []

        async def decline_callback(context, params):
            seen.append(params)
            return types.ElicitResult(action="decline", content=None)

        self.store.arm("decline", now=NOW)
        response = await self._call_server(decline_callback)

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].message, ELICITATION_MESSAGE)
        self.assertEqual(seen[0].requestedSchema.get("properties"), {})
        self.assertIs(seen[0].requestedSchema.get("additionalProperties"), False)
        database = self.database_path.read_bytes()
        report = canonical_json_bytes(self.store.report())
        for retained_value in (
            ELICITATION_MESSAGE.encode(),
            b"additionalProperties",
        ):
            self.assertNotIn(retained_value, database)
            self.assertNotIn(retained_value, canonical_json_bytes(response))
            self.assertNotIn(retained_value, report)

    async def test_callback_exception_is_non_authorizing_and_sanitized(self):
        sentinel = "PRIVATE_ELICITATION_EXCEPTION_SENTINEL"

        async def failing_callback(context, params):
            raise RuntimeError(sentinel)

        self.store.arm("accept", now=NOW)
        response = await self._call_server(failing_callback)
        self.assertEqual(response["action"], "failed")
        self.assertFalse(response["authorized"])
        self.assertNotIn(sentinel, json.dumps(response))
        self.assertNotIn(sentinel.encode(), self.database_path.read_bytes())
        self.assertNotIn(sentinel.encode(), canonical_json_bytes(self.store.report()))

    async def test_protocol_failures_are_non_authorizing_and_sanitized(self):
        failure_cases = (
            ("PRIVATE_PROMPT_TIMEOUT_SENTINEL", TimeoutError),
            ("PRIVATE_SOURCE_EOF_SENTINEL", EOFError),
            (
                "PRIVATE_DIFF_MALFORMED_SENTINEL",
                lambda message: types.ElicitResult.model_construct(
                    action=message,
                    content=None,
                ),
            ),
            ("PRIVATE_DECISION_RUNTIME_SENTINEL", RuntimeError),
        )

        for sentinel, failure in failure_cases:
            with self.subTest(failure=sentinel), tempfile.TemporaryDirectory() as directory:
                database_path = Path(directory) / "probe.sqlite3"
                store = ProbeReceiptStore.open(database_path)
                try:
                    store.arm("accept", now=NOW)

                    async def failing_callback(context, params):
                        if isinstance(failure, type):
                            raise failure(sentinel)
                        return failure(sentinel)

                    server = build_probe_server(database_path)
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", UserWarning)
                        async with create_connected_server_and_client_session(
                            server,
                            elicitation_callback=failing_callback,
                            raise_exceptions=True,
                        ) as session:
                            result = await session.call_tool(
                                "probe_zdecision_elicitation", {}
                            )
                    response = json.loads(result.content[0].text)
                    receipt = store.receipt("accept")
                    report = canonical_json_bytes(store.report())

                    self.assertEqual(response["action"], "failed")
                    self.assertFalse(response["authorized"])
                    self.assertEqual(receipt.completion_count, 0)
                    self.assertNotIn(sentinel.encode(), database_path.read_bytes())
                    self.assertNotIn(sentinel, json.dumps(response))
                    self.assertNotIn(sentinel.encode(), report)
                finally:
                    store.close()

    async def test_cancellation_persists_failed_then_reraises(self):
        self.store.arm("accept", now=NOW)
        session = SimpleNamespace(
            client_params=SimpleNamespace(
                capabilities=SimpleNamespace(
                    elicitation=SimpleNamespace(form=object())
                )
            ),
            elicit_form=AsyncMock(side_effect=asyncio.CancelledError),
        )
        context = Context(
            request_context=RequestContext(
                request_id="cancelled-tool-request",
                meta=None,
                session=session,
                lifespan_context=None,
            )
        )

        with self.assertRaises(asyncio.CancelledError):
            await _run_probe(context=context, database_path=self.database_path)

        receipt = self.store.receipt("accept")
        self.assertEqual((receipt.state, receipt.completion_count), ("failed", 0))

    async def test_tool_schema_has_no_model_authored_fields(self):
        tools = await build_probe_server(self.database_path).list_tools()
        self.assertEqual(len(tools), 1)
        probe = next(item for item in tools if item.name == "probe_zdecision_elicitation")
        self.assertEqual(probe.inputSchema.get("properties"), {})
        self.assertEqual(probe.inputSchema.get("required", []), [])

    async def test_missing_current_case_fails_closed_without_prompt(self):
        calls = 0

        async def accept_callback(context, params):
            nonlocal calls
            calls += 1
            return types.ElicitResult(action="accept", content={})

        response = await self._call_server(accept_callback)

        self.assertEqual(response["action"], "unavailable")
        self.assertFalse(response["authorized"])
        self.assertEqual(calls, 0)

    async def test_pending_case_is_not_reprompted(self):
        self.store.arm("restart", now=NOW)
        pending = self.store.claim_armed(request_digest="c" * 64, now=NOW)
        calls = 0

        async def accept_callback(context, params):
            nonlocal calls
            calls += 1
            return types.ElicitResult(action="accept", content={})

        response = await self._call_server(accept_callback)

        self.assertEqual(response["action"], "pending")
        self.assertFalse(response["authorized"])
        self.assertEqual(response["replayed"], True)
        self.assertEqual(calls, 0)
        self.assertEqual(self.store.current(), pending)


if __name__ == "__main__":
    unittest.main()
