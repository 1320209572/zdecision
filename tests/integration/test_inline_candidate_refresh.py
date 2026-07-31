from __future__ import annotations

import json
import sqlite3
import unittest

from tests.integration import test_on_demand_capture_core as core
from tests.integration.test_on_demand_capture_core import (
    LOCAL_PATH_SENTINEL,
    RAW_PROMPT,
    RAW_SOURCE,
    SESSION_A,
    SESSION_B,
    SESSION_CHILD,
    TURN_A1,
    TURN_B1,
    TURN_CHILD,
)
from zdecision.agent.hooks import CONTROL_BINDING_TOOL, handle_hook
from zdecision.agent.mcp_server import LocalMcpTools


CONTROL_A = "ctl_" + "a" * 32
CONTROL_ALL = "ctl_" + "b" * 32
MODEL_CONTROL = "ctl_" + "c" * 32
RENDER_TURN_A = "render-turn-a-raw-sentinel"
RENDER_TURN_B = "render-turn-b-raw-sentinel"
RAW_DIFF = "DIFF-RAW-SENTINEL-DO-NOT-SYNC"
RAW_TOOL_OUTPUT = "TOOL-OUTPUT-RAW-SENTINEL-DO-NOT-SYNC"


class InlineCandidateRefreshIntegrationTest(unittest.TestCase):
    def test_inline_current_then_all_valid_is_exact_private_and_replayable(
        self,
    ) -> None:
        harness = core.OnDemandCaptureCoreTest(methodName="runTest")
        started = False
        try:
            harness.setUp()
            started = True
            harness._observe(
                SESSION_A,
                TURN_A1,
                harness.registered_repository,
                diff=RAW_DIFF,
                tool_output=RAW_TOOL_OUTPUT,
            )
            harness._observe(
                SESSION_B,
                TURN_B1,
                harness.registered_repository,
                diff=RAW_DIFF,
                tool_output=RAW_TOOL_OUTPUT,
            )
            harness._observe(
                SESSION_CHILD,
                TURN_CHILD,
                harness.registered_repository,
                agent_id="child-agent-raw-sentinel",
                diff=RAW_DIFF,
                tool_output=RAW_TOOL_OUTPUT,
            )
            harness._drain_hooks()

            current_control = self._render_control(
                harness, SESSION_A, RENDER_TURN_A, CONTROL_A
            )
            all_control = self._render_control(
                harness, SESSION_B, RENDER_TURN_B, CONTROL_ALL
            )
            action_ids = iter(
                ("codex_action_current", "codex_action_all_valid")
            )
            tools = LocalMcpTools(
                database=harness.agent_database,
                cwd=str(harness.registered_repository),
                binding_store=harness.control_store,
                central_client=harness.central_client,
                central_base_url="http://central.test",
                clock=harness.clock,
                action_id_factory=lambda: next(action_ids),
            )
            mcp_outputs: list[object] = []
            rendered = tools.show_zdecision_update(current_control)
            mcp_outputs.append(rendered.structuredContent)
            self.assertEqual(
                {"actions_enabled": True, "safe_state": "ready"},
                rendered.structuredContent,
            )

            started_current = tools.start_zdecision_candidate_refresh(
                current_control, "current_session"
            )
            mcp_outputs.append(started_current)
            self.assertEqual("queued", started_current["safe_state"])
            request_a = harness.control_store.get(
                current_control
            ).central_request_id
            self.assertIsNotNone(request_a)

            busy = tools.start_zdecision_candidate_refresh(
                all_control, "all_valid_sessions"
            )
            mcp_outputs.append(busy)
            self.assertEqual("busy", busy["safe_state"])
            self.assertIsNone(
                harness.control_store.get(all_control).central_request_id
            )

            self.assertTrue(harness._run_agent_once())
            status_a = tools.get_zdecision_candidate_refresh(current_control)
            mcp_outputs.append(status_a)
            self.assertEqual("succeeded", status_a["safe_state"])
            self.assertEqual(1, status_a["candidate_revision_count"])
            checkpoints = self._checkpoint_state(harness.agent_path)
            self.assertEqual(TURN_A1, checkpoints[SESSION_A][0])
            self.assertIsNone(checkpoints[SESSION_B][0])
            self.assertIsNone(checkpoints[SESSION_CHILD][0])

            request_count = self._table_count(
                harness.central_store.connection, "capture_requests"
            )
            revision_count = self._table_count(
                harness.central_store.connection, "candidate_revisions"
            )
            replay = tools.start_zdecision_candidate_refresh(
                current_control, "current_session"
            )
            mcp_outputs.append(replay)
            self.assertEqual(status_a, replay)
            self.assertEqual(
                request_count,
                self._table_count(
                    harness.central_store.connection, "capture_requests"
                ),
            )
            self.assertEqual(
                revision_count,
                self._table_count(
                    harness.central_store.connection, "candidate_revisions"
                ),
            )

            started_all = tools.start_zdecision_candidate_refresh(
                all_control, "all_valid_sessions"
            )
            mcp_outputs.append(started_all)
            self.assertEqual("queued", started_all["safe_state"])
            request_b = harness.control_store.get(
                all_control
            ).central_request_id
            self.assertIsNotNone(request_b)
            self.assertNotEqual(request_a, request_b)
            self.assertTrue(harness._run_agent_once())
            status_b = tools.get_zdecision_candidate_refresh(all_control)
            mcp_outputs.append(status_b)
            self.assertEqual("succeeded", status_b["safe_state"])
            self.assertEqual(1, status_b["candidate_revision_count"])

            checkpoints = self._checkpoint_state(harness.agent_path)
            self.assertEqual(TURN_A1, checkpoints[SESSION_A][0])
            self.assertEqual(TURN_B1, checkpoints[SESSION_B][0])
            self.assertEqual("subagent_session", checkpoints[SESSION_CHILD][1])
            self.assertEqual(
                [SESSION_A, SESSION_B], harness.gateway.source_reads
            )
            self.assertEqual(
                {"inventory": 2, "extraction": 2, "reconciliation": 2},
                harness.gateway.structured_turn_creates,
            )

            for request_id, status in (
                (request_a, status_a),
                (request_b, status_b),
            ):
                batch = harness.central_store.connection.execute(
                    "SELECT item_count FROM candidate_batches "
                    "WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                self.assertIsNotNone(batch)
                self.assertEqual(
                    batch["item_count"], status["candidate_revision_count"]
                )

            candidates = harness._candidates()
            self.assertEqual(2, len(candidates))
            candidate_text = json.dumps(
                candidates, ensure_ascii=False, sort_keys=True
            )
            mcp_text = json.dumps(
                mcp_outputs, ensure_ascii=False, sort_keys=True
            )
            for candidate in candidates:
                self.assertIn(candidate["revision_id"], candidate_text)
                self.assertNotIn(candidate["revision_id"], mcp_text)
                self.assertNotIn(candidate["content"]["claim"], mcp_text)

            event_payloads = []
            for request_id in (request_a, request_b):
                response = harness.browser.get(
                    f"/api/v1/capture-requests/{request_id}/events"
                )
                self.assertEqual(200, response.status_code, response.text)
                event_payloads.append(response.json())
                self.assertEqual(
                    "succeeded", response.json()["events"][-1]["state"]
                )

            paths = [path for path, _, _ in harness.bridge.records]
            self.assertIn("/api/v1/plugin/capture-requests", paths)
            self.assertIn("/api/v1/agent/capture-requests/claim", paths)
            self.assertTrue(any(path.endswith("/candidates") for path in paths))
            self.assertTrue(any(path.endswith("/complete") for path in paths))
            self.assertTrue(
                any("/api/v1/plugin/capture-requests/" in path for path in paths)
            )

            central_cells = self._all_database_cells(
                harness.central_store.connection
            )
            http_bodies = b"".join(
                request + response
                for _, request, response in harness.bridge.records
            )
            scanned = b"\n".join(
                (
                    central_cells,
                    http_bodies,
                    json.dumps(event_payloads, sort_keys=True).encode(),
                    mcp_text.encode("utf-8"),
                )
            )
            for forbidden in (
                SESSION_A,
                SESSION_B,
                SESSION_CHILD,
                TURN_A1,
                TURN_B1,
                TURN_CHILD,
                RENDER_TURN_A,
                RENDER_TURN_B,
                str(harness.registered_repository),
                LOCAL_PATH_SENTINEL,
                RAW_PROMPT,
                RAW_SOURCE,
                RAW_DIFF,
                RAW_TOOL_OUTPUT,
                CONTROL_A,
                CONTROL_ALL,
                MODEL_CONTROL,
            ):
                with self.subTest(forbidden=forbidden):
                    self.assertNotIn(forbidden.encode("utf-8"), scanned)
        finally:
            if started:
                harness.tearDown()
            harness.doCleanups()

    def _render_control(
        self,
        harness: core.OnDemandCaptureCoreTest,
        session_id: str,
        turn_id: str,
        control_id: str,
    ) -> str:
        response = handle_hook(
            {
                "hook_event_name": "PreToolUse",
                "session_id": session_id,
                "turn_id": turn_id,
                "cwd": str(harness.registered_repository),
                "tool_name": CONTROL_BINDING_TOOL,
                "tool_input": {
                    "control_id": MODEL_CONTROL,
                    "prompt": RAW_PROMPT,
                    "diff": RAW_DIFF,
                    "tool_output": RAW_TOOL_OUTPUT,
                },
            },
            database=harness.agent_database,
            clock=harness.clock,
            control_store=harness.control_store,
            control_id_factory=lambda: control_id,
            worker_waker=lambda _: self.fail("render hook must not wake worker"),
        )
        return response.output["hookSpecificOutput"]["updatedInput"][
            "control_id"
        ]

    @staticmethod
    def _checkpoint_state(path) -> dict[str, tuple[str | None, str | None]]:
        connection = sqlite3.connect(path)
        try:
            rows = connection.execute(
                "SELECT session_id, handled_turn_id, excluded_reason "
                "FROM session_checkpoints"
            ).fetchall()
        finally:
            connection.close()
        return {row[0]: (row[1], row[2]) for row in rows}

    @staticmethod
    def _table_count(connection, table: str) -> int:
        if table not in {"capture_requests", "candidate_revisions"}:
            raise AssertionError("unexpected table")
        return connection.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]

    @staticmethod
    def _all_database_cells(connection) -> bytes:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' ORDER BY name"
            ).fetchall()
            if not row[0].startswith("sqlite_")
        ]
        values: list[object] = []
        for table in tables:
            rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
            values.extend(dict(row) for row in rows)
        return json.dumps(
            values, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
