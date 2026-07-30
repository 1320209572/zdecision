from __future__ import annotations

import io
import queue
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from zdecision.agent.db import AgentDatabase
from zdecision.app_server.gateway import (
    AppServerGateway,
    AppServerUnavailable,
    IncompleteSourceTurn,
    ModelDiscoveryConflict,
    UnknownSourceTurn,
)
from zdecision.app_server.jsonl import (
    CONTROLLED_APP_SERVER_COMMAND,
    AppServerEOF,
    AppServerProtocolError,
    AppServerRequestError,
    AppServerTimeout,
    JsonlAppServerClient,
    ProcessJsonlTransport,
    UnexpectedServerRequest,
)
from zdecision.app_server.models import FeasibilityModelProfile, SourceBoundary


FIXED_TIME = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
SOURCE_THREAD = "019fb100-0000-7000-8000-000000000001"
SOURCE_TURN = "019fb100-0000-7000-8000-000000000002"
FORK_THREAD = "019fb100-0000-7000-8000-000000000003"
GENERATED_TURN = "019fb100-0000-7000-8000-000000000004"


class QueueTransport:
    def __init__(self, on_send=None) -> None:
        self.sent: list[dict[str, object]] = []
        self.incoming: queue.Queue[object] = queue.Queue()
        self.on_send = on_send
        self.closed = False

    def send(self, message) -> None:
        copied = dict(message)
        self.sent.append(copied)
        if self.on_send is not None:
            self.on_send(copied, self)

    def receive(self, timeout_seconds: float):
        try:
            value = self.incoming.get(timeout=timeout_seconds)
        except queue.Empty:
            raise AppServerTimeout("fake timeout") from None
        if isinstance(value, BaseException):
            raise value
        return value

    def push(self, value: object) -> None:
        self.incoming.put(value)

    def close(self) -> None:
        self.closed = True
        self.incoming.put(AppServerEOF("fake closed"))


class ScriptedClient:
    def __init__(self, responses, notifications=()) -> None:
        self.responses = list(responses)
        self.notifications = list(notifications)
        self.requests: list[tuple[str, dict[str, object]]] = []

    def request(self, method: str, params, timeout_seconds=None):
        self.requests.append((method, dict(params)))
        if not self.responses:
            raise AssertionError(f"Unexpected request: {method}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def wait_for_notification(self, method, predicate, timeout_seconds=None):
        if not self.notifications:
            raise AssertionError(f"Unexpected notification wait: {method}")
        notification_method, params = self.notifications.pop(0)
        if notification_method != method or not predicate(params):
            raise AssertionError("Notification did not match the requested Turn")
        return params

    def close(self) -> None:
        pass


class BlockingLines:
    def __init__(self) -> None:
        self.values: queue.Queue[str | None] = queue.Queue()

    def push(self, value: str | None) -> None:
        self.values.put(value)

    def __iter__(self):
        return self

    def __next__(self) -> str:
        value = self.values.get(timeout=2)
        if value is None:
            raise StopIteration
        return value


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = io.StringIO()
        self.stdout = BlockingLines()
        self.stderr = BlockingLines()
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0
        self.stdout.push(None)
        self.stderr.push(None)

    def wait(self, timeout=None):
        return 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def model_catalog(*, default_model: str = "model-default") -> dict[str, object]:
    return {
        "data": [
            {
                "id": "model-source",
                "model": "model-source",
                "displayName": "Source",
                "description": "Source fixture",
                "hidden": False,
                "isDefault": False,
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low", "description": "Low"},
                    {"reasoningEffort": "high", "description": "High"},
                ],
            },
            {
                "id": default_model,
                "model": default_model,
                "displayName": "Default",
                "description": "Default fixture",
                "hidden": False,
                "isDefault": True,
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "medium", "description": "Medium"},
                ],
            },
        ],
        "nextCursor": None,
    }


