from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from zdecision.agent import cli as agent_cli
from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import (
    HookInvocation,
    RepositorySnapshot,
    TestRepositoryMapping,
    event_id_for,
)
from zdecision.agent.hooks import handle_hook
from zdecision.agent.session_index import SessionIndex, SessionIndexEventProcessor
from zdecision.central.decision_spaces import EnabledRepository
from zdecision.ids import product_id

try:
    from zdecision.agent.worker import (
        RetryableWorkerError,
        Worker,
        WorkerConfig,
    )
except ModuleNotFoundError as error:
    WORKER_IMPORT_ERROR: ModuleNotFoundError | None = error
else:
    WORKER_IMPORT_ERROR = None


FIXED_TIME = datetime(2026, 7, 30, 9, 30, tzinfo=UTC)


class RecordingProcessor:
    def __init__(self, *, delay_seconds: float = 0.0) -> None:
        self.delay_seconds = delay_seconds
        self.seen_event_ids: list[str] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def process(self, event) -> None:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay_seconds:
                time.sleep(self.delay_seconds)
            with self._lock:
                self.seen_event_ids.append(event.event_id)
        finally:
            with self._lock:
                self.active -= 1


class FailOnceProcessor:
    def __init__(self) -> None:
        self.attempts = 0
        self.seen_event_ids: list[str] = []

    def process(self, event) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise RetryableWorkerError("central_unavailable")
        self.seen_event_ids.append(event.event_id)


class IncrementingPoller:
    def __init__(self) -> None:
        self.seen_cursors: list[int] = []

    def poll(self, current_cursor: int) -> int:
        self.seen_cursors.append(current_cursor)
        return current_cursor + 1


class FailingPoller:
    def poll(self, current_cursor: int) -> int:
        raise ConnectionError("fake central endpoint unavailable")


