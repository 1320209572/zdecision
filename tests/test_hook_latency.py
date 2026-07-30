from __future__ import annotations

import math
import subprocess
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import TestRepositoryMapping
from zdecision.agent.hooks import handle_hook
from zdecision.agent.repository import RepositoryResolver
from zdecision.ids import product_id

try:
    from zdecision.agent.worker import RetryableWorkerError, Worker, WorkerConfig
except ModuleNotFoundError as error:
    WORKER_IMPORT_ERROR: ModuleNotFoundError | None = error
else:
    WORKER_IMPORT_ERROR = None


FIXED_TIME = datetime(2026, 7, 30, 9, 30, tzinfo=UTC)


class SlowUnavailableProcessor:
    def __init__(self) -> None:
        self.calls = 0

    def process(self, event) -> None:
        self.calls += 1
        time.sleep(0.2)
        raise RetryableWorkerError("central_unavailable")


class NoopPoller:
    def poll(self, current_cursor: int) -> int:
        return current_cursor


class HookLatencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNone(
            WORKER_IMPORT_ERROR,
            f"zdecision.agent.worker is missing: {WORKER_IMPORT_ERROR}",
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
        self.resolver = RepositoryResolver(timeout_seconds=0.5)
        snapshot = self.resolver.resolve(self.repository)
        self.assertIsNotNone(snapshot)
        self.database.put_test_repository_mapping(
            TestRepositoryMapping(
                repository_id=snapshot.repository_id,
                product_id=product_id("Latency Test"),
                product_name="Latency Test",
                enabled=True,
            )
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

    def _record(self, index: int):
        return handle_hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "thr_latency",
                "turn_id": f"turn_{index}",
                "cwd": str(self.repository),
                "prompt": "not retained",
            },
            database=self.database,
            clock=lambda: FIXED_TIME,
            repository_resolver=self.resolver,
            worker_waker=lambda _: None,
        )

    def test_warm_hook_ingestion_p95_is_at_most_150_milliseconds(self) -> None:
        for index in range(10):
            self.assertTrue(self._record(index).event_id)

        elapsed_milliseconds: list[float] = []
        for index in range(10, 210):
            started = time.perf_counter()
            response = self._record(index)
            elapsed_milliseconds.append((time.perf_counter() - started) * 1000)
            self.assertTrue(response.event_id)

        ordered = sorted(elapsed_milliseconds)
        p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
        print(f"Hook latency p95: {p95:.2f} ms")
        self.assertLessEqual(p95, 150.0)
        self.assertEqual(210, self.database.count_events())

    def test_slow_network_failure_runs_only_inside_worker(self) -> None:
        started = time.perf_counter()
        response = self._record(500)
        hook_elapsed = time.perf_counter() - started

        processor = SlowUnavailableProcessor()
        worker = Worker(
            database=self.database,
            processor=processor,
            sync_poller=NoopPoller(),
            lock_path=self.root / "worker.lock",
            config=WorkerConfig(processing_lease_seconds=30.0),
        )
        started = time.perf_counter()
        cycle = worker.run_once(FIXED_TIME)
        worker_elapsed = time.perf_counter() - started

        self.assertTrue(response.event_id)
        self.assertLess(hook_elapsed, 0.15)
        self.assertGreaterEqual(worker_elapsed, 0.19)
        self.assertEqual(1, processor.calls)
        self.assertEqual(1, cycle.failed_retryable)


if __name__ == "__main__":
    unittest.main()
