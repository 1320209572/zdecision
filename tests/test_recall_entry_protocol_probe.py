from __future__ import annotations

import io
import queue
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from zdecision.app_server.gateway import AppServerUnavailable, InvalidAppServerResponse
from zdecision.app_server.jsonl import AppServerEOF, AppServerTimeout

from tests.recall_entry_protocol_probe import (
    forbid_controlled_process,
    launch_desktop_proxy,
    main,
    probe_known_thread,
)


THREAD_ID = "019fdf3f-2b42-79f1-b049-c8e464c330ab"
PEER_TEXT = "peer-secret-must-not-leak"


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.returncode = -9


class ThreadReadTransport:
    def __init__(self, reply: object) -> None:
        self.reply = reply
        self.incoming: queue.Queue[object] = queue.Queue()
        self.closed = False

    def send(self, message: dict[str, object]) -> None:
        if message["method"] == "initialize":
            self.incoming.put({"id": message["id"], "result": {}})
        elif message["method"] == "thread/read":
            self.incoming.put({"id": message["id"], "result": self.reply})

    def receive(self, timeout_seconds: float) -> object:
        try:
            item = self.incoming.get(timeout=timeout_seconds)
        except queue.Empty:
            raise AppServerTimeout("fixture timeout") from None
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True
        self.incoming.put(AppServerEOF("fixture closed"))


class FailingTransport:
    def send(self, message: dict[str, object]) -> None:
        raise AppServerEOF(PEER_TEXT)

    def receive(self, timeout_seconds: float) -> object:
        raise AppServerEOF(PEER_TEXT)

    def close(self) -> None:
        pass


class ImmediateFailureTransport:
    def __init__(self, failure: BaseException) -> None:
        self.failure = failure

    def send(self, message: dict[str, object]) -> None:
        raise self.failure

    def receive(self, timeout_seconds: float) -> object:
        raise self.failure

    def close(self) -> None:
        pass


class CloseTrackingTransport:
    def __init__(self) -> None:
        self.closed = False

    def send(self, message: dict[str, object]) -> None:
        raise AssertionError("connect fixture must not send")

    def receive(self, timeout_seconds: float) -> object:
        raise AssertionError("connect fixture must not receive")

    def close(self) -> None:
        self.closed = True


