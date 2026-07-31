from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from zdecision.agent.events import (
    AgentEvent,
    HookInvocation,
    RepositorySnapshot,
    event_id_for,
)

try:
    from zdecision.agent.session_index import SessionIndex
except ModuleNotFoundError as error:
    SESSION_INDEX_IMPORT_ERROR: ModuleNotFoundError | None = error
else:
    SESSION_INDEX_IMPORT_ERROR = None


REPOSITORY_ID = "repo_" + "a" * 32
FIRST_REQUEST_ID = "crq_" + "1" * 32
SECOND_REQUEST_ID = "crq_" + "2" * 32
NOW = datetime(2026, 7, 30, 9, 30, tzinfo=UTC)


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

    def test_freeze_keeps_later_activity_for_the_next_request(self) -> None:
        self.index.observe(
            observed_event(
                "Stop",
                "session_a",
                "turn_1",
                observed="2026-07-30T01:00:00+00:00",
            )
        )
        first = self.index.freeze_sources(FIRST_REQUEST_ID, REPOSITORY_ID, NOW)

        self.index.observe(
            observed_event(
                "Stop",
                "session_a",
                "turn_2",
                observed="2026-07-30T01:01:00+00:00",
            )
        )
        self.index.acknowledge(FIRST_REQUEST_ID, "a" * 64, NOW)
        second = self.index.freeze_sources(SECOND_REQUEST_ID, REPOSITORY_ID, NOW)

        self.assertEqual(["turn_1"], [item.upper_turn_id for item in first])
        self.assertEqual(["turn_2"], [item.upper_turn_id for item in second])
        self.assertEqual("turn_1", second[0].previous_handled_turn_id)

    def test_failed_request_does_not_advance_handled_checkpoint(self) -> None:
        self.index.observe(observed_event("Stop", "session_a", "turn_1"))

        first = self.index.freeze_sources(FIRST_REQUEST_ID, REPOSITORY_ID, NOW)
        replay = self.index.freeze_sources(FIRST_REQUEST_ID, REPOSITORY_ID, NOW)
        next_request = self.index.freeze_sources(
            SECOND_REQUEST_ID, REPOSITORY_ID, NOW
        )

        self.assertEqual(first, replay)
        self.assertEqual("turn_1", next_request[0].upper_turn_id)
        self.assertIsNone(next_request[0].previous_handled_turn_id)

    def test_empty_snapshot_replays_empty_after_later_activity(self) -> None:
        first = self.index.freeze_sources(FIRST_REQUEST_ID, REPOSITORY_ID, NOW)
        self.index.observe(observed_event("Stop", "session_a", "turn_1"))

        replay = self.index.freeze_sources(FIRST_REQUEST_ID, REPOSITORY_ID, NOW)
        next_request = self.index.freeze_sources(
            SECOND_REQUEST_ID, REPOSITORY_ID, NOW
        )

        self.assertEqual((), first)
        self.assertEqual((), replay)
        self.assertEqual(["turn_1"], [item.upper_turn_id for item in next_request])

    def test_out_of_order_stop_does_not_regress_latest_turn(self) -> None:
        self.index.observe(
            observed_event(
                "Stop",
                "session_a",
                "turn_2",
                observed="2026-07-30T01:01:00+00:00",
            )
        )
        self.index.observe(
            observed_event(
                "Stop",
                "session_a",
                "turn_1",
                observed="2026-07-30T01:00:00+00:00",
            )
        )

        frozen = self.index.freeze_sources(FIRST_REQUEST_ID, REPOSITORY_ID, NOW)

        self.assertEqual("turn_2", frozen[0].upper_turn_id)

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

        frozen = self.index.freeze_sources(FIRST_REQUEST_ID, REPOSITORY_ID, NOW)

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

        frozen = self.index.freeze_sources(FIRST_REQUEST_ID, REPOSITORY_ID, NOW)

        self.assertEqual(1, len(frozen))
        self.assertEqual("turn_1", frozen[0].upper_turn_id)

    def test_excluded_source_is_removed_from_replay_and_future_requests(self) -> None:
        self.index.observe(observed_event("Stop", "session_a", "turn_1"))
        self.index.observe(observed_event("Stop", "session_b", "turn_1"))
        frozen = self.index.freeze_sources(FIRST_REQUEST_ID, REPOSITORY_ID, NOW)
        excluded = next(item for item in frozen if item.session_id == "session_a")

        self.index.mark_excluded(
            FIRST_REQUEST_ID, excluded.source_key, "subagent_session"
        )
        replay = self.index.freeze_sources(FIRST_REQUEST_ID, REPOSITORY_ID, NOW)
        self.index.acknowledge(FIRST_REQUEST_ID, "a" * 64, NOW)
        next_request = self.index.freeze_sources(
            SECOND_REQUEST_ID, REPOSITORY_ID, NOW
        )

        self.assertEqual(["session_b"], [item.session_id for item in replay])
        self.assertEqual((), next_request)

    def test_observation_and_frozen_snapshot_survive_restart(self) -> None:
        self.index.observe(observed_event("Stop", "session_a", "turn_1"))
        self.index.close()
        self.index = SessionIndex.open(self.database_path)

        first = self.index.freeze_sources(FIRST_REQUEST_ID, REPOSITORY_ID, NOW)
        self.index.close()
        self.index = SessionIndex.open(self.database_path)
        replay = self.index.freeze_sources(FIRST_REQUEST_ID, REPOSITORY_ID, NOW)

        self.assertEqual(first, replay)


if __name__ == "__main__":
    unittest.main()
