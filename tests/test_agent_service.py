from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from argparse import Namespace
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from zdecision.agent.db import AgentDatabase
from zdecision.agent.central_client import CentralClientError
from zdecision.sync.contracts import (
    ClaimedCaptureRequest,
    RepositoryView,
)

try:
    from zdecision.agent.service import (
        AgentConfig,
        AgentService,
        AgentServiceConfigError,
        RetryableCaptureRequestError,
        TerminalCaptureRequestError,
        configured_processor,
        load_agent_config,
        mirror_repository_mappings,
    )
except ModuleNotFoundError as error:
    SERVICE_IMPORT_ERROR: ModuleNotFoundError | None = error
else:
    SERVICE_IMPORT_ERROR = None


REQUEST_ID = "crq_" + "1" * 32
REPOSITORY_ID = "repo_" + "2" * 32
PRODUCT_ID = "prod_" + "3" * 32


def claimed_request() -> ClaimedCaptureRequest:
    return ClaimedCaptureRequest(
        request_id=REQUEST_ID,
        repository_id=REPOSITORY_ID,
        product_id=PRODUCT_ID,
        product_name="ZDecision",
        template_id="business",
        capture_scope="all_valid_sessions",
        client_action_id="web_action_001",
        lease_token="lease_0123456789abcdef",
        lease_expires_at="2026-07-31T03:00:30Z",
    )


class FakeCentralClient:
    def __init__(
        self,
        claims: list[object],
        *,
        on_fail=None,
    ) -> None:
        self.claims = list(claims)
        self.failures: list[tuple[str, str, str, bool]] = []
        self.heartbeats: list[tuple[str, str]] = []
        self.progresses: list[tuple[str, str, str]] = []
        self.calls: list[str] = []
        self.on_fail = on_fail

    def claim_next(self):
        result = self.claims.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def fail(
        self,
        request_id: str,
        lease_token: str,
        code: str,
        retryable: bool,
    ) -> None:
        if self.on_fail is not None:
            self.on_fail()
        self.calls.append("fail")
        self.failures.append((request_id, lease_token, code, retryable))

    def heartbeat(self, request_id: str, lease_token: str) -> None:
        self.calls.append("heartbeat")
        self.heartbeats.append((request_id, lease_token))

    def progress(
        self,
        request_id: str,
        lease_token: str,
        code: str,
    ) -> None:
        self.calls.append("progress")
        self.progresses.append((request_id, lease_token, code))


class FakeLeaseClient:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.heartbeat_seen = threading.Event()
        self.closed = threading.Event()

    def heartbeat(self, request_id: str, lease_token: str) -> None:
        self.heartbeat_seen.set()
        if self.failure is not None:
            raise self.failure

    def close(self) -> None:
        self.closed.set()


class FakeProcessor:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.processed: list[str] = []

    def process(self, request, client) -> None:
        self.processed.append(request.request_id)
        if self.error is not None:
            raise self.error


class _CapturedOutput:
    def __init__(self) -> None:
        self.buffer = BytesIO()


class AgentServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNone(
            SERVICE_IMPORT_ERROR,
            f"Agent service is missing: {SERVICE_IMPORT_ERROR}",
        )

    def test_service_claims_after_codex_session_has_ended(self) -> None:
        client = FakeCentralClient([claimed_request(), None])
        processor = FakeProcessor()
        service = AgentService(
            client=client,
            processor=processor,
            lease_client_factory=FakeLeaseClient,
            sleeper=lambda _: None,
        )

        self.assertTrue(service.run_once())
        self.assertEqual([REQUEST_ID], processor.processed)
        self.assertFalse(service.run_once())

    def test_declared_and_unexpected_failures_are_sanitized(self) -> None:
        cases = (
            (
                RetryableCaptureRequestError("central_temporarily_unavailable"),
                "central_temporarily_unavailable",
                True,
            ),
            (
                TerminalCaptureRequestError("source_not_interactive"),
                "source_not_interactive",
                False,
            ),
            (
                RuntimeError("raw local path /Users/demo"),
                "unexpected_processor_error",
                True,
            ),
        )
        for error, expected_code, retryable in cases:
            with self.subTest(error=type(error).__name__):
                client = FakeCentralClient([claimed_request()])
                service = AgentService(
                    client=client,
                    processor=FakeProcessor(error),
                    lease_client_factory=FakeLeaseClient,
                    sleeper=lambda _: None,
                )

                self.assertTrue(service.run_once())
                self.assertEqual(
                    (
                        REQUEST_ID,
                        "lease_0123456789abcdef",
                        expected_code,
                        retryable,
                    ),
                    client.failures[0],
                )

    def test_forever_loop_survives_claim_failure(self) -> None:
        class StopLoop(Exception):
            pass

        delays: list[float] = []

        def stop_after_two(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 2:
                raise StopLoop()

        service = AgentService(
            client=FakeCentralClient([ConnectionError("offline"), None]),
            processor=FakeProcessor(),
            lease_client_factory=FakeLeaseClient,
            sleeper=stop_after_two,
        )

        with self.assertRaises(StopLoop):
            service.run_forever()
        self.assertEqual([5.0, 5.0], delays)

    def test_service_renews_lease_while_processor_is_blocked(self) -> None:
        lease_client = FakeLeaseClient()

        class BlockingProcessor:
            def process(self, request, client) -> None:
                if not lease_client.heartbeat_seen.wait(timeout=1.0):
                    raise AssertionError(
                        "independent renewal did not run"
                    )

        service = AgentService(
            client=FakeCentralClient([claimed_request()]),
            processor=BlockingProcessor(),
            lease_client_factory=lambda: lease_client,
            lease_interval_seconds=0.001,
            sleeper=lambda _: None,
        )

        self.assertTrue(service.run_once())
        self.assertTrue(lease_client.closed.is_set())

    def test_renewal_failure_blocks_mutations_and_old_token_failure(
        self,
    ) -> None:
        lease_client = FakeLeaseClient(
            CentralClientError("central_request_rejected")
        )
        client = FakeCentralClient([claimed_request()])

        class MutatingProcessor:
            def process(self, request, guarded_client) -> None:
                if not lease_client.heartbeat_seen.wait(timeout=1.0):
                    raise AssertionError("renewal failure was not observed")
                guarded_client.progress(
                    request.request_id,
                    request.lease_token,
                    "capturing_sessions",
                )

        service = AgentService(
            client=client,
            processor=MutatingProcessor(),
            lease_client_factory=lambda: lease_client,
            lease_interval_seconds=0.001,
            sleeper=lambda _: None,
        )

        self.assertTrue(service.run_once())

        self.assertEqual([], client.progresses)
        self.assertEqual([], client.failures)
        self.assertTrue(lease_client.closed.is_set())

    def test_processor_failure_quiesces_before_heartbeat_and_fail(
        self,
    ) -> None:
        lease_client = FakeLeaseClient()

        def require_closed() -> None:
            if not lease_client.closed.is_set():
                raise AssertionError("renewal client is still active")

        client = FakeCentralClient(
            [claimed_request()], on_fail=require_closed
        )
        service = AgentService(
            client=client,
            processor=FakeProcessor(
                RetryableCaptureRequestError(
                    "central_temporarily_unavailable"
                )
            ),
            lease_client_factory=lambda: lease_client,
            sleeper=lambda _: None,
        )

        self.assertTrue(service.run_once())

        self.assertTrue(lease_client.closed.is_set())
        self.assertEqual(["heartbeat", "fail"], client.calls)

    def test_config_is_owner_only_and_mirrors_repository_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "agent.json"
            path.write_text(
                json.dumps(
                    {
                        "central_url": "http://127.0.0.1:8765",
                        "organization_id": "org_demo",
                        "device_id": "device_demo",
                        "device_token": "device-secret-token",
                        "repositories": [
                            {
                                "repository_id": REPOSITORY_ID,
                                "product_id": PRODUCT_ID,
                                "product_name": "ZDecision",
                                "enabled": True,
                            }
                        ],
                    }
                ),
                "utf-8",
            )
            os.chmod(path, 0o600)
            config = load_agent_config(path)
            database = AgentDatabase.open(root / "state.sqlite3")
            try:
                mirror_repository_mappings(database, config)
                mapping = database.get_repository_mapping(REPOSITORY_ID)
            finally:
                database.close()

            self.assertIsInstance(config, AgentConfig)
            self.assertEqual(PRODUCT_ID, mapping.product_id)
            self.assertTrue(mapping.enabled)

            os.chmod(path, 0o644)
            with self.assertRaisesRegex(
                AgentServiceConfigError, "agent_config_permissions_invalid"
            ):
                load_agent_config(path)

    def test_service_install_publishes_locator_only_after_config_validation(
        self,
    ) -> None:
        from zdecision.agent.cli import (
            _run_service_command,
            config_locator_path,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config_path = root / "agent.json"
            config_path.write_text("{}", "utf-8")
            os.chmod(config_path, 0o600)
            state_path = root / "state" / "agent" / "zdecision.sqlite3"
            locator = config_locator_path(
                {"ZDECISION_STATE_DIR": str(root / "state")}
            )
            output = _CapturedOutput()
            error_output = _CapturedOutput()
            with patch("sys.stdout", output), patch(
                "sys.stderr", error_output
            ), patch(
                "zdecision.agent.launchd.install_launch_agent"
            ) as install:
                result = _run_service_command(
                    Namespace(service_action="install", config=str(config_path)),
                    state_path,
                )
            self.assertEqual(1, result)
            self.assertFalse(locator.exists())
            install.assert_not_called()
            self.assertNotIn(
                b"RAW_DEVICE_TOKEN_NOT_IN_OUTPUT",
                output.buffer.getvalue() + error_output.buffer.getvalue(),
            )

            config_path.write_text(
                json.dumps(
                    {
                        "central_url": "http://127.0.0.1:8765",
                        "organization_id": "org_demo",
                        "device_id": "device_demo",
                        "device_token": "RAW_DEVICE_TOKEN_NOT_IN_OUTPUT",
                        "repositories": [
                            {
                                "repository_id": REPOSITORY_ID,
                                "product_id": PRODUCT_ID,
                                "product_name": "ZDecision",
                                "enabled": True,
                            }
                        ],
                    }
                ),
                "utf-8",
            )
            os.chmod(config_path, 0o600)
            output = _CapturedOutput()
            installed_path = root / "LaunchAgents" / "zdecision.plist"
            with patch("sys.stdout", output), patch(
                "zdecision.agent.launchd.install_launch_agent",
                return_value=installed_path,
            ):
                result = _run_service_command(
                    Namespace(service_action="install", config=str(config_path)),
                    state_path,
                )
            self.assertEqual(0, result)
            self.assertEqual(
                config_path,
                Path(json.loads(locator.read_text())["agent_config_path"]),
            )
            self.assertNotIn(
                b"RAW_DEVICE_TOKEN_NOT_IN_OUTPUT", output.buffer.getvalue()
            )

    def test_service_run_refreshes_locator_before_constructing_central_client(
        self,
    ) -> None:
        from zdecision.agent.cli import (
            _run_service_command,
            config_locator_path,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state_path = root / "state" / "agent" / "zdecision.sqlite3"
            locator = config_locator_path(
                {"ZDECISION_STATE_DIR": str(root / "state")}
            )
            config_path = root / "agent.json"
            config_path.write_text(
                json.dumps(
                    {
                        "central_url": "http://127.0.0.1:8765",
                        "organization_id": "org_demo",
                        "device_id": "device_demo",
                        "device_token": "device-secret-token",
                        "repositories": [
                            {
                                "repository_id": REPOSITORY_ID,
                                "product_id": PRODUCT_ID,
                                "product_name": "ZDecision",
                                "enabled": True,
                            }
                        ],
                    }
                ),
                "utf-8",
            )
            os.chmod(config_path, 0o600)

            class FakeClient:
                def __init__(self, central_url, token) -> None:
                    located_path = Path(
                        json.loads(locator.read_text("utf-8"))["agent_config_path"]
                    )
                    if located_path != config_path:
                        raise AssertionError("locator was not refreshed first")

                def close(self) -> None:
                    pass

            output = _CapturedOutput()
            with patch("sys.stdout", output), patch(
                "zdecision.agent.central_client.CentralClient", FakeClient
            ), patch(
                "zdecision.agent.service.configured_processor", return_value=None
            ), patch.object(AgentService, "run_forever", return_value=None):
                result = _run_service_command(
                    Namespace(service_action="run", config=str(config_path)),
                    state_path,
                )
            self.assertEqual(0, result)
            self.assertEqual(
                config_path,
                Path(json.loads(locator.read_text())["agent_config_path"]),
            )

    def test_configured_service_builds_the_on_demand_processor(
        self,
    ) -> None:
        config = AgentConfig(
            central_url="http://127.0.0.1:8765",
            organization_id="org_demo",
            device_id="device_demo",
            device_token="device-secret-token",
            repositories=(
                RepositoryView(
                    repository_id=REPOSITORY_ID,
                    product_id=PRODUCT_ID,
                    product_name="ZDecision",
                    enabled=True,
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state_path = root / "agent" / "zdecision.sqlite3"
            database = AgentDatabase.open(state_path)
            self.addCleanup(database.close)
            fake_gateway = object()
            with patch(
                "zdecision.app_server.gateway.AppServerGateway.connect",
                return_value=fake_gateway,
            ) as connect:
                processor = configured_processor(
                    database, config, state_path
                )

            from zdecision.agent.capture_processor import (
                OnDemandCaptureProcessor,
            )

            self.assertIsInstance(
                processor, OnDemandCaptureProcessor
            )
            connect.assert_called_once_with(database=database)
            self.assertEqual(state_path, processor.control_store.path)
            processor.session_index.close()
            processor.request_state.close()
            processor.control_store.close()
            processor.capture_runner.operation_store.close()


if __name__ == "__main__":
    unittest.main()