class JsonlClientTests(unittest.TestCase):
    def test_controlled_process_uses_only_the_predeclared_command(self):
        process = FakeProcess()
        with patch(
            "zdecision.app_server.jsonl.subprocess.Popen", return_value=process
        ) as popen:
            transport = ProcessJsonlTransport.launch()
            transport.close()

        self.assertEqual(list(CONTROLLED_APP_SERVER_COMMAND), popen.call_args.args[0])
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertTrue(process.terminated)

    def test_one_handshake_monotonic_ids_response_correlation_and_notification(self):
        def respond(message, transport: QueueTransport) -> None:
            if message.get("method") == "initialize":
                transport.push({"id": message["id"], "result": {"userAgent": "fake"}})
            elif message.get("method") == "model/list":
                transport.push({"method": "warning", "params": {"message": "bounded"}})
                transport.push({"id": message["id"], "result": model_catalog()})

        transport = QueueTransport(respond)
        client = JsonlAppServerClient(transport, default_timeout_seconds=0.5)
        try:
            client.initialize()
            client.initialize()
            result = client.request("model/list", {"limit": 100, "includeHidden": True})
            warning = client.wait_for_notification(
                "warning", lambda params: params.get("message") == "bounded"
            )
        finally:
            client.close()

        self.assertEqual("model-default", result["data"][1]["id"])
        self.assertEqual("bounded", warning["message"])
        self.assertEqual("initialize", transport.sent[0]["method"])
        self.assertEqual(1, transport.sent[0]["id"])
        self.assertEqual(
            {"method": "initialized", "params": {}}, transport.sent[1]
        )
        self.assertEqual("model/list", transport.sent[2]["method"])
        self.assertEqual(2, transport.sent[2]["id"])
        self.assertNotIn("jsonrpc", transport.sent[0])

    def test_timeout_eof_malformed_json_and_request_error_are_bounded(self):
        timeout_transport = QueueTransport()
        timeout_client = JsonlAppServerClient(
            timeout_transport, default_timeout_seconds=0.02
        )
        with self.assertRaises(AppServerTimeout):
            timeout_client.initialize()
        timeout_client.close()

        eof_transport = QueueTransport()
        eof_transport.push(AppServerEOF("secret-value-must-not-leak"))
        eof_client = JsonlAppServerClient(eof_transport, default_timeout_seconds=0.2)
        with self.assertRaises(AppServerEOF) as eof_error:
            eof_client.initialize()
        self.assertNotIn("secret-value-must-not-leak", str(eof_error.exception))
        eof_client.close()

        process = FakeProcess()
        process_transport = ProcessJsonlTransport(process, max_stderr_lines=2)
        process.stderr.push("token=secret-value-must-not-leak\n")
        process.stderr.push("second\n")
        process.stderr.push("third\n")
        process.stdout.push("{not valid json secret-value-must-not-leak}\n")
        with self.assertRaises(AppServerProtocolError) as malformed_error:
            process_transport.receive(0.5)
        self.assertNotIn("secret-value-must-not-leak", str(malformed_error.exception))
        process_transport.close()
        self.assertTrue(process.terminated)
        self.assertLessEqual(len(process_transport.stderr_tail), 2)

        def reject(message, transport: QueueTransport) -> None:
            transport.push(
                {
                    "id": message["id"],
                    "error": {
                        "code": 401,
                        "message": "credential secret-value-must-not-leak",
                    },
                }
            )

        rejected_transport = QueueTransport(reject)
        rejected_client = JsonlAppServerClient(
            rejected_transport, default_timeout_seconds=0.5
        )
        with self.assertRaises(AppServerRequestError) as request_error:
            rejected_client.initialize()
        self.assertNotIn("secret-value-must-not-leak", str(request_error.exception))
        rejected_client.close()

    def test_server_approval_is_cancelled_and_fails_instead_of_hanging(self):
        def respond(message, transport: QueueTransport) -> None:
            if message.get("method") == "initialize":
                transport.push({"id": message["id"], "result": {}})
            elif message.get("method") == "turn/start":
                transport.push(
                    {
                        "id": message["id"],
                        "result": {
                            "turn": {
                                "id": GENERATED_TURN,
                                "status": "inProgress",
                                "items": [],
                            }
                        },
                    }
                )
                transport.push(
                    {
                        "id": "approval-1",
                        "method": "item/commandExecution/requestApproval",
                        "params": {"threadId": FORK_THREAD, "turnId": GENERATED_TURN},
                    }
                )

        transport = QueueTransport(respond)
        client = JsonlAppServerClient(transport, default_timeout_seconds=0.5)
        try:
            client.initialize()
            client.request("turn/start", {"threadId": FORK_THREAD, "input": []})
            with self.assertRaises(UnexpectedServerRequest):
                client.wait_for_notification("turn/completed", lambda _: True)
        finally:
            client.close()

        self.assertIn(
            {"id": "approval-1", "result": {"decision": "cancel"}},
            transport.sent,
        )


class AppServerGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database = AgentDatabase.open(self.root / "agent.sqlite3")

    def tearDown(self) -> None:
        self.database.close()
        self.temporary_directory.cleanup()

    def _gateway(self, client: ScriptedClient) -> AppServerGateway:
        return AppServerGateway(
            client=client,
            database=self.database,
            clock=lambda: FIXED_TIME,
            turn_timeout_seconds=0.5,
        )

    def _thread_read_result(self, status: str = "completed") -> dict[str, object]:
        return {
            "thread": {
                "id": SOURCE_THREAD,
                "cwd": str(self.root),
                "model": "model-source",
                "reasoningEffort": "high",
                "turns": [
                    {"id": SOURCE_TURN, "status": status, "items": []},
                ],
            }
        }

    def test_connect_prefers_an_explicit_host_transport(self):
        def initialize(message, transport: QueueTransport) -> None:
            if message.get("method") == "initialize":
                transport.push({"id": message["id"], "result": {}})

        host = QueueTransport(initialize)
        gateway = AppServerGateway.connect(
            database=self.database,
            host_transport=host,
            process_factory=lambda: self.fail("fallback must not launch"),
            clock=lambda: FIXED_TIME,
            request_timeout_seconds=0.5,
        )
        try:
            self.assertEqual("host", gateway.route)
            self.assertEqual(
                [("host", None)],
                [
                    (value.route, value.failure_code)
                    for value in self.database.list_app_server_route_events()
                ],
            )
        finally:
            gateway.close()

    def test_connect_records_missing_host_and_uses_controlled_fallback(self):
        def initialize(message, transport: QueueTransport) -> None:
            if message.get("method") == "initialize":
                transport.push({"id": message["id"], "result": {}})

        controlled = QueueTransport(initialize)
        launches = 0

        def launch():
            nonlocal launches
            launches += 1
            return controlled

        gateway = AppServerGateway.connect(
            database=self.database,
            process_factory=launch,
            clock=lambda: FIXED_TIME,
            request_timeout_seconds=0.5,
        )
        try:
            self.assertEqual("controlled_process", gateway.route)
            self.assertEqual(1, launches)
            self.assertEqual(
                [
                    ("host", "host_transport_unavailable"),
                    ("controlled_process", None),
                ],
                [
                    (value.route, value.failure_code)
                    for value in self.database.list_app_server_route_events()
                ],
            )
        finally:
            gateway.close()

    def test_connect_stops_when_host_and_controlled_routes_fail(self):
        failing_host = QueueTransport()

        def unavailable_process():
            raise OSError("private credential details must not escape")

        with self.assertRaises(AppServerUnavailable) as error:
            AppServerGateway.connect(
                database=self.database,
                host_transport=failing_host,
                process_factory=unavailable_process,
                clock=lambda: FIXED_TIME,
                request_timeout_seconds=0.02,
            )

        self.assertNotIn("private credential", str(error.exception))
        self.assertEqual(
            [
                ("host", "host_transport_failed"),
                ("controlled_process", "controlled_process_unavailable"),
            ],
            [
                (value.route, value.failure_code)
                for value in self.database.list_app_server_route_events()
            ],
        )

    def test_reads_only_an_exact_completed_boundary_and_forks_through_it(self):
        client = ScriptedClient(
            [
                self._thread_read_result(),
                {
                    "thread": {
                        "id": FORK_THREAD,
                        "ephemeral": True,
                        "forkedFromId": SOURCE_THREAD,
                    }
                },
            ]
        )
        gateway = self._gateway(client)

        boundary = gateway.read_completed_boundary(SOURCE_THREAD, SOURCE_TURN)
        fork_id = gateway.fork_ephemeral(SOURCE_THREAD, SOURCE_TURN)

        self.assertEqual(
            SourceBoundary(
                thread_id=SOURCE_THREAD,
                turn_id=SOURCE_TURN,
                cwd=str(self.root),
                status="completed",
                model_id="model-source",
                reasoning_effort="high",
            ),
            boundary,
        )
        self.assertEqual(FORK_THREAD, fork_id)
        self.assertEqual(
            (
                "thread/read",
                {"threadId": SOURCE_THREAD, "includeTurns": True},
            ),
            client.requests[0],
        )
        self.assertEqual(
            (
                "thread/fork",
                {
                    "threadId": SOURCE_THREAD,
                    "lastTurnId": SOURCE_TURN,
                    "ephemeral": True,
                },
            ),
            client.requests[1],
        )

    def test_rejects_unknown_or_in_progress_source_turn(self):
        unknown = self._gateway(ScriptedClient([self._thread_read_result()]))
        with self.assertRaises(UnknownSourceTurn):
            unknown.read_completed_boundary(SOURCE_THREAD, "missing-turn")

        active = self._gateway(
            ScriptedClient([self._thread_read_result(status="inProgress")])
        )
        with self.assertRaises(IncompleteSourceTurn):
            active.read_completed_boundary(SOURCE_THREAD, SOURCE_TURN)

    def test_freezes_supported_source_profile_and_replays_same_discovery(self):
        boundary = SourceBoundary(
            thread_id=SOURCE_THREAD,
            turn_id=SOURCE_TURN,
            cwd=str(self.root),
            status="completed",
            model_id="model-source",
            reasoning_effort="high",
        )
        client = ScriptedClient([model_catalog(), model_catalog()])
        gateway = self._gateway(client)

        first = gateway.discover_and_freeze_profile(boundary)
        replay = gateway.discover_and_freeze_profile(
            SourceBoundary(
                thread_id="other-thread",
                turn_id="other-turn",
                cwd=str(self.root),
                status="completed",
                model_id=None,
                reasoning_effort=None,
            )
        )

        self.assertEqual("model-source", first.model_id)
        self.assertEqual("high", first.reasoning_effort)
        self.assertEqual(first, replay)
        stored = self.database.get_feasibility_model_profile()
        self.assertEqual(first.profile_id, stored.profile_id)
        self.assertEqual(
            ("model/list", {"limit": 100, "includeHidden": True}),
            client.requests[0],
        )

    def test_uses_returned_default_when_source_profile_is_not_supported(self):
        boundary = SourceBoundary(
            thread_id=SOURCE_THREAD,
            turn_id=SOURCE_TURN,
            cwd=str(self.root),
            status="completed",
            model_id="model-source",
            reasoning_effort="xhigh",
        )
        profile = self._gateway(
            ScriptedClient([model_catalog()])
        ).discover_and_freeze_profile(boundary)

        self.assertEqual("model-default", profile.model_id)
        self.assertEqual("medium", profile.reasoning_effort)
        self.assertTrue(profile.profile_id.startswith("fmp_"))

    def test_conflicting_catalog_digest_stops_gate_three(self):
        boundary = SourceBoundary(
            thread_id=SOURCE_THREAD,
            turn_id=SOURCE_TURN,
            cwd=str(self.root),
            status="completed",
            model_id=None,
            reasoning_effort=None,
        )
        gateway = self._gateway(
            ScriptedClient(
                [model_catalog(), model_catalog(default_model="changed-default")]
            )
        )
        gateway.discover_and_freeze_profile(boundary)

        with self.assertRaises(ModelDiscoveryConflict):
            gateway.discover_and_freeze_profile(boundary)

    def test_structured_turn_pins_profile_and_returns_native_completed_receipt(self):
        profile = FeasibilityModelProfile.create(
            model_id="model-default",
            reasoning_effort="medium",
            discovery_digest="a" * 64,
            discovered_at="2026-07-30T12:00:00.000000Z",
        )
        output = {"capture_eligible": True, "reason": "stable milestone"}
        client = ScriptedClient(
            [
                {
                    "turn": {
                        "id": GENERATED_TURN,
                        "status": "inProgress",
                        "items": [],
                    }
                }
            ],
            [
                (
                    "turn/completed",
                    {
                        "threadId": FORK_THREAD,
                        "turn": {
                            "id": GENERATED_TURN,
                            "status": "completed",
                            "items": [
                                {
                                    "id": "message-1",
                                    "type": "agentMessage",
                                    "phase": "final_answer",
                                    "text": (
                                        '{"capture_eligible":true,'
                                        '"reason":"stable milestone"}'
                                    ),
                                }
                            ],
                        },
                    },
                )
            ],
        )
        schema = {
            "type": "object",
            "properties": {"capture_eligible": {"type": "boolean"}},
            "required": ["capture_eligible"],
            "additionalProperties": True,
        }

        receipt = self._gateway(client).run_structured_turn(
            thread_id=FORK_THREAD,
            prompt="Assess the completed boundary.",
            output_schema=schema,
            profile=profile,
            cwd=str(self.root),
        )

        self.assertEqual(output, receipt.structured_output)
        self.assertEqual("completed", receipt.status)
        self.assertEqual(profile.profile_id, receipt.model_profile_id)
        self.assertEqual(64, len(receipt.output_sha256))
        method, params = client.requests[0]
        self.assertEqual("turn/start", method)
        self.assertEqual("model-default", params["model"])
        self.assertEqual("medium", params["effort"])
        self.assertEqual("never", params["approvalPolicy"])
        self.assertEqual(
            {"type": "readOnly", "access": {"type": "fullAccess"}},
            params["sandboxPolicy"],
        )
        self.assertEqual(schema, params["outputSchema"])


if __name__ == "__main__":
    unittest.main()
