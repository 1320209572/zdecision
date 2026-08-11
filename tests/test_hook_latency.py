from __future__ import annotations

import json
import math
import subprocess
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import TestRepositoryMapping
from zdecision.agent.hooks import handle_hook
from zdecision.agent.recall_host_state import RecallHostStore
from zdecision.agent.repository import RepositoryResolver
from zdecision.central.decision_spaces import EnabledRepository
from zdecision.ids import product_id
from zdecision.recall.handoff import RecallPreflightReady
from zdecision.recall.session import RecallIntent

try:
    from zdecision.agent.worker import RetryableWorkerError, Worker, WorkerConfig
except ModuleNotFoundError as error:
    WORKER_IMPORT_ERROR: ModuleNotFoundError | None = error
else:
    WORKER_IMPORT_ERROR = None


FIXED_TIME = datetime(2026, 7, 30, 9, 30, tzinfo=UTC)
SHOW_RECALL_CONFIRMATION_TOOL = (
    "mcp__zdecision_local__show_zdecision_recall_confirmation"
)
VALID_INTENT: dict[str, object] = {
    "target_decision_space_ids": ["dsp_" + "4" * 32],
    "explicit_multi_space": False,
    "feature_goal": "Measure trusted Recall preflight latency",
    "domain_objects": ["RecallPreflightReady"],
    "repository_relative_paths": ["src/zdecision/agent/hooks.py"],
    "constraints": ["No network access"],
    "exclusions": ["Decision retrieval"],
}


class ReadyPreflightProvider:
    def __init__(self, preflight: RecallPreflightReady) -> None:
        self.preflight_value = preflight
        self.preflight_calls = 0
        self.retrieve_calls = 0

    def preflight(self, **_kwargs: object) -> RecallPreflightReady:
        self.preflight_calls += 1
        return self.preflight_value

    def retrieve(self, _preflight: RecallPreflightReady):
        self.retrieve_calls += 1
        raise AssertionError("Hook preflight must never retrieve")


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
        self.recall_store = RecallHostStore.open(self.database_path)
        self.resolver = RepositoryResolver(timeout_seconds=0.5)
        self.snapshot = self.resolver.resolve(self.repository)
        self.assertIsNotNone(self.snapshot)
        self.database.put_test_repository_mapping(
            TestRepositoryMapping(
                repository_id=self.snapshot.repository_id,
                product_id=product_id("Latency Test"),
                product_name="Latency Test",
                enabled=True,
            )
        )
        self.database.put_enabled_repository(
            EnabledRepository(self.snapshot.repository_id, True)
        )
        self.plugin_root = self.root / "plugin-cache/zdecision/0.1.0"
        recall_skill = self.plugin_root / "skills/zdecision/SKILL.md"
        recall_skill.parent.mkdir(parents=True)
        recall_skill.write_text("---\nname: zdecision\n---\n", "utf-8")
        (self.plugin_root / ".codex-plugin").mkdir()
        (self.plugin_root / ".codex-plugin/plugin.json").write_text(
            json.dumps(
                {
                    "name": "zdecision",
                    "version": "0.1.0",
                    "skills": "./skills/",
                    "mcpServers": "./.mcp.json",
                }
            ),
            "utf-8",
        )
        (self.plugin_root / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "zdecision-local": {
                            "command": "zdecision-agent",
                            "args": ["mcp"],
                        }
                    }
                }
            ),
            "utf-8",
        )
        self.environment = patch.dict(
            "os.environ", {"PLUGIN_ROOT": str(self.plugin_root)}, clear=False
        )
        self.environment.start()

    def tearDown(self) -> None:
        if hasattr(self, "environment"):
            self.environment.stop()
        if hasattr(self, "recall_store"):
            self.recall_store.close()
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

    def test_active_turn_gate_hook_p95_is_at_most_150_milliseconds(self) -> None:
        self.recall_store.bind_activation(
            session_id="thr_active_latency",
            turn_id="turn_activation",
            cwd=str(self.repository),
            binding_id="activation-latency",
            now=FIXED_TIME,
        )

        elapsed_milliseconds: list[float] = []
        with patch(
            "socket.socket.connect",
            side_effect=AssertionError("Hook must not make a network call"),
        ):
            for index in range(200):
                started = time.perf_counter()
                raw = {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "thr_active_latency",
                    "turn_id": f"turn_{index}",
                    "cwd": str(self.repository),
                    "prompt": "must not be read",
                }
                response = handle_hook(
                    raw,
                    database=self.database,
                    clock=lambda: FIXED_TIME,
                    repository_resolver=self.resolver,
                    worker_waker=lambda _: None,
                    recall_store=self.recall_store,
                )
                elapsed_milliseconds.append((time.perf_counter() - started) * 1000)
                self.assertTrue(response.event_id)
                self.assertIn("additionalContext", response.output["hookSpecificOutput"])

        ordered = sorted(elapsed_milliseconds)
        p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
        print(f"Active recall Hook latency p95: {p95:.2f} ms")
        self.assertLessEqual(p95, 150.0)

    def test_ready_preflight_hook_p95_is_at_most_150_milliseconds(self) -> None:
        self._record(900)
        intent = RecallIntent.from_dict(VALID_INTENT)
        preflight = RecallPreflightReady(
            repository_id=self.snapshot.repository_id,
            repository_display_name=self.repository.name,
            intent=intent,
            target_decision_space_ids=("dsp_" + "4" * 32,),
            target_display_names=("Latency Test",),
            catalog_digest="a" * 64,
            generation=2,
            generation_digest="b" * 64,
            retrieval_profile_digest="c" * 64,
            index_generation=2,
            freshness="ready",
            expires_at="2026-07-30T10:30:00Z",
        )
        provider = ReadyPreflightProvider(preflight)
        raw = {
            "hook_event_name": "PreToolUse",
            "session_id": "thr_latency",
            "turn_id": "turn_900",
            "cwd": str(self.repository),
            "tool_name": SHOW_RECALL_CONFIRMATION_TOOL,
            "tool_input": {
                "activation_attempt_id": "model-attempt",
                "intent": dict(VALID_INTENT),
            },
        }

        elapsed_milliseconds: list[float] = []
        with patch(
            "socket.socket.connect",
            side_effect=AssertionError("Hook must not make a network call"),
        ):
            for _ in range(200):
                started = time.perf_counter()
                response = handle_hook(
                    raw,
                    database=self.database,
                    clock=lambda: FIXED_TIME,
                    repository_resolver=self.resolver,
                    worker_waker=lambda _: None,
                    recall_store=self.recall_store,
                    recall_provider=provider,
                    activation_attempt_id_factory=lambda: "activation-latency-ready",
                )
                elapsed_milliseconds.append(
                    (time.perf_counter() - started) * 1000
                )
                self.assertEqual(
                    "allow",
                    response.output["hookSpecificOutput"]["permissionDecision"],
                )

        ordered = sorted(elapsed_milliseconds)
        p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
        print(f"Ready preflight Hook latency p95: {p95:.2f} ms")
        self.assertLessEqual(p95, 150.0)
        self.assertEqual(200, provider.preflight_calls)
        self.assertEqual(0, provider.retrieve_calls)


if __name__ == "__main__":
    unittest.main()