class RecallEntryProtocolProbeTests(unittest.TestCase):
    def fake_process(
        self, command: tuple[str, ...], commands: list[tuple[str, ...]]
    ) -> FakeProcess:
        commands.append(command)
        return FakeProcess()

    def test_launches_only_the_existing_desktop_proxy(self) -> None:
        """This catches a probe that launches a controlled or shell command."""
        commands: list[tuple[str, ...]] = []
        transport = launch_desktop_proxy(
            lambda command: self.fake_process(tuple(command), commands)
        )
        try:
            self.assertEqual(commands, [("codex", "app-server", "proxy")])
        finally:
            transport.close()

    def test_main_prints_bounded_json_when_proxy_launch_fails(self) -> None:
        """This catches proxy launch errors escaping without a canonical result."""
        output = io.StringIO()
        with patch(
            "tests.recall_entry_protocol_probe.launch_desktop_proxy",
            side_effect=OSError(PEER_TEXT),
        ):
            with redirect_stdout(output):
                result = main(["thread", "--thread-id", THREAD_ID])

        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), '{"gate":"0A","status":"FAIL"}\n')
        self.assertNotIn(PEER_TEXT, output.getvalue())

    def test_probe_never_falls_back_to_a_controlled_app_server(self) -> None:
        """This catches host-route failure silently starting a controlled process."""
        controlled_process_launches = 0
        with self.assertRaises(AppServerUnavailable):
            probe_known_thread(thread_id=THREAD_ID, transport=FailingTransport())
        self.assertEqual(controlled_process_launches, 0)

    def test_probe_returns_only_the_sanitized_host_route_result(self) -> None:
        """This catches the probe exposing a Thread payload or wrong route result."""
        result = probe_known_thread(
            thread_id=THREAD_ID,
            transport=ThreadReadTransport(self.thread_reply()),
        )

        self.assertEqual(
            result,
            {
                "gate": "0A",
                "route": "host_unix",
                "thread_match": True,
                "endpoint_category": "desktop_default_control_socket",
            },
        )

    def test_invalid_thread_id_is_rejected_without_echoing_it(self) -> None:
        """This catches forwarding an operator-supplied non-UUID task identifier."""
        invalid_thread_id = f"not-a-thread-id-{PEER_TEXT}"
        with self.assertRaises(ValueError) as error:
            probe_known_thread(
                thread_id=invalid_thread_id,
                transport=ThreadReadTransport(self.thread_reply()),
            )
        self.assertNotIn(PEER_TEXT, str(error.exception))

    def test_different_valid_uuid_is_rejected_as_not_the_gate_task(self) -> None:
        """This catches probing a valid UUID other than the bound Gate 0A task."""
        different_thread_id = "019fdf3f-2b42-79f1-b049-c8e464c330ac"
        reply = self.thread_reply(thread_id=different_thread_id)
        thread = reply["thread"]
        assert isinstance(thread, dict)
        thread["sessionId"] = different_thread_id
        with self.assertRaises(ValueError):
            probe_known_thread(
                thread_id=different_thread_id,
                transport=ThreadReadTransport(reply),
            )

    def test_probe_closes_transport_when_gateway_connection_fails(self) -> None:
        """This catches a connection failure leaking the supplied host transport."""
        transport = CloseTrackingTransport()
        with patch(
            "tests.recall_entry_protocol_probe.AppServerGateway.connect",
            side_effect=AppServerUnavailable("fixture failure"),
        ):
            with self.assertRaises(AppServerUnavailable):
                probe_known_thread(thread_id=THREAD_ID, transport=transport)

        self.assertTrue(transport.closed)

    def test_non_object_reply_is_a_bounded_failure(self) -> None:
        """This catches accepting an invalid thread/read response shape."""
        with self.assertRaises(InvalidAppServerResponse) as error:
            probe_known_thread(
                thread_id=THREAD_ID,
                transport=ThreadReadTransport([PEER_TEXT]),
            )
        self.assertNotIn(PEER_TEXT, str(error.exception))

    def test_wrong_returned_thread_id_is_a_bounded_failure(self) -> None:
        """This catches treating a different returned Thread as the requested task."""
        with self.assertRaises(InvalidAppServerResponse) as error:
            probe_known_thread(
                thread_id=THREAD_ID,
                transport=ThreadReadTransport(
                    self.thread_reply(thread_id=f"019fdf3f-2b42-79f1-b049-{PEER_TEXT}")
                ),
            )
        self.assertNotIn(PEER_TEXT, str(error.exception))

    def test_eof_and_timeout_are_bounded_without_peer_text(self) -> None:
        """This catches peer text escaping through EOF or timeout failures."""
        for failure in (AppServerEOF(PEER_TEXT), AppServerTimeout(PEER_TEXT)):
            with self.assertRaises(AppServerUnavailable) as error:
                probe_known_thread(
                    thread_id=THREAD_ID,
                    transport=ImmediateFailureTransport(failure),
                )
            self.assertNotIn(PEER_TEXT, str(error.exception))

    def test_peer_text_fields_are_not_echoed_on_invalid_identity(self) -> None:
        """This catches unsafe peer text leaking when Thread identity validation fails."""
        reply = self.thread_reply()
        thread = reply["thread"]
        assert isinstance(thread, dict)
        thread["cwd"] = PEER_TEXT
        with self.assertRaises(InvalidAppServerResponse) as error:
            probe_known_thread(
                thread_id=THREAD_ID, transport=ThreadReadTransport(reply)
            )
        self.assertNotIn(PEER_TEXT, str(error.exception))

    def test_controlled_process_factory_is_always_forbidden(self) -> None:
        """This catches accidentally making the forbidden fallback usable."""
        with self.assertRaises(AppServerUnavailable):
            forbid_controlled_process()

    @staticmethod
    def thread_reply(thread_id: str = THREAD_ID) -> dict[str, object]:
        return {
            "thread": {
                "id": thread_id,
                "sessionId": THREAD_ID,
                "forkedFromId": None,
                "cwd": "/tmp/recall-entry-probe",
                "ephemeral": False,
                "turns": [],
            }
        }
