from __future__ import annotations

import threading
import unittest

from zdecision.agent.central_client import CentralClientError
from zdecision.agent.request_lease import (
    LeaseAwareCentralClient,
    RequestLeaseSession,
)


REQUEST_ID = "crq_" + "1" * 32
LEASE_TOKEN = "lease_0123456789abcdef"


class LeaseClient:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.heartbeat_seen = threading.Event()
        self.closed = threading.Event()
        self.count = 0

    def heartbeat(self, request_id: str, lease_token: str) -> None:
        self.count += 1
        self.heartbeat_seen.set()
        if self.failure is not None:
            raise self.failure

    def close(self) -> None:
        self.closed.set()


class RequestLeaseSessionTest(unittest.TestCase):
    def test_renews_while_foreground_is_blocked_and_quiesces(self) -> None:
        lease_client = LeaseClient()
        session = RequestLeaseSession(
            REQUEST_ID,
            LEASE_TOKEN,
            client_factory=lambda: lease_client,
            interval_seconds=0.001,
        )

        session.start()
        self.assertTrue(lease_client.heartbeat_seen.wait(timeout=1.0))
        session.quiesce()

        self.assertGreaterEqual(lease_client.count, 1)
        self.assertTrue(lease_client.closed.is_set())

    def test_first_renewal_failure_is_rethrown_on_foreground(self) -> None:
        lease_client = LeaseClient(
            failure=CentralClientError("central_request_rejected")
        )
        session = RequestLeaseSession(
            REQUEST_ID,
            LEASE_TOKEN,
            client_factory=lambda: lease_client,
            interval_seconds=0.001,
        )

        session.start()
        self.assertTrue(lease_client.heartbeat_seen.wait(timeout=1.0))
        with self.assertRaisesRegex(
            CentralClientError, "central_request_rejected"
        ):
            session.checkpoint()
        self.assertTrue(session.uncertain)
        with self.assertRaises(CentralClientError):
            session.quiesce()

    def test_complete_quiesces_then_renews_once_before_terminal_call(
        self,
    ) -> None:
        lease_client = LeaseClient()
        session = RequestLeaseSession(
            REQUEST_ID,
            LEASE_TOKEN,
            client_factory=lambda: lease_client,
            interval_seconds=10.0,
        )
        session.start()
        calls: list[str] = []

        class Foreground:
            def heartbeat(self, request_id: str, lease_token: str) -> None:
                if not lease_client.closed.is_set():
                    raise AssertionError(
                        "renewal worker was not quiesced"
                    )
                calls.append("heartbeat")

            def complete(
                self,
                request_id: str,
                lease_token: str,
                batch_digest: str,
            ) -> None:
                calls.append("complete")

        guarded = LeaseAwareCentralClient(Foreground(), session)
        guarded.complete(REQUEST_ID, LEASE_TOKEN, "a" * 64)

        self.assertEqual(["heartbeat", "complete"], calls)


if __name__ == "__main__":
    unittest.main()
