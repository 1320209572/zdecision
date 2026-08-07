from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from zdecision.agent.events import (
    AgentEvent,
    HookInvocation,
    RepositorySnapshot,
    event_id_for,
)
from zdecision.app_server.models import FeasibilityModelProfile

try:
    from zdecision.agent.session_index import (
        RequestModelProfileConflict,
        SessionIndex,
    )
except ModuleNotFoundError as error:
    SESSION_INDEX_IMPORT_ERROR: ModuleNotFoundError | None = error
else:
    SESSION_INDEX_IMPORT_ERROR = None


REPOSITORY_ID = "repo_" + "a" * 32
OTHER_REPOSITORY_ID = "repo_" + "b" * 32
FIRST_REQUEST_ID = "crq_" + "1" * 32
SECOND_REQUEST_ID = "crq_" + "2" * 32
NOW = datetime(2026, 7, 30, 9, 30, tzinfo=UTC)


def model_profile(model_id: str, digest: str) -> FeasibilityModelProfile:
    return FeasibilityModelProfile.create(
        model_id=model_id,
        reasoning_effort="medium",
        discovery_digest=digest,
        discovered_at="2026-08-03T02:00:00.000000Z",
    )


def observed_event(
    event_name: str,
    session_id: str,
    turn_id: str,
    *,
    observed: str = "2026-07-30T01:00:00+00:00",
    repository_id: str | None = REPOSITORY_ID,
    worktree_root: str | None = "/workspace/product",
    branch: str | None = "main",
    head_commit: str | None = "b" * 40,
) -> AgentEvent:
    repository = None
    if repository_id is not None and worktree_root is not None:
        repository = RepositorySnapshot(
            repository_id=repository_id,
            worktree_root=worktree_root,
            branch=branch,
            head_commit=head_commit,  # type: ignore[arg-type]
        )
    invocation = HookInvocation.from_dict(
        {
            "hook_event_name": event_name,
            "session_id": session_id,
            "turn_id": turn_id,
            "cwd": worktree_root or "/workspace/unbound",
        },
        occurred_at=observed,
        repository=repository,
    )
    return AgentEvent(
        event_id=event_id_for(invocation),
        invocation=invocation,
        state="recorded",
        failure_code=None,
    )


class SessionIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNone(
            SESSION_INDEX_IMPORT_ERROR,
            f"zdecision.agent.session_index is missing: {SESSION_INDEX_IMPORT_ERROR}",
        )
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "state" / "zdecision.sqlite3"
        )
        self.index = SessionIndex.open(self.database_path)

    def tearDown(self) -> None:
        if hasattr(self, "index"):
            self.index.close()
        if hasattr(self, "temporary_directory"):
            self.temporary_directory.cleanup()

    def append_ledger_events(self, *events: AgentEvent) -> None:
        self.index._connection.execute(
            "CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY)"
        )
        self.index._connection.executemany(
            "INSERT INTO events(event_id) VALUES (?)",
            tuple((event.event_id,) for event in events),
        )
        self.index._connection.commit()

    def test_freeze_keeps_later_activity_for_the_next_request(self) -> None:
        self.index.observe(
            observed_event(
                "Stop",
                "session_a",
                "turn_1",
                observed="2026-07-30T01:00:00+00:00",
            )
        )
        first = self.index.freeze_sources(
            FIRST_REQUEST_ID,
            REPOSITORY_ID,
            NOW,
            capture_scope="all_valid_sessions",
        )

        self.index.observe(
            observed_event(
                "Stop",
                "session_a",
                "turn_2",
                observed="2026-07-30T01:01:00+00:00",
            )
        )
        self.index.acknowledge(FIRST_REQUEST_ID, "a" * 64, NOW)
        second = self.index.freeze_sources(
            SECOND_REQUEST_ID,
            REPOSITORY_ID,
            NOW,
            capture_scope="all_valid_sessions",
        )

        self.assertEqual(["turn_1"], [item.upper_turn_id for item in first])
        self.assertEqual(["turn_2"], [item.upper_turn_id for item in second])
        self.assertEqual("turn_1", second[0].previous_handled_turn_id)
        self.assertEqual(first[0].upper_stop_event_id, second[0].previous_handled_event_id)

    def test_freeze_and_acknowledgement_preserve_event_boundaries(self) -> None:
        """This catches a replay recomputing event boundaries from later activity."""

        first_stop = observed_event("Stop", "session_a", "turn_1")
        self.index.observe(first_stop)
        frozen = self.index.freeze_sources(
            FIRST_REQUEST_ID, REPOSITORY_ID, NOW, capture_scope="all_valid_sessions"
        )
        self.index.acknowledge(FIRST_REQUEST_ID, "a" * 64, NOW)
        second_stop = observed_event(
            "Stop", "session_a", "turn_2", observed="2026-07-30T01:01:00+00:00"
        )
        self.index.observe(second_stop)
        next_sources = self.index.freeze_sources(
            SECOND_REQUEST_ID, REPOSITORY_ID, NOW, capture_scope="all_valid_sessions"
        )

        self.assertEqual(first_stop.event_id, frozen[0].upper_stop_event_id)
        self.assertIsNone(frozen[0].previous_handled_event_id)
        self.assertEqual(first_stop.event_id, next_sources[0].previous_handled_event_id)
        self.assertEqual(second_stop.event_id, next_sources[0].upper_stop_event_id)

    def test_old_boundary_tables_open_with_null_event_boundaries(self) -> None:
        """This catches migration inferring event boundaries from legacy Turn state."""

        self.index.close()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("DROP TABLE session_checkpoints")
            connection.execute("DROP TABLE capture_request_sources")
            connection.execute(
                """
                CREATE TABLE session_checkpoints (
                    source_key TEXT PRIMARY KEY, repository_id TEXT NOT NULL,
                    session_id TEXT NOT NULL, cwd TEXT NOT NULL, lineage TEXT NOT NULL,
                    latest_turn_id TEXT NOT NULL, latest_event_id TEXT NOT NULL,
                    latest_observed_at TEXT NOT NULL,
                    latest_source_fingerprint TEXT NOT NULL, handled_turn_id TEXT,
                    handled_source_fingerprint TEXT, latest_head_commit TEXT,
                    handled_head_commit TEXT, excluded_reason TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE capture_request_sources (
                    request_id TEXT NOT NULL, source_key TEXT NOT NULL,
                    repository_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    cwd TEXT NOT NULL, lineage TEXT NOT NULL,
                    previous_handled_turn_id TEXT, upper_turn_id TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    previous_handled_head_commit TEXT, upper_head_commit TEXT,
                    state TEXT NOT NULL, excluded_reason TEXT, frozen_at TEXT NOT NULL,
                    acknowledged_at TEXT, acknowledgement_digest TEXT,
                    PRIMARY KEY(request_id, source_key)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO session_checkpoints VALUES (
                    'src_old', ?, 'session_old', '/workspace/product', 'lin_old',
                    'turn_handled', 'evt_latest', '2026-07-30T01:00:00Z',
                    ?, 'turn_handled', ?, NULL, NULL, NULL
                )
                """,
                (REPOSITORY_ID, "a" * 64, "b" * 64),
            )
            connection.execute(
                """
                INSERT INTO capture_request_sources VALUES (
                    ?, 'src_old', ?, 'session_old', '/workspace/product', 'lin_old',
                    'turn_handled', 'turn_upper', ?, NULL, NULL, 'frozen', NULL,
                    ?, NULL, NULL
                )
                """,
                (FIRST_REQUEST_ID, REPOSITORY_ID, "a" * 64, NOW.isoformat()),
            )
            connection.commit()

        self.index = SessionIndex.open(self.database_path)
        checkpoint = self.index._connection.execute(
            "SELECT handled_event_id FROM session_checkpoints WHERE source_key = 'src_old'"
        ).fetchone()
        source = self.index._connection.execute(
            """SELECT previous_handled_event_id, upper_stop_event_id
               FROM capture_request_sources WHERE request_id = ?""",
            (FIRST_REQUEST_ID,),
        ).fetchone()

        self.assertIsNone(checkpoint["handled_event_id"])
        self.assertIsNone(source["previous_handled_event_id"])
        self.assertIsNone(source["upper_stop_event_id"])

    def test_failed_request_does_not_advance_handled_checkpoint(self) -> None:
        self.index.observe(observed_event("Stop", "session_a", "turn_1"))

        first = self.index.freeze_sources(
            FIRST_REQUEST_ID, REPOSITORY_ID, NOW,
            capture_scope="all_valid_sessions",
        )
        replay = self.index.freeze_sources(
            FIRST_REQUEST_ID, REPOSITORY_ID, NOW,
            capture_scope="all_valid_sessions",
        )
        next_request = self.index.freeze_sources(
            SECOND_REQUEST_ID, REPOSITORY_ID, NOW,
            capture_scope="all_valid_sessions",
        )

        self.assertEqual(first, replay)
        self.assertEqual("turn_1", next_request[0].upper_turn_id)
        self.assertIsNone(next_request[0].previous_handled_turn_id)

    def test_empty_snapshot_replays_empty_after_later_activity(self) -> None:
        first = self.index.freeze_sources(
            FIRST_REQUEST_ID, REPOSITORY_ID, NOW,
            capture_scope="all_valid_sessions",
        )
        self.index.observe(observed_event("Stop", "session_a", "turn_1"))

        replay = self.index.freeze_sources(
            FIRST_REQUEST_ID, REPOSITORY_ID, NOW,
            capture_scope="all_valid_sessions",
        )
        next_request = self.index.freeze_sources(
            SECOND_REQUEST_ID, REPOSITORY_ID, NOW,
            capture_scope="all_valid_sessions",
        )

        self.assertEqual((), first)
        self.assertEqual((), replay)
        self.assertEqual(["turn_1"], [item.upper_turn_id for item in next_request])

    def test_ledger_append_order_controls_equal_and_nonmonotonic_stop_timestamps(
        self,
    ) -> None:
        """This catches Stop boundaries ordered by timestamp or event-ID text."""

        equal_first = observed_event("Stop", "session_equal", "turn_1")
        equal_first = AgentEvent(
            "evt_f" + "9" * 31,
            equal_first.invocation,
            equal_first.state,
            equal_first.failure_code,
        )
        equal_second = observed_event("Stop", "session_equal", "turn_2")
        equal_second = AgentEvent(
            "evt_e" + "5" * 31,
            equal_second.invocation,
            equal_second.state,
            equal_second.failure_code,
        )
        older = observed_event(
            "Stop", "session_nonmonotonic", "turn_1",
            observed="2026-07-30T01:00:00+00:00",
        )
        newer = observed_event(
            "Stop", "session_nonmonotonic", "turn_2",
            observed="2026-07-30T01:01:00+00:00",
        )
        self.append_ledger_events(equal_first, equal_second, older, newer)

        self.index.observe(equal_first)
        self.index.observe(equal_second)
        self.index.observe(newer)
        self.index.observe(older)

        frozen = self.index.freeze_sources(
            FIRST_REQUEST_ID, REPOSITORY_ID, NOW,
            capture_scope="all_valid_sessions",
        )

        by_session = {source.session_id: source for source in frozen}
        self.assertEqual("turn_2", by_session["session_equal"].upper_turn_id)
        self.assertEqual(equal_second.event_id, by_session["session_equal"].upper_stop_event_id)
        self.assertEqual("turn_2", by_session["session_nonmonotonic"].upper_turn_id)
        self.assertEqual(newer.event_id, by_session["session_nonmonotonic"].upper_stop_event_id)

    def test_only_repository_bound_stop_events_are_indexed(self) -> None:
        self.index.observe(observed_event("UserPromptSubmit", "session_a", "turn_1"))
        self.index.observe(
            observed_event(
                "Stop",
                "session_unbound",
                "turn_1",
                repository_id=None,
                worktree_root=None,
            )
        )

        frozen = self.index.freeze_sources(
            FIRST_REQUEST_ID, REPOSITORY_ID, NOW,
            capture_scope="all_valid_sessions",
        )

        self.assertEqual((), frozen)

    def test_stop_without_branch_or_commit_still_freezes(self) -> None:
        self.index.observe(
            observed_event(
                "Stop",
                "session_a",
                "turn_1",
                branch=None,
                head_commit=None,
            )
        )

        frozen = self.index.freeze_sources(
            FIRST_REQUEST_ID, REPOSITORY_ID, NOW,
            capture_scope="all_valid_sessions",
        )

        self.assertEqual(1, len(frozen))
        self.assertEqual("turn_1", frozen[0].upper_turn_id)

    def test_excluded_source_is_removed_from_replay_and_future_requests(self) -> None:
        self.index.observe(observed_event("Stop", "session_a", "turn_1"))
        self.index.observe(observed_event("Stop", "session_b", "turn_1"))
        frozen = self.index.freeze_sources(
            FIRST_REQUEST_ID, REPOSITORY_ID, NOW,
            capture_scope="all_valid_sessions",
        )
        excluded = next(item for item in frozen if item.session_id == "session_a")

        self.index.mark_excluded(
            FIRST_REQUEST_ID, excluded.source_key, "subagent_session"
        )
        replay = self.index.freeze_sources(
            FIRST_REQUEST_ID, REPOSITORY_ID, NOW,
            capture_scope="all_valid_sessions",
        )
        self.index.acknowledge(FIRST_REQUEST_ID, "a" * 64, NOW)
        next_request = self.index.freeze_sources(
            SECOND_REQUEST_ID, REPOSITORY_ID, NOW,
            capture_scope="all_valid_sessions",
        )

        self.assertEqual(["session_b"], [item.session_id for item in replay])
        self.assertEqual((), next_request)

    def test_observation_and_frozen_snapshot_survive_restart(self) -> None:
        self.index.observe(observed_event("Stop", "session_a", "turn_1"))
        self.index.close()
        self.index = SessionIndex.open(self.database_path)

        first = self.index.freeze_sources(
            FIRST_REQUEST_ID, REPOSITORY_ID, NOW,
            capture_scope="all_valid_sessions",
        )
        self.index.close()
        self.index = SessionIndex.open(self.database_path)
        replay = self.index.freeze_sources(
            FIRST_REQUEST_ID, REPOSITORY_ID, NOW,
            capture_scope="all_valid_sessions",
        )

        self.assertEqual(first, replay)

    def test_scope_selects_current_session_or_all_remaining_sessions(self) -> None:
        self.index.observe(observed_event("Stop", "session_a", "turn_a"))
        self.index.observe(observed_event("Stop", "session_b", "turn_b"))

        current = self.index.freeze_sources(
            FIRST_REQUEST_ID,
            REPOSITORY_ID,
            NOW,
            capture_scope="current_session",
            selected_session_id="session_a",
        )
        self.index.acknowledge(FIRST_REQUEST_ID, "a" * 64, NOW)
        remaining = self.index.freeze_sources(
            SECOND_REQUEST_ID,
            REPOSITORY_ID,
            NOW,
            capture_scope="all_valid_sessions",
        )

        self.assertEqual(["session_a"], [item.session_id for item in current])
        self.assertEqual(["session_b"], [item.session_id for item in remaining])

    def test_scope_and_selected_session_arguments_must_agree(self) -> None:
        with self.assertRaises(ValueError):
            self.index.freeze_sources(
                FIRST_REQUEST_ID,
                REPOSITORY_ID,
                NOW,
                capture_scope="current_session",
            )
        with self.assertRaises(ValueError):
            self.index.freeze_sources(
                FIRST_REQUEST_ID,
                REPOSITORY_ID,
                NOW,
                capture_scope="all_valid_sessions",
                selected_session_id="session_a",
            )

    def test_current_session_freezes_only_its_newest_changed_lineage(self) -> None:
        self.index.observe(
            observed_event(
                "Stop", "session_a", "turn_old",
                observed="2026-07-30T01:00:00+00:00",
                branch="main",
            )
        )
        self.index.observe(
            observed_event(
                "Stop", "session_a", "turn_new",
                observed="2026-07-30T01:01:00+00:00",
                branch="feature",
            )
        )

        frozen = self.index.freeze_sources(
            FIRST_REQUEST_ID,
            REPOSITORY_ID,
            NOW,
            capture_scope="current_session",
            selected_session_id="session_a",
        )

        self.assertEqual(1, len(frozen))
        self.assertEqual("turn_new", frozen[0].upper_turn_id)

    def test_current_session_selects_latest_cross_lineage_ledger_stop(self) -> None:
        """This catches current-session lineage selection by time or event-ID text."""

        older_ledger_stop = observed_event(
            "Stop", "session_a", "turn_main",
            observed="2026-07-30T01:02:00+00:00", branch="main",
        )
        older_ledger_stop = AgentEvent(
            "evt_f" + "8" * 31,
            older_ledger_stop.invocation,
            older_ledger_stop.state,
            older_ledger_stop.failure_code,
        )
        later_ledger_stop = observed_event(
            "Stop", "session_a", "turn_feature",
            observed="2026-07-30T01:01:00+00:00", branch="feature",
        )
        later_ledger_stop = AgentEvent(
            "evt_e" + "4" * 31,
            later_ledger_stop.invocation,
            later_ledger_stop.state,
            later_ledger_stop.failure_code,
        )
        self.append_ledger_events(older_ledger_stop, later_ledger_stop)
        self.index.observe(older_ledger_stop)
        self.index.observe(later_ledger_stop)

        frozen = self.index.freeze_sources(
            FIRST_REQUEST_ID,
            REPOSITORY_ID,
            NOW,
            capture_scope="current_session",
            selected_session_id="session_a",
        )

        self.assertEqual(1, len(frozen))
        self.assertEqual("turn_feature", frozen[0].upper_turn_id)
        self.assertEqual(later_ledger_stop.event_id, frozen[0].upper_stop_event_id)

    def test_current_session_is_empty_when_only_another_session_changed(self) -> None:
        self.index.observe(observed_event("Stop", "session_b", "turn_b"))

        frozen = self.index.freeze_sources(
            FIRST_REQUEST_ID,
            REPOSITORY_ID,
            NOW,
            capture_scope="current_session",
            selected_session_id="session_a",
        )

        self.assertEqual((), frozen)

    def test_freeze_replay_rejects_repository_scope_or_session_mismatch(self) -> None:
        self.index.freeze_sources(
            FIRST_REQUEST_ID,
            REPOSITORY_ID,
            NOW,
            capture_scope="current_session",
            selected_session_id="session_a",
        )
        mismatches = (
            (OTHER_REPOSITORY_ID, "current_session", "session_a"),
            (REPOSITORY_ID, "all_valid_sessions", None),
            (REPOSITORY_ID, "current_session", "session_b"),
        )
        for repository_id, capture_scope, selected_session_id in mismatches:
            with self.subTest(
                repository_id=repository_id,
                capture_scope=capture_scope,
                selected_session_id=selected_session_id,
            ), self.assertRaises(ValueError):
                self.index.freeze_sources(
                    FIRST_REQUEST_ID,
                    repository_id,
                    NOW,
                    capture_scope=capture_scope,
                    selected_session_id=selected_session_id,
                )

    def test_request_profile_freezes_once_and_survives_restart(self) -> None:
        self.index.freeze_sources(
            FIRST_REQUEST_ID,
            REPOSITORY_ID,
            NOW,
            capture_scope="all_valid_sessions",
        )
        profile = model_profile("model-a", "a" * 64)

        stored = self.index.freeze_request_model_profile(
            FIRST_REQUEST_ID, profile
        )
        self.index.close()
        self.index = SessionIndex.open(self.database_path)

        self.assertEqual(profile, stored)
        self.assertEqual(
            profile,
            self.index.request_model_profile(FIRST_REQUEST_ID),
        )

    def test_request_profile_replay_rejects_a_different_profile(self) -> None:
        self.index.freeze_sources(
            FIRST_REQUEST_ID,
            REPOSITORY_ID,
            NOW,
            capture_scope="all_valid_sessions",
        )
        self.index.freeze_request_model_profile(
            FIRST_REQUEST_ID, model_profile("model-a", "a" * 64)
        )

        with self.assertRaisesRegex(
            RequestModelProfileConflict, "profile conflicts"
        ):
            self.index.freeze_request_model_profile(
                FIRST_REQUEST_ID,
                model_profile("model-b", "b" * 64),
            )

    def test_old_request_freeze_migrates_with_null_profile(self) -> None:
        self.index.close()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("DROP TABLE capture_request_freezes")
            connection.execute(
                """
                CREATE TABLE capture_request_freezes (
                    request_id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    capture_scope TEXT NOT NULL,
                    selected_session_id TEXT,
                    frozen_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    acknowledgement_digest TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO capture_request_freezes(
                    request_id, repository_id, capture_scope,
                    selected_session_id, frozen_at,
                    acknowledged_at, acknowledgement_digest
                ) VALUES (?, ?, 'all_valid_sessions', NULL, ?, NULL, NULL)
                """,
                (FIRST_REQUEST_ID, REPOSITORY_ID, NOW.isoformat()),
            )
            connection.commit()

        self.index = SessionIndex.open(self.database_path)

        self.assertIsNone(
            self.index.request_model_profile(FIRST_REQUEST_ID)
        )

    def test_profile_cannot_be_stored_for_an_unknown_request(self) -> None:
        with self.assertRaisesRegex(
            RequestModelProfileConflict, "has not been frozen"
        ):
            self.index.freeze_request_model_profile(
                FIRST_REQUEST_ID,
                model_profile("model-a", "a" * 64),
            )

    def test_open_migrates_old_freezes_to_all_valid_scope(self) -> None:
        self.index.close()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("DROP TABLE capture_request_freezes")
            connection.execute(
                """
                CREATE TABLE capture_request_freezes (
                    request_id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    frozen_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    acknowledgement_digest TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO capture_request_freezes(
                    request_id, repository_id, frozen_at,
                    acknowledged_at, acknowledgement_digest
                ) VALUES (?, ?, ?, NULL, NULL)
                """,
                (FIRST_REQUEST_ID, REPOSITORY_ID, NOW.isoformat()),
            )
        self.index = SessionIndex.open(self.database_path)

        replay = self.index.freeze_sources(
            FIRST_REQUEST_ID,
            REPOSITORY_ID,
            NOW,
            capture_scope="all_valid_sessions",
        )

        self.assertEqual((), replay)
        with self.assertRaises(ValueError):
            self.index.freeze_sources(
                FIRST_REQUEST_ID,
                REPOSITORY_ID,
                NOW,
                capture_scope="current_session",
                selected_session_id="session_a",
            )


if __name__ == "__main__":
    unittest.main()
