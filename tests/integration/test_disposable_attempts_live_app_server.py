from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from zdecision.agent.capture_operation_store import CaptureOperationStore
from zdecision.agent.db import AgentDatabase
from zdecision.agent.session_index import FrozenSessionSource
from zdecision.app_server.gateway import AppServerGateway, _select_model
from zdecision.app_server.jsonl import (
    AppServerTimeout,
    AppServerTransport,
    ProcessJsonlTransport,
)
from zdecision.app_server.models import FeasibilityModelProfile
from zdecision.app_server.requested_capture import (
    CaptureAttemptRetryable,
    RequestedCaptureRunner,
)
from zdecision.capture.templates import TemplateCatalog
from zdecision.jsonio import canonical_json_bytes


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPOSITORY_ROOT / "src" / "zdecision"


class DropResponseTransport:
    """Drop exactly the first real thread/fork response by JSON-RPC id."""

    def __init__(self, transport: AppServerTransport) -> None:
        self.transport = transport
        self.requests: list[dict[str, object]] = []
        self.drop_response_id: int | None = None
        self.dropped = False

    def send(self, message: Mapping[str, object]) -> None:
        copied = dict(message)
        self.requests.append(copied)
        if (
            copied.get("method") == "thread/fork"
            and self.drop_response_id is None
        ):
            request_id = copied.get("id")
            if not isinstance(request_id, int) or isinstance(request_id, bool):
                raise AssertionError("thread/fork request id is invalid")
            self.drop_response_id = request_id
        self.transport.send(copied)

    def receive(self, timeout_seconds: float) -> Mapping[str, object]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerTimeout("fault-injected receive timeout")
            message = self.transport.receive(remaining)
            if (
                not self.dropped
                and self.drop_response_id is not None
                and message.get("id") == self.drop_response_id
                and "method" not in message
            ):
                self.dropped = True
                continue
            return message

    def close(self) -> None:
        self.transport.close()


