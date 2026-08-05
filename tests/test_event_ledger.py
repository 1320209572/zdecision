from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from zdecision.ids import product_id

try:
    from zdecision.agent.db import AgentDatabase, AgentEventConflict
    from zdecision.agent.events import HookInvocation, TestRepositoryMapping
    from zdecision.agent.hooks import handle_hook
    from zdecision.agent.mcp_server import LocalMcpTools
    from zdecision.agent.repository import RepositoryResolver
    from zdecision.central.decision_spaces import EnabledRepository
except ModuleNotFoundError as error:
    AGENT_IMPORT_ERROR: ModuleNotFoundError | None = error
else:
    AGENT_IMPORT_ERROR = None


FIXED_TIME = datetime(2026, 7, 30, 9, 30, tzinfo=UTC)
SECRET = "ZDECISION-SECRET-SENTINEL-91B7"


class EventLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNone(
            AGENT_IMPORT_ERROR,
            f"zdecision.agent runtime is missing: {AGENT_IMPORT_ERROR}",
        )
        self.temporary_directory = tempfile.TemporaryDirectory()
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
        self.database_path = self.root / "state" / "zdecision.sqlite3"
        self.database = AgentDatabase.open(self.database_path)
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
        self.database.put_enabled_repository(
            EnabledRepository(self.snapshot.repository_id, True)
        )

    def tearDown(self) -> None:
        if hasattr(self, "database"):
            self.database.close()
        if hasattr(self, "temporary_directory"):
            self.temporary_directory.cleanup()

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.repository,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _raw(
        self,
        event_name: str,
        *,
        turn_id: str | None = None,
        session_id: str = "thr_fixture",
        **extra: object,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "hook_event_name": event_name,
            "session_id": session_id,
            "cwd": str(self.repository),
        }
        if turn_id is not None:
            value["turn_id"] = turn_id
        value.update(extra)
        return value

    def _handle(self, raw: object):
        return handle_hook(
            raw,
            database=self.database,
            clock=lambda: FIXED_TIME,
            repository_resolver=self.repository_resolver,
            worker_waker=lambda _: None,
        )

    def test_all_five_hook_kinds_record_only_allowlisted_facts(self) -> None:
        hooks = (
            self._raw("SessionStart", source="startup"),
            self._raw("UserPromptSubmit", turn_id="turn_1", prompt=SECRET),
            self._raw(
                "PostToolUse",
                turn_id="turn_1",
                tool_name="Bash",
                tool_use_id="tool_1",
                tool_input={"command": "python -m unittest"},
                tool_response={"exit_code": 0, "output": SECRET},
            ),
            self._raw(
                "Stop",
                turn_id="turn_1",
                last_assistant_message=SECRET,
                stop_hook_active=False,
            ),
            self._raw("SessionEnd", reason="other", transcript_path=SECRET),
        )

        responses = tuple(self._handle(raw) for raw in hooks)
        events = self.database.list_events("thr_fixture")

        self.assertEqual(5, len({response.event_id for response in responses}))
        self.assertEqual(
            {
                "SessionStart",
                "UserPromptSubmit",
                "PostToolUse",
                "Stop",
                "SessionEnd",
            },
            {event.invocation.event_name for event in events},
        )
        post_tool = next(
            event
            for event in events
            if event.invocation.event_name == "PostToolUse"
        )
        self.assertEqual("Bash", post_tool.invocation.tool_name)
        self.assertEqual(
            {"classification": "validation", "exit_status": 0},
            post_tool.invocation.safe_fact,
        )
        self.assertLess(len(json.dumps(post_tool.invocation.safe_fact)), 256)

    def test_duplicate_delivery_keeps_first_time_and_never_persists_raw_text(
        self,
    ) -> None:
        raw = self._raw(
            "UserPromptSubmit",
            turn_id="turn_private",
            prompt=SECRET,
            transcript_path=f"/tmp/{SECRET}.jsonl",
            unknown={"nested": SECRET},
        )

        first = self._handle(raw)
        second = handle_hook(
            raw,
            database=self.database,
            clock=lambda: datetime(2026, 7, 30, 10, 30, tzinfo=UTC),
            repository_resolver=self.repository_resolver,
            worker_waker=lambda _: None,
        )

        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(1, self.database.count_events())
        stored = self.database.get_event(first.event_id)
        self.assertEqual("2026-07-30T09:30:00Z", stored.invocation.occurred_at)
        for path in self.database_path.parent.iterdir():
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes())

    def test_invalid_or_corrupt_input_is_bounded_and_creates_no_event(self) -> None:
        invalid_values = (
            "{not-json",
            {},
            self._raw("Unknown", turn_id="turn_1"),
            self._raw("UserPromptSubmit"),
            self._raw("Stop", turn_id="turn with whitespace"),
        )

        for raw in invalid_values:
            with self.subTest(raw=raw):
                response = self._handle(raw)
                self.assertEqual("", response.event_id)
                self.assertEqual(
                    {"systemMessage": "ZDecision ignored an invalid hook event."},
                    response.output,
                )
        self.assertEqual(0, self.database.count_events())

    def test_unregistered_repository_is_a_silent_no_op(self) -> None:
        self.database.put_enabled_repository(
            EnabledRepository(self.snapshot.repository_id, False)
        )

        response = self._handle(
            self._raw("UserPromptSubmit", turn_id="turn_unregistered", prompt=SECRET)
        )

        self.assertEqual("", response.event_id)
        self.assertEqual({}, response.output)
        self.assertEqual(0, self.database.count_events())

    def test_replay_with_same_id_but_different_canonical_fields_conflicts(self) -> None:
        raw = self._raw("UserPromptSubmit", turn_id="turn_conflict")
        invocation = HookInvocation.from_dict(
            raw,
            occurred_at="2026-07-30T09:30:00Z",
        )
        self.database.record_hook(invocation)

        with self.assertRaises(AgentEventConflict):
            self.database.record_hook(replace(invocation, cwd="/different/path"))

    def test_open_boundary_is_none_when_two_sessions_are_active(self) -> None:
        self._handle(self._raw("UserPromptSubmit", turn_id="turn_a", session_id="thr_a"))
        self._handle(self._raw("UserPromptSubmit", turn_id="turn_b", session_id="thr_b"))

        self.assertIsNone(self.database.latest_open_boundary(str(self.repository)))

        self._handle(self._raw("SessionEnd", session_id="thr_b", reason="other"))
        self.assertEqual(
            ("thr_a", "turn_a"),
            self.database.latest_open_boundary(str(self.repository)),
        )

    def test_resumed_session_reopens_status_turn_binding(self) -> None:
        self._handle(
            self._raw(
                "UserPromptSubmit",
                turn_id="turn_before_end",
                session_id="thr_resumed",
            )
        )
        self._handle(
            self._raw("SessionEnd", session_id="thr_resumed", reason="other")
        )
        self.assertIsNone(self.database.latest_open_boundary(str(self.repository)))

        self._handle(
            self._raw("SessionStart", session_id="thr_resumed", source="resume")
        )
        self._handle(
            self._raw(
                "UserPromptSubmit",
                turn_id="turn_after_resume",
                session_id="thr_resumed",
            )
        )
        tools = LocalMcpTools(
            database=self.database,
            cwd=str(self.repository),
        )

        status = tools.zdecision_status()

        self.assertTrue(status["active_session_bound"])

    def test_local_mcp_status_does_not_store_tool_text(self) -> None:
        self._handle(self._raw("SessionStart", session_id="thr_tools", source="startup"))
        self._handle(
            self._raw(
                "PostToolUse",
                session_id="thr_tools",
                turn_id="turn_tools",
                tool_name="Bash",
                tool_use_id="tool_validation",
                tool_input={"command": f"python -m unittest {SECRET}"},
                tool_response={"exit_code": 0},
            )
        )
        tools = LocalMcpTools(
            database=self.database,
            cwd=str(self.repository),
        )

        status = tools.zdecision_status()

        self.assertEqual(True, status["repository_registered"])
        self.assertEqual(True, status["repository_enabled"])
        self.assertEqual(True, status["active_session_bound"])
        self.assertEqual(2, status["event_count"])
        for path in self.database_path.parent.iterdir():
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes())

    def test_local_mcp_status_has_no_capture_side_effect(self) -> None:
        tools = LocalMcpTools(
            database=self.database,
            cwd=str(self.repository),
        )

        result = tools.zdecision_status()

        self.assertFalse(result["active_session_bound"])
        self.assertEqual(0, self.database.count_events())

    def test_legacy_capture_tables_retire_only_after_new_stores_exist(self) -> None:
        from zdecision.agent.capture_operation_store import (
            CaptureOperationStore,
        )
        from zdecision.agent.request_state import RequestStateStore
        from zdecision.agent.session_index import SessionIndex

        self._handle(
            self._raw(
                "Stop",
                session_id="thr_migration",
                turn_id="turn_migration",
            )
        )
        self.database.close()
        connection = sqlite3.connect(self.database_path)
        with connection:
            connection.executescript(
                """
                CREATE TABLE automated_capture_runs (
                    automated_capture_id TEXT PRIMARY KEY
                );
                CREATE INDEX automated_capture_boundary
                    ON automated_capture_runs(automated_capture_id);
                CREATE TABLE boundary_assessments (
                    automated_capture_id TEXT PRIMARY KEY
                );
                """
            )
        connection.close()

        self.database = AgentDatabase.open(self.database_path)
        self.assertEqual(
            {"automated_capture_runs", "boundary_assessments"},
            self._legacy_table_names(),
        )
        self.database.close()

        session_index = SessionIndex.open(self.database_path)
        operation_store = CaptureOperationStore.open(self.database_path)
        request_state = RequestStateStore.open(self.database_path)
        session_index.close()
        operation_store.close()
        request_state.close()

        self.database = AgentDatabase.open(self.database_path)
        self.assertEqual(set(), self._legacy_table_names())
        self.assertEqual(1, self.database.count_events())
        self.assertEqual(
            self.mapping,
            self.database.get_repository_mapping(self.mapping.repository_id),
        )

    def _legacy_table_names(self) -> set[str]:
        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                  AND name IN ('automated_capture_runs', 'boundary_assessments')
                """
            ).fetchall()
            return {row[0] for row in rows}
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