class StaticResolver:
    def __init__(self, snapshot: RepositorySnapshot) -> None:
        self.snapshot = snapshot

    def resolve(self, cwd: str | Path) -> RepositorySnapshot | None:
        if Path(cwd) == Path(self.snapshot.worktree_root):
            return self.snapshot
        return None


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNone(
            WORKER_IMPORT_ERROR,
            f"zdecision.agent.worker is missing: {WORKER_IMPORT_ERROR}",
        )
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "state" / "zdecision.sqlite3"
        self.database = AgentDatabase.open(self.database_path)
        self.snapshot = RepositorySnapshot(
            repository_id="repo_" + "a" * 32,
            worktree_root=str(self.root),
            branch="main",
            head_commit="b" * 40,
        )

    def tearDown(self) -> None:
        if hasattr(self, "database"):
            self.database.close()
        if hasattr(self, "temporary_directory"):
            self.temporary_directory.cleanup()

    def _invocation(
        self,
        index: int,
        *,
        event_name: str = "UserPromptSubmit",
        session_id: str = "thr_worker",
        occurred_at: datetime = FIXED_TIME,
    ) -> HookInvocation:
        raw: dict[str, object] = {
            "hook_event_name": event_name,
            "session_id": session_id,
            "cwd": str(self.root),
        }
        if event_name in {"UserPromptSubmit", "PostToolUse", "Stop"}:
            raw["turn_id"] = f"turn_{index}"
        if event_name == "SessionStart":
            raw["source"] = "startup"
        elif event_name == "SessionEnd":
            raw["reason"] = "other"
        elif event_name == "PostToolUse":
            raw.update(
                {
                    "tool_name": "Bash",
                    "tool_use_id": f"tool_{index}",
                    "tool_input": {"command": "true"},
                    "tool_response": {"exit_code": 0},
                }
            )
        return HookInvocation.from_dict(
            raw,
            occurred_at=occurred_at.isoformat(),
            repository=self.snapshot,
        )

    def _worker(
        self,
        database: AgentDatabase,
        processor,
        *,
        poller=None,
        config=None,
    ):
        return Worker(
            database=database,
            processor=processor,
            sync_poller=poller or IncrementingPoller(),
            lock_path=self.root / "worker.lock",
            config=config
            or WorkerConfig(
                claim_limit=11,
                processing_lease_seconds=0.1,
                session_lease_seconds=1.0,
                poll_interval_seconds=0.05,
                idle_grace_seconds=0.02,
            ),
        )

    def test_two_simultaneous_workers_consume_100_rapid_unique_events_once(
        self,
    ) -> None:
        invocations = tuple(self._invocation(index) for index in range(100))
        expected_event_ids = {event_id_for(value) for value in invocations}

        def record(index: int) -> None:
            database = AgentDatabase.open(self.database_path)
            try:
                database.record_hook(invocations[index % 100])
            finally:
                database.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            tuple(pool.map(record, range(200)))

        self.assertEqual(100, self.database.count_events())
        processor = RecordingProcessor(delay_seconds=0.001)
        barrier = threading.Barrier(2)

        def run_worker() -> None:
            database = AgentDatabase.open(self.database_path)
            try:
                worker = self._worker(database, processor)
                barrier.wait(timeout=2)
                worker.run_until_idle()
            finally:
                database.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            tuple(pool.map(lambda _: run_worker(), range(2)))

        self.assertEqual(expected_event_ids, set(processor.seen_event_ids))
        self.assertEqual(100, len(processor.seen_event_ids))
        self.assertEqual(1, processor.max_active)
        for event_id in expected_event_ids:
            self.assertEqual("consumed", self.database.get_event(event_id).state)

        replayed = self.database.record_hook(invocations[0])
        self.assertEqual("consumed", replayed.state)
        self.assertEqual(100, len(processor.seen_event_ids))

    def test_expired_processing_claim_is_recovered_after_worker_crash(self) -> None:
        invocation = self._invocation(1)
        event = self.database.record_hook(invocation)

        claimed = self.database.claim_events(
            FIXED_TIME,
            limit=1,
            processing_lease_seconds=30.0,
        )

        self.assertEqual((event.event_id,), tuple(item.event_id for item in claimed))
        self.assertEqual("processing", self.database.get_event(event.event_id).state)
        processor = RecordingProcessor()
        worker = self._worker(
            self.database,
            processor,
            config=WorkerConfig(processing_lease_seconds=30.0),
        )

        early = worker.run_once(FIXED_TIME + timedelta(seconds=29))
        recovered = worker.run_once(FIXED_TIME + timedelta(seconds=30))

        self.assertEqual(0, early.claimed)
        self.assertEqual(1, recovered.claimed)
        self.assertEqual(1, recovered.consumed)
        self.assertEqual([event.event_id], processor.seen_event_ids)
        self.assertEqual("consumed", self.database.get_event(event.event_id).state)

    def test_retryable_processor_failure_waits_for_retry_deadline(self) -> None:
        event = self.database.record_hook(self._invocation(2))
        processor = FailOnceProcessor()
        worker = self._worker(
            self.database,
            processor,
            config=WorkerConfig(processing_lease_seconds=30.0),
        )

        failed = worker.run_once(FIXED_TIME)
        self.assertEqual(1, failed.failed_retryable)
        self.assertEqual("failed_retryable", self.database.get_event(event.event_id).state)
        self.assertEqual(
            "central_unavailable", self.database.get_event(event.event_id).failure_code
        )

        too_early = worker.run_once(FIXED_TIME + timedelta(seconds=29))
        self.assertEqual(0, too_early.claimed)

        retried = worker.run_once(FIXED_TIME + timedelta(seconds=30))
        self.assertEqual(1, retried.consumed)
        self.assertEqual(2, processor.attempts)
        self.assertEqual([event.event_id], processor.seen_event_ids)

    def test_active_session_polls_without_prompt_and_session_end_closes_lease(
        self,
    ) -> None:
        self.database.put_test_repository_mapping(
            TestRepositoryMapping(
                repository_id=self.snapshot.repository_id,
                product_id=product_id("Worker Test"),
                product_name="Worker Test",
                enabled=True,
            )
        )
        self.database.put_enabled_repository(
            EnabledRepository(self.snapshot.repository_id, True)
        )
        resolver = StaticResolver(self.snapshot)
        wake_counts: list[int] = []

        def committed_wake(database_path: Path) -> None:
            separate = AgentDatabase.open(database_path)
            try:
                wake_counts.append(separate.count_events())
            finally:
                separate.close()

        handle_hook(
            {
                "hook_event_name": "SessionStart",
                "session_id": "thr_lease",
                "cwd": str(self.root),
                "source": "startup",
            },
            database=self.database,
            clock=lambda: FIXED_TIME,
            repository_resolver=resolver,
            worker_waker=committed_wake,
        )
        self.assertEqual([1], wake_counts)
        self.assertEqual(1, len(self.database.active_session_leases(FIXED_TIME)))

        poller = IncrementingPoller()
        worker = self._worker(
            self.database,
            RecordingProcessor(),
            poller=poller,
            config=WorkerConfig(
                session_lease_seconds=120.0,
                poll_interval_seconds=60.0,
            ),
        )

        initial = worker.run_once(FIXED_TIME)
        before_deadline = worker.run_once(FIXED_TIME + timedelta(seconds=59))
        at_deadline = worker.run_once(FIXED_TIME + timedelta(seconds=60))

        self.assertEqual(1, initial.sync_cursor)
        self.assertEqual(1, before_deadline.sync_cursor)
        self.assertEqual(2, at_deadline.sync_cursor)
        self.assertEqual([0, 1], poller.seen_cursors)

        handle_hook(
            {
                "hook_event_name": "SessionEnd",
                "session_id": "thr_lease",
                "cwd": str(self.root),
                "reason": "other",
            },
            database=self.database,
            clock=lambda: FIXED_TIME + timedelta(seconds=61),
            repository_resolver=resolver,
            worker_waker=lambda _: None,
        )
        ended = worker.run_once(FIXED_TIME + timedelta(seconds=61))

        self.assertEqual(0, ended.active_sessions)
        self.assertEqual(
            (), self.database.active_session_leases(FIXED_TIME + timedelta(seconds=61))
        )

    def test_sync_outage_does_not_fail_event_processing(self) -> None:
        self.database.renew_session(
            "thr_outage",
            str(self.root),
            renewed_at=FIXED_TIME,
            expires_at=FIXED_TIME + timedelta(seconds=120),
            create=True,
        )
        event = self.database.record_hook(self._invocation(3, session_id="thr_outage"))
        processor = RecordingProcessor()
        worker = self._worker(self.database, processor, poller=FailingPoller())

        cycle = worker.run_once(FIXED_TIME)

        self.assertEqual(1, cycle.consumed)
        self.assertEqual(0, cycle.sync_cursor)
        self.assertEqual("consumed", self.database.get_event(event.event_id).state)

    def test_session_index_processor_records_stop_boundary_before_consuming(self) -> None:
        event = self.database.record_hook(
            self._invocation(4, event_name="Stop", session_id="thr_indexed")
        )
        index = SessionIndex.open(self.database_path)
        try:
            worker = self._worker(
                self.database,
                SessionIndexEventProcessor(index),
            )

            cycle = worker.run_once(FIXED_TIME)
            frozen = index.freeze_sources(
                "crq_" + "1" * 32,
                self.snapshot.repository_id,
                FIXED_TIME,
                capture_scope="all_valid_sessions",
            )
        finally:
            index.close()

        self.assertEqual(1, cycle.consumed)
        self.assertEqual("consumed", self.database.get_event(event.event_id).state)
        self.assertEqual(["turn_4"], [item.upper_turn_id for item in frozen])

    def test_worker_command_wires_the_session_index_processor(self) -> None:
        state_root = self.root / "cli-state"
        with (
            patch.dict(
                os.environ,
                {"ZDECISION_STATE_DIR": str(state_root)},
                clear=False,
            ),
            patch("zdecision.agent.worker.Worker") as worker_class,
        ):
            result = agent_cli.main(["worker"])

        self.assertEqual(0, result)
        processor = worker_class.call_args.kwargs["processor"]
        self.assertIsInstance(processor, SessionIndexEventProcessor)
        self.assertIsNone(worker_class.call_args.kwargs["sync_poller"])
        worker_class.return_value.run_until_idle.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