@unittest.skipUnless(
    os.environ.get("ZDECISION_LIVE_APP_SERVER") == "1",
    "set ZDECISION_LIVE_APP_SERVER=1 for model-backed acceptance",
)
class DisposableAttemptsLiveAppServerTest(unittest.TestCase):
    def test_lost_fork_response_creates_a_higher_generation_and_one_result(
        self,
    ) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name).resolve()
        repository = root / "repository"
        repository.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=repository,
            check=True,
            capture_output=True,
        )
        database_path = root / "state.sqlite3"
        database = AgentDatabase.open(database_path)
        self.addCleanup(database.close)
        store = CaptureOperationStore.open(database_path)
        self.addCleanup(store.close)
        transport = DropResponseTransport(ProcessJsonlTransport.launch())
        gateway = AppServerGateway.connect(
            database=database,
            host_transport=transport,
            request_timeout_seconds=2.0,
            turn_timeout_seconds=300.0,
        )
        self.addCleanup(gateway.close)

        catalog = gateway._discover_models()
        model_id, reasoning_effort = _select_model(
            catalog,
            self._profileless_boundary(repository),
        )
        discovery_digest = hashlib.sha256(
            canonical_json_bytes({"models": catalog})
        ).hexdigest()
        bootstrap_profile = FeasibilityModelProfile.create(
            model_id=model_id,
            reasoning_effort=reasoning_effort,
            discovery_digest=discovery_digest,
            discovered_at="2026-07-31T00:00:00Z",
        )
        source_thread = gateway.start_disposable_thread(
            str(repository), bootstrap_profile
        )
        self.addCleanup(self._archive_safely, gateway, source_thread)
        source_receipt = gateway.run_structured_turn(
            thread_id=source_thread,
            prompt=(
                "This is a completed ZDecision development boundary. "
                "The confirmed product decision is: Candidate extraction is "
                "authorized only by an explicit page Update action. "
                "Return the acknowledgement object."
            ),
            output_schema={
                "type": "object",
                "properties": {"acknowledged": {"type": "boolean"}},
                "required": ["acknowledged"],
                "additionalProperties": False,
            },
            profile=bootstrap_profile,
            cwd=str(repository),
        )
        boundary = gateway.read_completed_boundary(
            source_thread, source_receipt.turn_id
        )
        profile = gateway.discover_and_freeze_profile(boundary)
        source = FrozenSessionSource(
            request_id="crq_" + "1" * 32,
            source_key="src_" + "2" * 32,
            repository_id="repo_" + "3" * 32,
            session_id=source_thread,
            cwd=str(repository),
            lineage="lin_" + "4" * 32,
            previous_handled_turn_id=None,
            upper_turn_id=source_receipt.turn_id,
            source_fingerprint="5" * 64,
        )
        runner = RequestedCaptureRunner(
            gateway=gateway,
            operation_store=store,
            template_catalog=TemplateCatalog(
                REPOSITORY_ROOT / "decision-templates",
                PACKAGE_ROOT / "capture" / "prompt_contracts",
            ),
        )

        with self.assertRaises(CaptureAttemptRetryable):
            runner.run(
                source,
                product_name="ZDecision",
                template_id="business",
            )
        result = runner.run(
            source,
            product_name="ZDecision",
            template_id="business",
        )
        model_calls_before_replay = self._model_call_count(transport.requests)
        replay = runner.run(
            source,
            product_name="ZDecision",
            template_id="business",
        )

        self.assertTrue(transport.dropped)
        self.assertEqual(result, replay)
        self.assertEqual(
            model_calls_before_replay,
            self._model_call_count(transport.requests),
        )
        operation = store.operation_for_source(
            source.request_id, source.source_key
        )
        self.assertIsNotNone(operation)
        self.assertEqual("committed", operation.status)
        self.assertEqual(2, operation.active_generation)
        self.assertEqual(2, operation.winner_generation)
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        try:
            attempts = connection.execute(
                """
                SELECT attempt_id, generation, state, failure_code,
                       archive_state
                FROM capture_execution_attempts
                WHERE operation_id = ?
                ORDER BY generation
                """,
                (operation.operation_id,),
            ).fetchall()
            operation_results = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM capture_operations
                WHERE operation_id = ? AND committed_result_json IS NOT NULL
                """,
                (operation.operation_id,),
            ).fetchone()["count"]
        finally:
            connection.close()
        self.assertEqual([1, 2], [row["generation"] for row in attempts])
        self.assertEqual("fork_result_unknown", attempts[0]["failure_code"])
        self.assertEqual("accepted", attempts[1]["state"])
        self.assertEqual("archived", attempts[1]["archive_state"])
        self.assertEqual(1, operation_results)
        encoded_requests = json.dumps(
            transport.requests, ensure_ascii=False, separators=(",", ":")
        )
        self.assertNotIn("threadSource", encoded_requests)
        self.assertNotIn("clientUserMessageId", encoded_requests)

        summary = {
            "source_thread_id": source_thread,
            "source_turn_id": source_receipt.turn_id,
            "operation_id": operation.operation_id,
            "first_attempt_id": attempts[0]["attempt_id"],
            "winning_attempt_id": attempts[1]["attempt_id"],
            "generations": [1, 2],
            "candidate_count": len(result.observations),
            "archive_outcomes": [
                attempts[0]["archive_state"], attempts[1]["archive_state"]
            ],
        }
        print(
            "ZDECISION_LIVE_ACCEPTANCE "
            + json.dumps(summary, sort_keys=True, separators=(",", ":"))
        )

    @staticmethod
    def _profileless_boundary(repository: Path):
        from zdecision.app_server.models import SourceBoundary

        return SourceBoundary(
            thread_id="bootstrap-thread",
            turn_id="bootstrap-turn",
            cwd=str(repository),
            status="completed",
            model_id=None,
            reasoning_effort=None,
        )

    @staticmethod
    def _model_call_count(requests: list[dict[str, object]]) -> int:
        return sum(1 for item in requests if item.get("method") == "turn/start")

    @staticmethod
    def _archive_safely(gateway: AppServerGateway, thread_id: str) -> None:
        try:
            gateway.archive_thread(thread_id)
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
